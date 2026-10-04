#!/usr/bin/env bash
# Age one Floppy container under repeated real work and sample it throughout.
#
# benchmark_memory.sh answers "is image A lighter than image B when fresh".
# This answers the different question: does a process that has done hours of
# work return to the footprint it started with, or does it stair-step upward.
# It therefore keeps ONE container for the whole run and never tears it down
# between cycles.
#
# Usage:
#   scripts/memory_aging.sh --image floppy:aging-head --hours 6
#
# Output directory holds:
#   samples/<epoch>.json   full process sample, one per interval
#   workload.ndjson        one record per completed task/request phase
#   run.json               the run's own parameters and topology
#
# The compose project, volumes and Redis are disposable and separate from any
# application stack on this machine.
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE=""
HOURS="6"
SAMPLE_INTERVAL_SECONDS="${FLOPPY_AGING_SAMPLE_INTERVAL:-20}"
STARTUP_TIMEOUT_SECONDS="${FLOPPY_AGING_STARTUP_TIMEOUT:-900}"
MEMORY_LIMIT="${FLOPPY_AGING_MEMORY_LIMIT:-2000000000}"
WEB_CONCURRENCY="${FLOPPY_AGING_WEB_CONCURRENCY:-1}"
SCALES="${FLOPPY_AGING_SCALES:-500}"
CYCLES="${FLOPPY_AGING_CYCLES:-500}"
REQUEST_WORKERS="${FLOPPY_AGING_REQUEST_WORKERS:-2}"
DATABASE=""
OUTPUT_DIR="${FLOPPY_AGING_OUTPUT_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/floppy-aging.XXXXXX")}"

usage() { awk 'NR > 1 && /^set -euo/ { exit } NR > 1' "$0"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:?missing image}"; shift 2 ;;
    --hours) HOURS="${2:?missing hours}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:?missing output directory}"; shift 2 ;;
    --sample-interval-seconds) SAMPLE_INTERVAL_SECONDS="${2:?missing interval}"; shift 2 ;;
    --web-concurrency) WEB_CONCURRENCY="${2:?missing worker count}"; shift 2 ;;
    --memory-limit) MEMORY_LIMIT="${2:?missing limit}"; shift 2 ;;
    --scales) SCALES="${2:?missing scales}"; shift 2 ;;
    --database) DATABASE="${2:?missing database}"; shift 2 ;;
    --cycles) CYCLES="${2:?missing cycles}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$IMAGE" ] || { echo "--image is required." >&2; exit 2; }

PROJECT="floppy-memory-aging-$$"
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.memory-benchmark.yml)
export FLOPPY_BENCHMARK_IMAGE="$IMAGE"
export FLOPPY_BENCHMARK_MEMORY_LIMIT="$MEMORY_LIMIT"
export FLOPPY_BENCHMARK_WEB_CONCURRENCY="$WEB_CONCURRENCY"
export FLOPPY_BENCHMARK_GUNICORN_CMD_ARGS="${FLOPPY_AGING_GUNICORN_CMD_ARGS:-}"
export FLOPPY_BENCHMARK_MAX_WORKER_MEMORY_BYTES="${FLOPPY_AGING_MAX_WORKER_MEMORY_BYTES:-}"

mkdir -p "$OUTPUT_DIR/samples"
echo "Output: $OUTPUT_DIR"

# A synthetic fixture measures the code; a copy of a real database measures
# what the code does to a real library. The file named here is only ever read:
# it is copied into the project's own disposable volume before anything starts.
if [ -n "$DATABASE" ]; then
  [ -f "$DATABASE" ] || { echo "--database must be an existing SQLite file." >&2; exit 2; }
  docker volume create "${PROJECT}_benchmark_db" >/dev/null
  docker run --rm \
    -v "${PROJECT}_benchmark_db:/db" \
    -v "$(cd "$(dirname "$DATABASE")" && pwd):/src:ro" \
    alpine:3.20 sh -c "cp /src/$(basename "$DATABASE") /db/db.sqlite3 && chown -R 1000:1000 /db" >/dev/null
  echo "Seeded the benchmark volume from $(basename "$DATABASE")"
fi

SAMPLER_PID=""
cleanup() {
  [ -n "$SAMPLER_PID" ] && kill "$SAMPLER_PID" 2>/dev/null || true
  "${COMPOSE[@]}" logs --no-color --tail 4000 >"$OUTPUT_DIR/container.log" 2>&1 || true
  "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${COMPOSE[@]}" up -d --wait --wait-timeout "$STARTUP_TIMEOUT_SECONDS" >"$OUTPUT_DIR/up.log" 2>&1 || {
  echo "Container did not become healthy; see $OUTPUT_DIR/up.log" >&2
  exit 1
}

# The identity block is what makes a sample attributable to a build later.
{
  echo "=== run ==="
  echo "image=$IMAGE hours=$HOURS interval=${SAMPLE_INTERVAL_SECONDS}s limit=$MEMORY_LIMIT"
  echo "web_concurrency=$WEB_CONCURRENCY scales=$SCALES cycles=$CYCLES request_workers=$REQUEST_WORKERS"
  echo "gunicorn_cmd_args=${FLOPPY_BENCHMARK_GUNICORN_CMD_ARGS:-<image default>}"
  echo "heavy_routes=${FLOPPY_AGING_HEAVY_ROUTES:-0}"
  echo "database=${DATABASE:-<empty volume>}"
  echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) epoch=$(date +%s)"
  echo "=== build info ==="
  "${COMPOSE[@]}" exec -T --user root floppy cat /etc/floppy-build-info 2>/dev/null || true
  echo "=== topology ==="
  "${COMPOSE[@]}" logs --no-color 2>/dev/null | grep -E '^\[(entrypoint|gunicorn)\]' || true
} >"$OUTPUT_DIR/identity.txt" 2>&1

"${COMPOSE[@]}" cp scripts/container_memory_sample.py floppy:/tmp/floppy-memory-sample.py
"${COMPOSE[@]}" cp scripts/memory_workload.py floppy:/tmp/floppy-memory-workload.py

# Sampling runs independently of the workload so a stalled task still leaves a
# memory trace rather than a gap.
sample_forever() {
  while true; do
    stamp="$(date +%s)"
    "${COMPOSE[@]}" exec -T --user root floppy sh -c '
      if [ -r /sys/fs/cgroup/memory.current ]; then
        export FLOPPY_CGROUP_CURRENT_BEFORE_SAMPLER="$(cat /sys/fs/cgroup/memory.current)"
      fi
      exec python /tmp/floppy-memory-sample.py
    ' >"$OUTPUT_DIR/samples/$stamp.json" 2>/dev/null || rm -f "$OUTPUT_DIR/samples/$stamp.json"
    sleep "$SAMPLE_INTERVAL_SECONDS"
  done
}
sample_forever &
SAMPLER_PID=$!

DEADLINE=$(( $(date +%s) + $(printf '%.0f' "$(echo "$HOURS * 3600" | bc)") ))

"${COMPOSE[@]}" exec -T --user abc \
  -e FLOPPY_MEMORY_FIXTURE=disposable \
  -e FLOPPY_MEMORY_SCALES="$SCALES" \
  -e FLOPPY_MEMORY_CYCLES="$CYCLES" \
  -e FLOPPY_MEMORY_REQUEST_WORKERS="$REQUEST_WORKERS" \
  -e FLOPPY_MEMORY_HISTORY_DAYS="${FLOPPY_AGING_HISTORY_DAYS:-90}" \
  -e FLOPPY_MEMORY_HEAVY_ROUTES="${FLOPPY_AGING_HEAVY_ROUTES:-0}" \
  floppy python /tmp/floppy-memory-workload.py >"$OUTPUT_DIR/workload.ndjson" 2>"$OUTPUT_DIR/workload.err" &
WORKLOAD_PID=$!

# Stop at whichever comes first: the requested duration or the cycle budget.
while kill -0 "$WORKLOAD_PID" 2>/dev/null; do
  [ "$(date +%s)" -ge "$DEADLINE" ] && { kill "$WORKLOAD_PID" 2>/dev/null || true; break; }
  sleep 10
done
wait "$WORKLOAD_PID" 2>/dev/null || true

# One last sample after the work stops, to separate peak from what is retained.
sleep 60
stamp="$(date +%s)"
"${COMPOSE[@]}" exec -T --user root floppy sh -c '
  if [ -r /sys/fs/cgroup/memory.current ]; then
    export FLOPPY_CGROUP_CURRENT_BEFORE_SAMPLER="$(cat /sys/fs/cgroup/memory.current)"
  fi
  exec python /tmp/floppy-memory-sample.py
' >"$OUTPUT_DIR/samples/$stamp-settled.json" 2>/dev/null || true

echo "Aging run complete: $OUTPUT_DIR"
