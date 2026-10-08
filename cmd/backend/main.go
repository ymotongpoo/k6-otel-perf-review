// Command backend is the downstream service called by api. It processes at
// most BACKEND_CAPACITY requests at a time, each taking about
// BACKEND_SERVICE_MS milliseconds. Requests beyond the capacity wait in a
// queue; when more than BACKEND_QUEUE requests are already waiting, it answers
// 503 immediately.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync/atomic"
	"syscall"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"

	"github.com/ymotongpoo/k6-otel-perf-review/internal/telemetry"
)

func envInt(key string, def int) int {
	if v, err := strconv.Atoi(os.Getenv(key)); err == nil {
		return v
	}
	return def
}

func main() {
	capacity := envInt("BACKEND_CAPACITY", 6)
	serviceMS := envInt("BACKEND_SERVICE_MS", 50)
	jitterMS := envInt("BACKEND_JITTER_MS", 10)
	queueLimit := int64(envInt("BACKEND_QUEUE", 32))

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	shutdown, logger, err := telemetry.Setup(ctx, "inventory")
	if err != nil {
		log.Fatal(err)
	}

	tracer := otel.Tracer("inventory")
	meter := otel.Meter("inventory")
	active, _ := meter.Int64UpDownCounter("http.server.active_requests",
		metric.WithDescription("Number of active HTTP server requests."), metric.WithUnit("{request}"))
	waiting, _ := meter.Int64UpDownCounter("inventory.queue.waiting",
		metric.WithDescription("Requests waiting for a processing slot."), metric.WithUnit("{request}"))
	rejected, _ := meter.Int64Counter("inventory.queue.rejected",
		metric.WithDescription("Requests rejected because the queue was full."), metric.WithUnit("{request}"))

	slots := make(chan struct{}, capacity)
	var queued atomic.Int64

	mux := http.NewServeMux()
	mux.HandleFunc("GET /stock/{id}", func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		active.Add(ctx, 1)
		defer active.Add(ctx, -1)

		if queued.Add(1) > queueLimit {
			queued.Add(-1)
			rejected.Add(ctx, 1)
			logger.WarnContext(ctx, "queue full", "queue_limit", queueLimit)
			http.Error(w, "queue full", http.StatusServiceUnavailable)
			return
		}
		waiting.Add(ctx, 1)
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
			waiting.Add(ctx, -1)
			queued.Add(-1)
			return
		}
		waiting.Add(ctx, -1)
		queued.Add(-1)
		defer func() { <-slots }()

		_, span := tracer.Start(ctx, "lookup stock")
		d := time.Duration(serviceMS)*time.Millisecond +
			time.Duration(rand.IntN(2*jitterMS+1)-jitterMS)*time.Millisecond
		time.Sleep(d)
		span.SetAttributes(attribute.String("stock.id", r.PathValue("id")))
		span.End()

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"id": r.PathValue("id"), "available": 3})
	})

	srv := &http.Server{Addr: ":8081", Handler: otelhttp.NewHandler(mux, "inventory")}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatal(err)
		}
	}()
	logger.Info("started", slog.Int("capacity", capacity), slog.Int("service_ms", serviceMS),
		slog.Int("jitter_ms", jitterMS), slog.Int64("queue_limit", queueLimit))

	<-ctx.Done()
	sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	srv.Shutdown(sctx)
	shutdown(sctx)
}
