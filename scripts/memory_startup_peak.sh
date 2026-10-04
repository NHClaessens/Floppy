#!/usr/bin/env bash
# Attribute a container's startup memory peak to a startup phase.
#
# The application settles far below the peak the cgroup records, so the peak
# is a separate budget from the steady state: an instance that settles at
# 500 MiB but needs 1.9 GiB to boot cannot be deployed under a 750 MiB ceiling.
# This boots one image against a supplied database and traces the cgroup every
# second from container creation to readiness, so the peak can be read against
# the entrypoint phase that produced it -- and split into anonymous memory,
# which must fit, and file cache, which the kernel can reclaim.
#
# Usage:
#   scripts/memory_startup_peak.sh --image floppy:aging-head \
#     --database /path/to/db.sqlite3 --memory-limit 750000000
#
# The database is COPIED into a disposable volume; the file given is never
# opened by the container.
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE=""
DATABASE=""
MEMORY_LIMIT="${FLOPPY_STARTUP_MEMORY_LIMIT:-0}"
READY_TIMEOUT="${FLOPPY_STARTUP_READY_TIMEOUT:-900}"
OUTPUT_DIR="${FLOPPY_STARTUP_OUTPUT_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/floppy-startup.XXXXXX")}"

usage() { awk 'NR > 1 && /^set -euo/ { exit } NR > 1' "$0"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:?missing image}"; shift 2 ;;
    --database) DATABASE="${2:?missing database}"; shift 2 ;;
    --memory-limit) MEMORY_LIMIT="${2:?missing limit}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:?missing output directory}"; shift 2 ;;
    --ready-timeout) READY_TIMEOUT="${2:?missing timeout}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$IMAGE" ] || { echo "--image is required." >&2; exit 2; }
[ -f "$DATABASE" ] || { echo "--database must be an existing SQLite file." >&2; exit 2; }

SUFFIX="$$"
NETWORK="floppy-startup-net-$SUFFIX"
REDIS="floppy-startup-redis-$SUFFIX"
APP="floppy-startup-app-$SUFFIX"
VOLUME="floppy-startup-db-$SUFFIX"
mkdir -p "$OUTPUT_DIR"
echo "Output: $OUTPUT_DIR"

cleanup() {
  docker logs "$APP" >"$OUTPUT_DIR/container.log" 2>&1 || true
  docker rm -f "$APP" "$REDIS" >/dev/null 2>&1 || true
  docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$NETWORK" >/dev/null
docker volume create "$VOLUME" >/dev/null
docker run --rm -v "$VOLUME:/db" -v "$(cd "$(dirname "$DATABASE")" && pwd):/src:ro" \
  alpine:3.20 sh -c "cp /src/$(basename "$DATABASE") /db/db.sqlite3 && chown -R 1000:1000 /db" >/dev/null
docker run -d --name "$REDIS" --network "$NETWORK" redis:8-alpine \
  redis-server --appendonly no --save "" --maxmemory 256mb --maxmemory-policy volatile-lru >/dev/null

LIMIT_ARGS=()
if [ "$MEMORY_LIMIT" != "0" ]; then
  # No swap: a limit the boot can only meet by swapping is not a limit it meets.
  LIMIT_ARGS=(--memory "$MEMORY_LIMIT" --memory-swap "$MEMORY_LIMIT")
fi

STARTED_EPOCH="$(date +%s)"
docker run -d --name "$APP" --network "$NETWORK" ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} \
  -v "$VOLUME:/floppy/db" \
  -e SECRET=startup-peak-only-secret \
  -e REDIS_URL="redis://$REDIS:6379" \
  -e DEBUG=False -e DEMO_ACCOUNT_ENABLED=False -e REGISTRATION=False \
  -e WEB_CONCURRENCY="${FLOPPY_STARTUP_WEB_CONCURRENCY:-1}" \
  "$IMAGE" >/dev/null

# Traced from inside: on Docker Desktop the VM's cgroup tree is not reachable
# from the host, and `docker stats` reports neither the peak nor the anon/file
# split that decides whether a peak is a requirement or just reclaimable cache.
docker exec -d "$APP" sh -c '
  while true; do
    now=$(date +%s)
    if [ -r /sys/fs/cgroup/memory.current ]; then
      cur=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo)
      peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo)
      anon=$(awk "/^anon /{print \$2}" /sys/fs/cgroup/memory.stat 2>/dev/null)
      file=$(awk "/^file /{print \$2}" /sys/fs/cgroup/memory.stat 2>/dev/null)
      slab=$(awk "/^slab /{print \$2}" /sys/fs/cgroup/memory.stat 2>/dev/null)
      kills=$(awk "/^oom_kill /{print \$2}" /sys/fs/cgroup/memory.events 2>/dev/null)
    fi
    echo "$now cur=$cur peak=$peak anon=$anon file=$file slab=$slab oom_kill=$kills" >>/tmp/trace
    sleep 1
  done
' || echo "warning: could not attach the cgroup tracer" >&2

READY_EPOCH=""
DEADLINE=$(( STARTED_EPOCH + READY_TIMEOUT ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if ! docker inspect -f '{{.State.Running}}' "$APP" 2>/dev/null | grep -q true; then
    echo "Container exited before becoming ready." >&2
    break
  fi
  # The image defines its own health check; reuse it rather than guessing at a
  # URL and a client, which is what an in-container wget probe amounts to.
  case "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$APP" 2>/dev/null)" in
    healthy) READY_EPOCH="$(date +%s)"; break ;;
    none)
      # No health check to consult: fall back to asking the app for a page.
      if docker exec "$APP" sh -c 'wget -q -O /dev/null -T 5 http://127.0.0.1:8000/accounts/login/' >/dev/null 2>&1; then
        READY_EPOCH="$(date +%s)"
        break
      fi
      ;;
  esac
  sleep 2
done

# 60s past readiness separates the boot peak from what the running app keeps.
[ -n "$READY_EPOCH" ] && sleep 60

docker exec "$APP" sh -c 'cat /tmp/trace 2>/dev/null' >"$OUTPUT_DIR/cgroup-trace.txt" 2>/dev/null || true
docker logs "$APP" >"$OUTPUT_DIR/container.log" 2>&1 || true
docker exec "$APP" sh -c 'ps -o pid,comm 2>/dev/null' >"$OUTPUT_DIR/processes.txt" 2>&1 || true

{
  echo "image=$IMAGE"
  echo "database=$DATABASE database_bytes=$(wc -c <"$DATABASE")"
  echo "memory_limit=$MEMORY_LIMIT"
  echo "started_epoch=$STARTED_EPOCH"
  echo "ready_epoch=${READY_EPOCH:-none}"
  echo "seconds_to_ready=$([ -n "$READY_EPOCH" ] && echo $((READY_EPOCH - STARTED_EPOCH)) || echo none)"
  echo "exit_state=$(docker inspect -f '{{.State.Status}} oom={{.State.OOMKilled}} exit={{.State.ExitCode}}' "$APP" 2>/dev/null)"
} >"$OUTPUT_DIR/result.txt"

cat "$OUTPUT_DIR/result.txt"
