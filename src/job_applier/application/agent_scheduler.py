"""Scheduler loop for periodic agent executions."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from job_applier.application.agent_execution import ExecutionRunSummary
from job_applier.application.panel import PanelSettingsDocument
from job_applier.domain.enums import AgentExecutionStatus, DebugExecutionStage, ExecutionOrigin
from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore

logger = logging.getLogger(__name__)


class ExecutionRunner(Protocol):
    """Minimal protocol required by the scheduler."""

    async def run_execution(
        self,
        *,
        origin: ExecutionOrigin,
        stage: DebugExecutionStage | None = None,
    ) -> ExecutionRunSummary:
        """Run one agent execution."""

    def list_recent_executions(self, *, limit: int = 10) -> list[ExecutionRunSummary]:
        """Return recent executions for overlap inspection."""


class AgentScheduler:
    """Simple resilient scheduler that polls the persisted panel settings."""

    def __init__(
        self,
        *,
        panel_store: LocalPanelSettingsStore,
        orchestrator: ExecutionRunner,
        poll_interval_seconds: int = 30,
    ) -> None:
        self._panel_store = panel_store
        self._orchestrator = orchestrator
        self._poll_interval_seconds = poll_interval_seconds
        self._loop_task: asyncio.Task[None] | None = None
        self._run_lock = asyncio.Lock()
        self._last_schedule_token: str | None = None

    async def start(self) -> None:
        """Start the background scheduler loop if it is not already running."""

        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self._run_loop(), name="agent-scheduler-loop")

    async def stop(self) -> None:
        """Stop the background scheduler loop."""

        if self._loop_task is None:
            return
        self._loop_task.cancel()
        try:
            await self._loop_task
        except asyncio.CancelledError:
            pass
        self._loop_task = None

    async def trigger_now(
        self,
        *,
        stage: DebugExecutionStage | None = None,
    ) -> ExecutionRunSummary:
        """Run the orchestrator manually for debug/testing."""

        if self._run_lock.locked():
            running = self._find_running_execution()
            if running is not None:
                logger.info("agent_scheduler_manual_skip_due_to_running_execution")
                return running
        async with self._run_lock:
            logger.info(
                "agent_scheduler_manual_trigger",
                extra={"stage": stage.value if stage is not None else None},
            )
            return await self._orchestrator.run_execution(
                origin=ExecutionOrigin.MANUAL,
                stage=stage,
            )

    async def tick(self, *, now_utc: datetime | None = None) -> ExecutionRunSummary | None:
        """Check the persisted schedule and run the agent when it is due."""

        document = self._panel_store.load()
        if not self._is_due(document, now_utc=now_utc):
            return None

        schedule_token = self._schedule_token(document, now_utc=now_utc)
        if schedule_token == self._last_schedule_token:
            return None

        if self._run_lock.locked():
            logger.info("agent_scheduler_skip_due_to_running_execution")
            return None

        async with self._run_lock:
            self._last_schedule_token = schedule_token
            logger.info("agent_scheduler_due_trigger", extra={"schedule_token": schedule_token})
            return await self._orchestrator.run_execution(origin=ExecutionOrigin.SCHEDULED)

    async def _run_loop(self) -> None:
        """Keep polling without crashing on execution errors."""

        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("agent_scheduler_tick_failed")
            await asyncio.sleep(self._poll_interval_seconds)

    def _is_due(self, document: PanelSettingsDocument, *, now_utc: datetime | None) -> bool:
        """Return whether the configured schedule should fire right now."""

        local_now = self._local_now(document, now_utc=now_utc)
        hour_text, minute_text = document.schedule.run_at.split(":", maxsplit=1)
        return local_now.hour == int(hour_text) and local_now.minute == int(minute_text)

    def _schedule_token(self, document: PanelSettingsDocument, *, now_utc: datetime | None) -> str:
        """Return a stable token for the current scheduled slot."""

        local_now = self._local_now(document, now_utc=now_utc)
        return local_now.strftime("%Y-%m-%dT%H:%M")

    def _local_now(self, document: PanelSettingsDocument, *, now_utc: datetime | None) -> datetime:
        """Convert the current UTC time into the configured schedule timezone."""

        effective_now = now_utc or datetime.now().astimezone()
        timezone = ZoneInfo(document.schedule.timezone)
        return effective_now.astimezone(timezone)

    def _find_running_execution(self) -> ExecutionRunSummary | None:
        """Return the most recent running execution when one exists."""

        for summary in self._orchestrator.list_recent_executions(limit=5):
            if summary.status is AgentExecutionStatus.RUNNING:
                return summary
        return None
