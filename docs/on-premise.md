# On-premise runtime

The product is designed to run close to the user.

Why this matters:

- the panel and API stay inside the same app container;
- if no external database URL is provided, the app falls back to a local SQLite file;
- Playwright can open a visible browser on the host so the user can follow the automation and step in for captchas.

## Default runtime behavior

- panel: port `3000`
- backend API: port `8000`
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
  -v job-applier-data:/data \
  job-applier
```

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
