#!/bin/sh

set -eu

BACKEND_HOST="${JOB_APPLIER_BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${JOB_APPLIER_BACKEND_PORT:-8000}"
PANEL_PORT="${JOB_APPLIER_PANEL_PORT:-3000}"

export JOB_APPLIER_DATABASE_URL="${JOB_APPLIER_DATABASE_URL:-sqlite:////data/job-applier.db}"
export JOB_APPLIER_DATA_DIR="${JOB_APPLIER_DATA_DIR:-/data}"

mkdir -p "${JOB_APPLIER_DATA_DIR}"

if [ -n "${DISPLAY:-}" ]; then
  echo "Host display detected at ${DISPLAY}. Playwright can launch a visible browser."
else
  echo "DISPLAY is not set. Playwright should run headless unless you pass a host display."
fi

cleanup() {
  kill "${BACKEND_PID}" "${PANEL_PID}" 2>/dev/null || true
}

trap cleanup INT TERM EXIT

uv run --no-sync uvicorn job_applier.main:app --host "${BACKEND_HOST}" --port "${BACKEND_PORT}" &
BACKEND_PID=$!

cd /app/apps/panel
npm run start &
PANEL_PID=$!

while kill -0 "${BACKEND_PID}" 2>/dev/null && kill -0 "${PANEL_PID}" 2>/dev/null; do
  sleep 1
done

cleanup
wait "${BACKEND_PID}" 2>/dev/null || true
wait "${PANEL_PID}" 2>/dev/null || true
exit 1
