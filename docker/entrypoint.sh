#!/bin/sh

set -eu

BACKEND_HOST="${JOB_APPLIER_BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${JOB_APPLIER_BACKEND_PORT:-8000}"
PANEL_PORT="${JOB_APPLIER_PANEL_PORT:-3000}"
PLAYWRIGHT_MCP_URL="${JOB_APPLIER_PLAYWRIGHT_MCP_URL:-}"
PLAYWRIGHT_MCP_HOST="${JOB_APPLIER_PLAYWRIGHT_MCP_HOST:-127.0.0.1}"
PLAYWRIGHT_MCP_PORT="${JOB_APPLIER_PLAYWRIGHT_MCP_PORT:-8931}"

export JOB_APPLIER_DATABASE_URL="${JOB_APPLIER_DATABASE_URL:-sqlite:////data/job-applier.db}"
export JOB_APPLIER_DATA_DIR="${JOB_APPLIER_DATA_DIR:-/data}"

mkdir -p "${JOB_APPLIER_DATA_DIR}"

if [ -n "${DISPLAY:-}" ]; then
  echo "Host display detected at ${DISPLAY}. Playwright can launch a visible browser."
else
  echo "DISPLAY is not set. Playwright should run headless unless you pass a host display."
fi

BACKEND_PID=""
PANEL_PID=""
MCP_PID=""

cleanup() {
  if [ -n "${MCP_PID}" ]; then
    kill "${MCP_PID}" 2>/dev/null || true
  fi
  if [ -n "${BACKEND_PID}" ]; then
    kill "${BACKEND_PID}" 2>/dev/null || true
  fi
  if [ -n "${PANEL_PID}" ]; then
    kill "${PANEL_PID}" 2>/dev/null || true
  fi
}

trap cleanup INT TERM EXIT

if [ -n "${PLAYWRIGHT_MCP_URL}" ]; then
  export JOB_APPLIER_PLAYWRIGHT_MCP_URL="${PLAYWRIGHT_MCP_URL}"
  echo "Using external Playwright MCP server at ${JOB_APPLIER_PLAYWRIGHT_MCP_URL}."
else
  export JOB_APPLIER_PLAYWRIGHT_MCP_URL="http://${PLAYWRIGHT_MCP_HOST}:${PLAYWRIGHT_MCP_PORT}"
  echo "Starting local Playwright MCP server at ${JOB_APPLIER_PLAYWRIGHT_MCP_URL}."
  node /usr/local/bin/job-applier-playwright-mcp --port "${PLAYWRIGHT_MCP_PORT}" &
  MCP_PID=$!
fi

uv run --no-sync uvicorn job_applier.main:app --host "${BACKEND_HOST}" --port "${BACKEND_PORT}" &
BACKEND_PID=$!

cd /app/apps/panel
npm run start &
PANEL_PID=$!

while kill -0 "${BACKEND_PID}" 2>/dev/null && kill -0 "${PANEL_PID}" 2>/dev/null; do
  if [ -n "${MCP_PID}" ] && ! kill -0 "${MCP_PID}" 2>/dev/null; then
    break
  fi
  sleep 1
done

cleanup
if [ -n "${MCP_PID}" ]; then
  wait "${MCP_PID}" 2>/dev/null || true
fi
if [ -n "${BACKEND_PID}" ]; then
  wait "${BACKEND_PID}" 2>/dev/null || true
fi
if [ -n "${PANEL_PID}" ]; then
  wait "${PANEL_PID}" 2>/dev/null || true
fi
exit 1
