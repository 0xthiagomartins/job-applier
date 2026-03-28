FROM node:22-bookworm-slim AS panel-builder

WORKDIR /workspace/apps/panel

COPY apps/panel/package.json apps/panel/package-lock.json ./
RUN npm ci

COPY apps/panel ./
RUN npm run build && npm prune --omit=dev


FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    JOB_APPLIER_DATA_DIR=/data \
    JOB_APPLIER_DATABASE_URL=sqlite:////data/job-applier.db \
    JOB_APPLIER_BACKEND_HOST=0.0.0.0 \
    JOB_APPLIER_BACKEND_PORT=8000 \
    JOB_APPLIER_PANEL_PORT=3000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        nodejs \
        npm \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic ./alembic
COPY docker ./docker

RUN chmod +x /app/docker/entrypoint.sh \
    && uv sync --frozen --no-dev \
    && uv run playwright install --with-deps chromium

COPY --from=panel-builder /workspace/apps/panel /app/apps/panel

EXPOSE 3000 8000
VOLUME ["/data"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
