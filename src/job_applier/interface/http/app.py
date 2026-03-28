"""FastAPI application factory used by the bootstrap project."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Create the FastAPI app used by the project."""

    app = FastAPI(title="Job Applier", version="0.1.0")

    @app.get("/health", tags=["health"])
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    return app
