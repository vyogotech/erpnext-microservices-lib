#!/usr/bin/env bash
# Real-fork smoke test: Gunicorn --preload + embedded RQ worker (ENABLE_RQ=1).
#
# Verifies, with real processes and a real Redis:
#   1. No RQ consumer thread runs in the Gunicorn master (job enqueued at import
#      time in the master is consumed by a forked worker pid, not the master).
#   2. Exactly ONE consumer per container (others on standby).
#   3. All workers keep serving HTTP (no post-fork deadlock).
#   4. Fail-over: SIGKILL the consuming worker -> a sibling takes over.
#   5. SIGHUP reload of the master -> jobs still consumed.
#
# Usage: tests/fork_safety/run_preload_rq_smoke.sh [lib-image]
#   default image: localhost/frappe-microservice-lib:latest (needs gunicorn, rq, flask)
set -euo pipefail

ENGINE="${CONTAINER_ENGINE:-podman}"
IMAGE="${1:-localhost/frappe-microservice-lib:latest}"
REDIS_IMAGE="${REDIS_IMAGE:-docker.io/library/redis:7-alpine}"
WORKERS="${WORKERS:-4}"
PORT="${PORT:-18765}"
SUFFIX="$$"
NET="fork-smoke-net-$SUFFIX"
REDIS="fork-smoke-redis-$SUFFIX"
APP="fork-smoke-app-$SUFFIX"
LIB_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; echo "--- app logs ---"; $ENGINE logs "$APP" 2>&1 | tail -60; exit 1; }
rcli() { $ENGINE exec "$REDIS" redis-cli "$@"; }
cleanup() { $ENGINE rm -f "$APP" "$REDIS" >/dev/null 2>&1 || true; $ENGINE network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT

wait_job() { # tag -> echoes consumer pid or fails after 20s
  for _ in $(seq 1 40); do
    local p; p="$(rcli HGET fork-smoke:jobs "$1" | tr -d '\r')"
    [[ -n "$p" ]] && { echo "$p"; return 0; }
    sleep 0.5
  done
  return 1
}
consumer_log_count() { $ENGINE logs "$APP" 2>&1 | grep -c "RQ worker consuming queue 'fork-safety-smoke'" || true; }

echo "== starting redis + gunicorn --preload (workers=$WORKERS) using $IMAGE"
$ENGINE network create "$NET" >/dev/null
$ENGINE run -d --name "$REDIS" --network "$NET" "$REDIS_IMAGE" >/dev/null
$ENGINE run -d --name "$APP" --network "$NET" -p "$PORT:8000" \
  -v "$LIB_ROOT":/src:ro,Z \
  -e PYTHONPATH=/src:/src/tests/fork_safety \
  -e ENABLE_RQ=1 -e REDIS_URL="redis://$REDIS:6379/0" \
  --entrypoint /opt/venv/bin/gunicorn "$IMAGE" \
  --preload --bind=0.0.0.0:8000 --workers="$WORKERS" --worker-class=sync \
  --worker-tmp-dir=/dev/shm --timeout=30 --error-logfile=- preload_app:app >/dev/null

for _ in $(seq 1 60); do curl -fsS "localhost:$PORT/pid" >/dev/null 2>&1 && break; sleep 0.5; done
curl -fsS "localhost:$PORT/pid" >/dev/null || fail "app did not become ready"

echo "== 1. job enqueued in master is consumed by a forked worker"
MASTER_PID="$(rcli GET fork-smoke:master_pid | tr -d '\r')"
OWNER="$(wait_job enqueued-in-master)" || fail "job enqueued in master was never consumed"
[[ "$OWNER" != "$MASTER_PID" ]] && pass "consumed by worker pid=$OWNER (master pid=$MASTER_PID)" \
  || fail "job consumed by the MASTER process (pid=$MASTER_PID) - thread is running pre-fork"

echo "== 2. exactly one consumer per container"
sleep 2
N="$(consumer_log_count)"
[[ "$N" == "1" ]] && pass "1 consuming worker, $((WORKERS-1)) on standby" || fail "expected 1 consumer, found $N"

echo "== 3. all workers keep serving HTTP (no post-fork deadlock)"
PIDS="$(for _ in $(seq 1 60); do curl -fsS -m 5 "localhost:$PORT/pid" || echo TIMEOUT; echo; done)"
grep -q TIMEOUT <<<"$PIDS" && fail "request timed out (worker hung)"
DISTINCT="$(grep -o '"pid":[0-9]*' <<<"$PIDS" | sort -u | wc -l | tr -d ' ')"
pass "60/60 requests ok across $DISTINCT worker pid(s)"

echo "== 4. fail-over: SIGKILL the consuming worker"
$ENGINE exec "$APP" /opt/venv/bin/python -c "import os,signal; os.kill($OWNER, signal.SIGKILL)"
sleep 3
curl -fsS -X POST "localhost:$PORT/enqueue/after-kill" >/dev/null
NEW_OWNER="$(wait_job after-kill)" || fail "no consumer took over after killing pid=$OWNER"
[[ "$NEW_OWNER" != "$OWNER" && "$NEW_OWNER" != "$MASTER_PID" ]] \
  && pass "sibling worker pid=$NEW_OWNER took over" || fail "unexpected consumer pid=$NEW_OWNER"

echo "== 5. SIGHUP reload of master (re-forks all workers)"
$ENGINE exec "$APP" /opt/venv/bin/python -c "import os,signal; os.kill(1, signal.SIGHUP)"
sleep 4
curl -fsS -m 5 "localhost:$PORT/pid" >/dev/null || fail "app not serving after HUP"
curl -fsS -X POST "localhost:$PORT/enqueue/after-hup" >/dev/null
HUP_OWNER="$(wait_job after-hup)" || fail "job not consumed after HUP reload"
[[ "$HUP_OWNER" != "$MASTER_PID" ]] && pass "consumed by worker pid=$HUP_OWNER after reload" \
  || fail "consumed by master after HUP"

echo
echo "ALL CHECKS PASSED"
