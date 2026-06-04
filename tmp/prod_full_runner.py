from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from job_applier.application.panel import PanelSettingsDocument
    from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore

RUN_TOKEN = datetime.now().strftime("%Y%m%dT%H%M%S")

os.environ.setdefault("UV_CACHE_DIR", "/tmp/uv-cache")
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/home/thiago/.cache/ms-playwright",
)
os.environ.setdefault(
    "JOB_APPLIER_OUTPUT_DIR",
    f"/tmp/job-applier-prod-full-focused-backend-rerun-{RUN_TOKEN}",
)
os.environ.setdefault("JOB_APPLIER_AGENT_MAX_SELECTED_JOBS_PER_RUN", "5")
os.environ.setdefault("JOB_APPLIER_PLAYWRIGHT_HEADLESS", "true")
os.environ.setdefault("JOB_APPLIER_RESUME_DYNAMIC_ENABLED", "true")
os.environ.setdefault("JOB_APPLIER_LINKEDIN_MAX_SEARCH_PAGES", "3")


class FocusedPanelStore:
    def __init__(self, wrapped: LocalPanelSettingsStore) -> None:
        self._wrapped = wrapped

    def load(self) -> PanelSettingsDocument:
        state = self._wrapped.load()
        preferences = state.preferences.model_copy(
            update={"keywords": ("Backend Developer", "Desenvolvedor Backend")}
        )
        return state.model_copy(update={"preferences": preferences})


async def main() -> None:
    from job_applier.application.agent_execution import AgentExecutionOrchestrator
    from job_applier.domain.enums import DebugExecutionStage, ExecutionOrigin
    from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore
    from job_applier.interface.http.dependencies import (
        get_execution_store,
        get_job_fetcher,
        get_job_scorer,
        get_job_submitter,
        get_panel_settings_store,
        get_submission_repository,
        get_successful_submission_store,
    )
    from job_applier.settings import get_runtime_settings

    runtime = get_runtime_settings()
    orchestrator = AgentExecutionOrchestrator(
        panel_store=cast(
            LocalPanelSettingsStore,
            FocusedPanelStore(get_panel_settings_store()),
        ),
        execution_store=get_execution_store(),
        successful_submission_store=get_successful_submission_store(),
        submission_repository=get_submission_repository(),
        job_fetcher=get_job_fetcher(),
        job_scorer=get_job_scorer(),
        job_submitter=get_job_submitter(),
        output_dir=runtime.output_dir,
        max_selected_jobs_per_run=runtime.resolved_agent_max_selected_jobs_per_run,
        test_minimum_score_threshold=runtime.resolved_agent_test_minimum_score_threshold,
        failed_submission_retry_limit=runtime.resolved_agent_failed_submission_retry_limit,
        debug_stage=DebugExecutionStage.FULL,
        debug_max_jobs=runtime.resolved_agent_debug_max_jobs,
    )
    result = await orchestrator.run_execution(
        origin=ExecutionOrigin.SCHEDULED,
        stage=DebugExecutionStage.FULL,
    )
    print(
        json.dumps(
            {
                "execution_id": str(result.execution_id),
                "status": result.status.value,
                "jobs_seen": result.jobs_seen,
                "jobs_selected": result.jobs_selected,
                "successful_submissions": result.successful_submissions,
                "error_count": result.error_count,
                "last_error": result.last_error,
                "output_dir": runtime.output_dir.as_posix(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
