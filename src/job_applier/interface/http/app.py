"""FastAPI application factory used by the bootstrap project."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from job_applier.interface.http.routes.panel import api_router as panel_api_router


def create_app() -> FastAPI:
    """Create the FastAPI app used by the project."""

    app = FastAPI(title="Job Applier", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:3000",
            "http://localhost:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["health"])
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(panel_api_router)

    return app
