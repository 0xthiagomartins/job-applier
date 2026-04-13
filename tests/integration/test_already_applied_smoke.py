from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from job_applier.application.agent_execution import (
    AgentExecutionOrchestrator,
    JobFetcher,
    JobScorer,
    JobSubmitter,
    ScoredJobPosting,
)
from job_applier.application.panel import (
    AIFormInput,
    PreferencesFormInput,
    ProfileFormInput,
    ScheduleFormInput,
)
from job_applier.domain import (
    AgentExecutionStatus,
    ExecutionEventType,
    ExecutionOrigin,
    JobPosting,
    ScheduleFrequency,
    SeniorityLevel,
    SubmissionStatus,
    WorkplaceType,
)
from job_applier.infrastructure import (
    InMemorySuccessfulSubmissionStore,
    LocalPanelSettingsStore,
    MirroredExecutionStore,
)
from job_applier.infrastructure.sqlite import create_session_factory
from job_applier.infrastructure.sqlite.repositories import (
    SqliteExecutionEventRepository,
    SqliteJobPostingRepository,
    SqliteProfileSnapshotRepository,
    SqliteSubmissionRepository,
)
from tests.integration.sqlite_helpers import (
    build_posting,
    build_snapshot,
    build_submission,
    upgrade_to_head,
    utc_dt,
)


def test_already_applied_guard_smoke_skips_submit_for_previously_submitted_job(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'already-applied-smoke.db').resolve()}"
    upgrade_to_head(database_url)
    session_factory = create_session_factory(database_url)

    panel_store = _build_ready_panel_store(tmp_path / "panel")
    execution_store = MirroredExecutionStore(
        event_repository=SqliteExecutionEventRepository(session_factory),
        root_dir=tmp_path / "executions",
    )
    posting_repository = SqliteJobPostingRepository(session_factory)
    snapshot_repository = SqliteProfileSnapshotRepository(session_factory)
    submission_repository = SqliteSubmissionRepository(session_factory)

    posting = posting_repository.save(
        build_posting(
            company_name="Acme",
            title="Senior Python Automation Engineer",
            external_job_id="job-already-applied-smoke",
            captured_at=utc_dt(28, 10),
        ),
    )
    snapshot = snapshot_repository.save(build_snapshot(created_at=utc_dt(28, 10)))
    existing_submission = submission_repository.save(
        build_submission(
            job_posting_id=posting.id,
            profile_snapshot_id=snapshot.id,
            submitted_at=utc_dt(28, 11),
            note_suffix=" smoke",
        ),
    )

    class FakeFetcher(JobFetcher):
        async def fetch(self, settings: Any) -> list[JobPosting]:
            del settings
            return [posting]

    class FakeScorer(JobScorer):
        async def score(self, settings: Any, current_posting: JobPosting) -> ScoredJobPosting:
            del settings
            return ScoredJobPosting(
                posting=current_posting,
                selected=True,
                score=0.99,
                reason="Smoke validation forces the submit gate to evaluate.",
            )

    class ExplodingSubmitter(JobSubmitter):
        def __init__(self) -> None:
            self.calls = 0

        async def submit(self, settings, posting, *, execution_id, origin):
            del settings, posting, execution_id, origin
            self.calls += 1
            raise AssertionError("submitter should not be called for already-applied jobs")

    submitter = ExplodingSubmitter()
    orchestrator = AgentExecutionOrchestrator(
        panel_store=panel_store,
        execution_store=execution_store,
        successful_submission_store=InMemorySuccessfulSubmissionStore(),
        submission_repository=submission_repository,
        job_fetcher=FakeFetcher(),
        job_scorer=FakeScorer(),
        job_submitter=submitter,
        output_dir=tmp_path / "out",
    )

    summary = asyncio.run(orchestrator.run_execution(origin=ExecutionOrigin.MANUAL))
    events = execution_store.list_events(summary.execution_id)

    assert summary.status is AgentExecutionStatus.COMPLETED
    assert summary.jobs_seen == 1
    assert summary.jobs_selected == 1
    assert summary.successful_submissions == 0
    assert summary.error_count == 0
    assert submitter.calls == 0
    assert any(
        event["event_type"] == ExecutionEventType.STEP_REACHED.value
        and '"stage": "submit_skipped"' in event["payload_json"]
        and '"reason": "already_applied"' in event["payload_json"]
        and str(existing_submission.id) in event["payload_json"]
        for event in events
    )
    assert any(
        event["event_type"] == ExecutionEventType.JOB_PROCESSED.value
        and f'"status": "{SubmissionStatus.SKIPPED.value}"' in event["payload_json"]
        and '"reason": "already_applied"' in event["payload_json"]
        for event in events
    )


def _build_ready_panel_store(root_dir: Path) -> LocalPanelSettingsStore:
    store = LocalPanelSettingsStore(root_dir=root_dir)
    store.save_profile(
        ProfileFormInput.model_validate(
            {
                "name": "Thiago Martins",
                "email": "thiago@example.com",
                "phone": "+5511999999999",
                "city": "Sao Paulo - SP Brasil",
                "linkedin_url": "https://www.linkedin.com/in/thiago",
                "github_url": "https://github.com/0xthiagomartins",
                "portfolio_url": "https://thiago.example.com",
                "years_experience_by_stack": {"python": 8},
                "work_authorized": True,
                "availability": "Immediate",
                "default_responses": {"work_authorization": "Yes"},
            },
        ),
    )
    store.save_preferences(
        PreferencesFormInput(
            keywords=("python", "automation"),
            location="Remote",
            posted_within_hours=24,
            workplace_types=(WorkplaceType.REMOTE,),
            seniority=(SeniorityLevel.SENIOR,),
            easy_apply_only=True,
            minimum_score_threshold=0.55,
            positive_keywords=("fastapi",),
            negative_keywords=("internship",),
        ),
    )
    store.save_schedule(
        ScheduleFormInput(
            frequency=ScheduleFrequency.DAILY,
            run_at="23:00",
            timezone="UTC",
        ),
    )
    store.save_ai(AIFormInput.model_validate({"api_key": "sk-test-12345", "model": "o3-mini"}))
    return store
