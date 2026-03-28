"""API routes for scheduler controls and execution history."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from job_applier.application.agent_execution import AgentExecutionOrchestrator
from job_applier.application.agent_scheduler import AgentScheduler
from job_applier.infrastructure.local_execution_store import LocalExecutionStore
from job_applier.interface.http.dependencies import (
    get_agent_orchestrator,
    get_agent_scheduler,
    get_execution_store,
)

api_router = APIRouter(prefix="/api/agent", tags=["agent-api"])


@api_router.get("/executions")
async def list_executions(
    orchestrator: Annotated[AgentExecutionOrchestrator, Depends(get_agent_orchestrator)],
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> JSONResponse:
    """Return recent execution summaries for debugging and panel display."""

    executions = [
        item.model_dump(mode="json") for item in orchestrator.list_recent_executions(limit=limit)
    ]
    return JSONResponse(content={"executions": executions})


@api_router.get("/executions/{execution_id}/events")
async def list_execution_events(
    execution_id: UUID,
    store: Annotated[LocalExecutionStore, Depends(get_execution_store)],
) -> JSONResponse:
    """Return recorded events for a single execution."""

    return JSONResponse(content={"events": store.list_events(execution_id)})


@api_router.post("/run")
async def run_agent_now(
    scheduler: Annotated[AgentScheduler, Depends(get_agent_scheduler)],
) -> JSONResponse:
    """Trigger one manual execution for debug/testing."""

    summary = await scheduler.trigger_now()
    return JSONResponse(content={"execution": summary.model_dump(mode="json")})
