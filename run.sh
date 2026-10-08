#!/usr/bin/env bash
# Run one k6 test against the demo and append the result to results/runs.jsonl.
#
# Usage: ./run.sh <label> [KEY=VALUE ...]
#
# Keys for the applications (API_CONCURRENCY, INSTRUMENT_WAIT, BACKEND_*,
# *_CPUS) restart api and backend when they differ from the running values.
# Keys for k6 (MODEL, VUS, RATE, DURATION, PRE_VUS, MAX_VUS, P95_MS, TIMEOUT)
# are passed to the script. K6_OUT=none disables the OpenTelemetry output.
set -euo pipefail
cd "$(dirname "$0")"

# k6 runs as a non-root user in its container and writes its summary here.
mkdir -p results
chmod 777 results

label=$1; shift
app_env=(); k6_env=(); k6_out=opentelemetry
for kv in "$@"; do
  case $kv in
    API_*|INSTRUMENT_WAIT=*|TELEMETRY_DISABLED=*|BACKEND_*|K6_CPUS=*) app_env+=("$kv") ;;
    K6_OUT=*) k6_out=${kv#K6_OUT=} ;;
    *) k6_env+=(-e "$kv") ;;
  esac
done

# Restart the applications with the requested settings. env(1) keeps the
# settings out of the caller's shell.
env "${app_env[@]}" docker compose up -d --wait api backend >/dev/null 2>&1
sleep 3

# Sample CPU usage of every container once per second while k6 runs.
stats=results/$label.stats
: >"$stats"
( while :; do docker stats --no-stream --format '{{.Name}} {{.CPUPerc}}' >>"$stats" 2>/dev/null; done ) &
sampler=$!
trap 'kill $sampler 2>/dev/null || true' EXIT

# Record the settings the containers actually run with, not only the ones
# given on the command line. Compose defaults are invisible otherwise.
config=$(env "${app_env[@]}" python3 - <<'PY'
import json, subprocess
out = {}
for svc in ("api", "backend"):
    cid = subprocess.run(["docker", "compose", "ps", "-q", svc], capture_output=True, text=True).stdout.strip()
    info = json.loads(subprocess.run(["docker", "inspect", cid], capture_output=True, text=True).stdout)[0]
    env = dict(e.split("=", 1) for e in info["Config"]["Env"])
    keep = {k: v for k, v in env.items() if k.startswith(("API_", "BACKEND_", "INSTRUMENT_", "TELEMETRY_", "OTEL_SERVICE_NAME"))}
    out[svc] = {"env": keep, "cpus": info["HostConfig"]["NanoCpus"] / 1e9, "image": info["Image"]}
k6img = subprocess.run(["docker", "compose", "--profile", "tools", "config", "--images"], capture_output=True, text=True).stdout.split()
out["k6_image"] = next((i for i in k6img if i.startswith("grafana/k6")), None)
out["k6_cpus"] = float(__import__("os").environ.get("K6_CPUS", "1"))
out["script_sha256"] = __import__("hashlib").sha256(open("k6/test.js", "rb").read()).hexdigest()
print(json.dumps(out))
PY
)

start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
out_flag=(); [ "$k6_out" != none ] && out_flag=(-o "$k6_out")
set +e
env "${app_env[@]}" docker compose run --rm -e LABEL="$label" "${k6_env[@]}" \
  k6 run --quiet "${out_flag[@]}" /scripts/test.js >results/$label.stdout 2>results/$label.stderr
code=$?
set -e
summary=$(grep '^{' results/$label.stdout || true)
end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
kill $sampler 2>/dev/null || true

cpu=$(python3 - "$stats" <<'PY'
import collections, json, sys
acc = collections.defaultdict(list)
for line in open(sys.argv[1]):
    parts = line.split()
    if len(parts) == 2 and parts[0].startswith('cndw-perf-'):
        acc[parts[0].removeprefix('cndw-perf-').rsplit('-', 1)[0]].append(float(parts[1].rstrip('%')))
print(json.dumps({k: round(sum(v) / len(v), 1) for k, v in sorted(acc.items())}))
PY
)

python3 - "$label" "$start" "$end" "$cpu" "$summary" "$code" "$*" "$config" <<'PY' | tee -a results/runs.jsonl
import json, sys
label, start, end, cpu, summary, code, args, config = sys.argv[1:]
rec = {"label": label, "start": start, "end": end, "args": args, "k6_exit": int(code),
       "config": json.loads(config), "cpu_pct": json.loads(cpu), **json.loads(summary or "{}")}
print(json.dumps(rec, ensure_ascii=False))
PY
