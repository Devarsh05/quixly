#!/usr/bin/env bash
#
# Agent container entrypoint: migrate once, then supervise two long-lived children.
#
# Ordering is deliberate. `alembic upgrade head` runs ONCE, here, before anything
# serves — never inside the supervisor and never per-process. Two processes racing
# `upgrade head` against one database is a corrupt migration history, and a per-process
# migration would re-run on every worker restart. If it fails the container dies with
# alembic's own exit status; nothing starts against an un-migrated schema.
#
# The supervision rule is the point of this file: the container MUST die if EITHER child
# dies. A half-alive container — arq wedged, uvicorn still answering the health check —
# is the worst outcome available, because the platform sees green while no job is being
# consumed. That exact wedge cost a Phase 4 acceptance run. `wait -n` returns on the
# first child to exit, whichever it is, and we then take the other one down and exit
# non-zero so the platform restarts the whole container.
#
# Requires bash (`wait -n` is not POSIX; Debian's /bin/sh is dash and does not have it).

set -euo pipefail

echo "entrypoint: applying database migrations (alembic upgrade head)"
alembic upgrade head
echo "entrypoint: migrations applied"

api_pid=""
worker_pid=""

# Forward an orderly shutdown to both children, then leave. The trap is disarmed first
# so a second signal cannot re-enter this handler mid-teardown.
on_term() {
  trap '' TERM INT
  echo "entrypoint: received shutdown signal, stopping children" >&2
  kill -TERM "$api_pid" "$worker_pid" 2>/dev/null || true
  wait 2>/dev/null || true
  exit 143
}
trap on_term TERM INT

uvicorn app.main:app --host 0.0.0.0 --port 8000 &
api_pid=$!
echo "entrypoint: uvicorn started (pid ${api_pid})"

arq app.worker.WorkerSettings &
worker_pid=$!
echo "entrypoint: arq worker started (pid ${worker_pid})"

# Block until the FIRST child exits, for any reason.
set +e
wait -n
status=$?
set -e

if [ "$api_pid" -gt 0 ] && ! kill -0 "$api_pid" 2>/dev/null; then
  echo "entrypoint: uvicorn exited (status ${status})" >&2
else
  echo "entrypoint: arq worker exited (status ${status})" >&2
fi

echo "entrypoint: taking the container down so the platform restarts it" >&2
kill -TERM "$api_pid" "$worker_pid" 2>/dev/null || true
wait 2>/dev/null || true

# A clean exit from a process that is supposed to run forever is still a failure —
# exiting 0 here would let the platform treat the container as having completed.
if [ "$status" -eq 0 ]; then
  status=1
fi

exit "$status"
