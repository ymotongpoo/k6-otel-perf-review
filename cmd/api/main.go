// Command api serves GET /items/{id}. Each request does a few milliseconds of
// local work and then calls backend. At most API_CONCURRENCY calls to backend
// run at a time; other requests wait for a free slot.
//
// INSTRUMENT_WAIT=true adds a span and metrics for the time spent waiting for
// a slot. Without it, the wait is only visible as the gap between the server
// span and the client span.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"

	"github.com/ymotongpoo/k6-otel-perf-review/internal/telemetry"
)

func envInt(key string, def int) int {
	if v, err := strconv.Atoi(os.Getenv(key)); err == nil {
		return v
	}
	return def
}

func main() {
	concurrency := envInt("API_CONCURRENCY", 4)
	localMS := envInt("API_LOCAL_MS", 5)
	instrumentWait := os.Getenv("INSTRUMENT_WAIT") == "true"
	backendURL := os.Getenv("BACKEND_URL")
	if backendURL == "" {
		backendURL = "http://backend:8081"
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	shutdown, logger, err := telemetry.Setup(ctx, "catalog")
	if err != nil {
		log.Fatal(err)
	}

	tracer := otel.Tracer("catalog")
	meter := otel.Meter("catalog")
	waitHist, _ := meter.Float64Histogram("catalog.limiter.wait.duration",
		metric.WithDescription("Time spent waiting for a slot to call backend."), metric.WithUnit("s"),
		metric.WithExplicitBucketBoundaries(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
	waiting, _ := meter.Int64UpDownCounter("catalog.limiter.waiting",
		metric.WithDescription("Requests waiting for a slot to call backend."), metric.WithUnit("{request}"))

	client := &http.Client{
		Transport: otelhttp.NewTransport(&http.Transport{MaxIdleConnsPerHost: 256}),
		Timeout:   2 * time.Second,
	}
	slots := make(chan struct{}, concurrency)

	// acquire blocks until a slot to call backend is free.
	acquire := func(ctx context.Context) error {
		if !instrumentWait {
			select {
			case slots <- struct{}{}:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		start := time.Now()
		waiting.Add(ctx, 1)
		defer waiting.Add(ctx, -1)
		ctx, span := tracer.Start(ctx, "wait for backend slot", trace.WithSpanKind(trace.SpanKindInternal))
		defer span.End()
		select {
		case slots <- struct{}{}:
			waitHist.Record(ctx, time.Since(start).Seconds())
			return nil
		case <-ctx.Done():
			span.SetStatus(codes.Error, "canceled while waiting")
			return ctx.Err()
		}
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /items/{id}", func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		time.Sleep(time.Duration(localMS) * time.Millisecond)

		if err := acquire(ctx); err != nil {
			return
		}
		defer func() { <-slots }()

		req, _ := http.NewRequestWithContext(ctx, http.MethodGet,
			fmt.Sprintf("%s/stock/%s", backendURL, r.PathValue("id")), nil)
		resp, err := client.Do(req)
		if err != nil {
			logger.ErrorContext(ctx, "backend call failed", "error", err)
			http.Error(w, "backend unavailable", http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		if resp.StatusCode != http.StatusOK {
			logger.WarnContext(ctx, "backend returned error", "status", resp.StatusCode)
			http.Error(w, "backend error", http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write(body)
	})

	srv := &http.Server{Addr: ":8080", Handler: otelhttp.NewHandler(mux, "catalog")}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatal(err)
		}
	}()
	logger.Info("started", slog.Int("concurrency", concurrency), slog.Int("local_ms", localMS),
		slog.Bool("instrument_wait", instrumentWait))

	<-ctx.Done()
	sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	srv.Shutdown(sctx)
	shutdown(sctx)
}
