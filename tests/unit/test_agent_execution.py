import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from job_applier.application.agent_execution import (
    AgentExecutionOrchestrator,
    ExecutionRunSummary,
    JobFetcher,
    JobScorer,
    JobSubmitter,
    ScoredJobPosting,
)
from job_applier.application.agent_scheduler import AgentScheduler
from job_applier.application.panel import (
    AIFormInput,
    PreferencesFormInput,
    ProfileFormInput,
    ScheduleFormInput,
)
from job_applier.domain import (
    AgentExecutionStatus,
    ApplicationSubmission,
    ExecutionEventType,
    ExecutionOrigin,
    JobPosting,
    Platform,
    ScheduleFrequency,
    SeniorityLevel,
    WorkplaceType,
)
from job_applier.infrastructure import (
    InMemorySuccessfulSubmissionStore,
    LocalExecutionStore,
    LocalPanelSettingsStore,
)


def test_orchestrator_records_events_and_continues_after_submit_error(tmp_path: Path) -> None:
    panel_store = build_ready_panel_store(tmp_path / "panel")
    execution_store = LocalExecutionStore(root_dir=tmp_path / "executions")
    submission_store = InMemorySuccessfulSubmissionStore()

    class FakeFetcher(JobFetcher):
        async def fetch(self, settings):  # type: ignore[no-untyped-def]
            return [
                JobPosting(
                    platform=Platform.LINKEDIN,
                    url="https://www.linkedin.com/jobs/view/1",
                    title="Python Engineer",
                    company_name="Acme",
                    description_raw="Build workflow automation.",
                ),
                JobPosting(
                    platform=Platform.LINKEDIN,
                    url="https://www.linkedin.com/jobs/view/2",
                    title="Automation Engineer",
                    company_name="Beta",
                    description_raw="Own the job application pipeline.",
                ),
            ]

    class FakeScorer(JobScorer):
        async def score(self, settings, posting):  # type: ignore[no-untyped-def]
            return ScoredJobPosting(posting=posting, selected=True, score=0.9)

    class FakeSubmitter(JobSubmitter):
        def __init__(self) -> None:
            self.calls = 0

        async def submit(self, settings, posting, *, origin):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 2:
                msg = "submit failure on second posting"
                raise RuntimeError(msg)
            return ApplicationSubmission(job_posting_id=posting.id, execution_origin=origin)

    orchestrator = AgentExecutionOrchestrator(
        panel_store=panel_store,
        execution_store=execution_store,
        successful_submission_store=submission_store,
        job_fetcher=FakeFetcher(),
        job_scorer=FakeScorer(),
        job_submitter=FakeSubmitter(),
    )

    summary = asyncio.run(orchestrator.run_execution(origin=ExecutionOrigin.MANUAL))
    events = execution_store.list_events(summary.execution_id)

    assert summary.status is AgentExecutionStatus.COMPLETED
    assert summary.snapshot_id is not None
    assert summary.jobs_seen == 2
    assert summary.jobs_selected == 2
    assert summary.successful_submissions == 1
    assert summary.error_count == 1
    assert [event["event_type"] for event in events] == [
        ExecutionEventType.EXECUTION_STARTED.value,
        ExecutionEventType.STEP_REACHED.value,
        ExecutionEventType.SUBMISSION_COMPLETED.value,
        ExecutionEventType.EXECUTION_FAILED.value,
        ExecutionEventType.EXECUTION_COMPLETED.value,
    ]


def test_scheduler_runs_once_per_slot_and_supports_manual_trigger(tmp_path: Path) -> None:
    panel_store = LocalPanelSettingsStore(root_dir=tmp_path / "panel")
    panel_store.save_schedule(
        ScheduleFormInput(
            frequency=ScheduleFrequency.DAILY,
            run_at="23:00",
            timezone="UTC",
        ),
    )

    class FakeOrchestrator:
        def __init__(self) -> None:
            self.origins: list[ExecutionOrigin] = []

        async def run_execution(self, *, origin: ExecutionOrigin) -> ExecutionRunSummary:
            self.origins.append(origin)
            return ExecutionRunSummary(
                execution_id=uuid4(),
                origin=origin,
                status=AgentExecutionStatus.COMPLETED,
                started_at=datetime(2026, 3, 28, 23, 0, tzinfo=UTC),
                finished_at=datetime(2026, 3, 28, 23, 1, tzinfo=UTC),
            )

    fake_orchestrator = FakeOrchestrator()
    scheduler = AgentScheduler(
        panel_store=panel_store,
        orchestrator=fake_orchestrator,
        poll_interval_seconds=1,
    )

    async def exercise() -> tuple[
        ExecutionRunSummary | None,
        ExecutionRunSummary | None,
        ExecutionRunSummary,
    ]:
        first = await scheduler.tick(now_utc=datetime(2026, 3, 28, 23, 0, tzinfo=UTC))
        second = await scheduler.tick(now_utc=datetime(2026, 3, 28, 23, 0, tzinfo=UTC))
        manual = await scheduler.trigger_now()
        return first, second, manual

    first, second, manual = asyncio.run(exercise())

    assert first is not None
    assert second is None
    assert manual.origin is ExecutionOrigin.MANUAL
    assert fake_orchestrator.origins == [ExecutionOrigin.SCHEDULED, ExecutionOrigin.MANUAL]


def build_ready_panel_store(root_dir: Path) -> LocalPanelSettingsStore:
    store = LocalPanelSettingsStore(root_dir=root_dir)
    store.save_profile(
        ProfileFormInput.model_validate(
            {
                "name": "Thiago Martins",
                "email": "thiago@example.com",
                "phone": "+5511999999999",
                "city": "Sao Paulo",
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
