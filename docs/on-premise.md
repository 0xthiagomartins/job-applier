# On-premise runtime

The product is designed to run close to the user.

Why this matters:

- the panel and API stay inside the same app container;
- the container can also boot a local Playwright MCP sidecar when no external MCP URL is configured;
- if no external database URL is provided, the app falls back to a local SQLite file;
- Playwright can open a visible browser on the host so the user can follow the automation and step in for captchas.

## Default runtime behavior

- panel: port `3000`
- backend API: port `8000`
- local Playwright MCP sidecar: port `8931`
- default database: `sqlite:////data/job-applier.db`
- default persisted runtime data: `/data`

## Build

```bash
docker build -t job-applier .
```

## Run with local SQLite

```bash
docker run --rm -it \
  -p 3000:3000 \
  -p 8000:8000 \
  -p 8931:8931 \
  -v job-applier-data:/data \
  job-applier
```

If `JOB_APPLIER_PLAYWRIGHT_MCP_URL` is empty, the container starts its own Playwright MCP sidecar at `http://localhost:8931/mcp`.
If `JOB_APPLIER_PLAYWRIGHT_MCP_URL` is set, the container skips the local MCP and assumes you already have one running elsewhere.

## Run with a visible browser on Linux

Allow the container to use the host display:

```bash
xhost +local:docker
```

Then run:

```bash
docker run --rm -it \
  -p 3000:3000 \
  -p 8000:8000 \
  -p 8931:8931 \
  -e DISPLAY=$DISPLAY \
  -e JOB_APPLIER_LINKEDIN_EMAIL="you@example.com" \
  -e JOB_APPLIER_LINKEDIN_PASSWORD="your-linkedin-password" \
  -e JOB_APPLIER_PLAYWRIGHT_HEADLESS=false \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v job-applier-data:/data \
  --ipc=host \
  job-applier
```

The container persists the reusable LinkedIn session at `/data/linkedin/storage-state.json`.

## Run with an external MCP server

```bash
docker run --rm -it \
  -p 3000:3000 \
  -p 8000:8000 \
  -e JOB_APPLIER_PLAYWRIGHT_MCP_URL="http://your-mcp-host:8931/mcp" \
  -v job-applier-data:/data \
  job-applier
```

## External database

If the user wants to point the app to another database later:

```bash
docker run --rm -it \
  -p 3000:3000 \
  -p 8000:8000 \
  -e JOB_APPLIER_DATABASE_URL="postgresql+psycopg://user:pass@host/dbname" \
  -v job-applier-data:/data \
  job-applier
```
