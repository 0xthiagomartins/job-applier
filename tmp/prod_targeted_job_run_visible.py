from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from uuid import uuid4

RUN_TOKEN = datetime.now().strftime("%Y%m%dT%H%M%S")

TARGET_JOB_ID = os.environ.get("JOB_APPLIER_TARGET_JOB_ID", "4422249987")

os.environ.setdefault("UV_CACHE_DIR", "/tmp/uv-cache")
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/home/thiago/.cache/ms-playwright",
)
os.environ["JOB_APPLIER_OUTPUT_DIR"] = (
    f"/tmp/job-applier-prod-targeted-visible-{TARGET_JOB_ID}-{RUN_TOKEN}"
)
os.environ["JOB_APPLIER_PLAYWRIGHT_HEADLESS"] = "false"
os.environ["JOB_APPLIER_RESUME_DYNAMIC_ENABLED"] = "true"


async def main() -> None:
    from job_applier.application.agent_execution import build_user_agent_settings
    from job_applier.domain.enums import ExecutionOrigin, Platform
    from job_applier.interface.http.dependencies import (
        get_job_posting_repository,
        get_job_scorer,
        get_job_submitter,
        get_panel_settings_store,
    )
    from job_applier.observability import (
        bind_run_output,
        reset_run_output,
        update_progress_snapshot,
        update_summary_snapshot,
    )
    from job_applier.settings import get_runtime_settings, initialize_runtime_environment

    runtime = get_runtime_settings()
    runtime.playwright_mcp_url = None
    runtime.playwright_headless = False
    initialize_runtime_environment(runtime)
    reset_run_output(
        runtime.output_dir,
        execution_id=TARGET_JOB_ID,
        origin="targeted-visible",
        started_at=datetime.now().astimezone(),
    )
    settings = build_user_agent_settings(get_panel_settings_store().load())
    with bind_run_output(runtime.output_dir):
        posting = get_job_posting_repository().find_by_external_job_id(
            platform=Platform.LINKEDIN.value,
            external_job_id=TARGET_JOB_ID,
        )
        if posting is None:
            raise RuntimeError(f"Posting {TARGET_JOB_ID} not found")
        scored_job = await get_job_scorer().score(settings, posting)
        attempt = await get_job_submitter().submit(
            settings,
            posting,
            scored_job,
            execution_id=uuid4(),
            origin=ExecutionOrigin.SCHEDULED,
        )
        submission_status = attempt.submission.status.value
        submission_notes = attempt.submission.notes
        finished_at = datetime.now().astimezone().isoformat()
        update_progress_snapshot(
            {
                "status": "completed",
                "current_stage": "job_processed",
                "current_step": None,
                "current_job": {
                    "job_posting_id": str(posting.id),
                    "external_job_id": TARGET_JOB_ID,
                    "submission_id": str(attempt.submission.id),
                    "company_name": posting.company_name,
                    "title": posting.title,
                    "url": posting.url,
                    "status": submission_status,
                },
            },
            output_dir=runtime.output_dir,
        )
        update_summary_snapshot(
            {
                "status": "completed",
                "finished_at": finished_at,
                "jobs_seen": 1,
                "jobs_selected": 1,
                "successful_submissions": 1 if submission_status == "submitted" else 0,
                "error_count": 1 if submission_status == "failed" else 0,
                "last_error": submission_notes if submission_status == "failed" else None,
            },
            output_dir=runtime.output_dir,
        )
        print(
            json.dumps(
                {
                    "target_job_id": TARGET_JOB_ID,
                    "submission_id": str(attempt.submission.id),
                    "status": attempt.submission.status.value,
                    "notes": attempt.submission.notes,
                    "output_dir": runtime.output_dir.as_posix(),
                    "score": scored_job.score,
                    "matched_role_target": scored_job.matched_role_target,
                    "matched_specializations": scored_job.matched_specializations,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
