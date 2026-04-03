import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from job_applier.application.agent_execution import (
    AgentExecutionOrchestrator,
    ExecutionRunSummary,
    JobFetcher,
    JobScorer,
    JobSubmitter,
    ScoredJobPosting,
    SubmissionAttempt,
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
    SubmissionStatus,
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
        async def fetch(self, settings):
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
        async def score(self, settings, posting):
            return ScoredJobPosting(posting=posting, selected=True, score=0.9)

    class FakeSubmitter(JobSubmitter):
        def __init__(self) -> None:
            self.calls = 0

        async def submit(self, settings, posting, *, execution_id, origin):
            del execution_id
            self.calls += 1
            if self.calls == 2:
                msg = "submit failure on second posting"
                raise RuntimeError(msg)
            return SubmissionAttempt(
                submission=ApplicationSubmission(
                    job_posting_id=posting.id,
                    execution_origin=origin,
                ),
            )

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
        ExecutionEventType.JOB_PROCESSED.value,
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


def test_orchestrator_only_submits_jobs_that_pass_scoring(tmp_path: Path) -> None:
    panel_store = build_ready_panel_store(tmp_path / "panel")
    execution_store = LocalExecutionStore(root_dir=tmp_path / "executions")
    submission_store = InMemorySuccessfulSubmissionStore()

    rejected_posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/rejected",
        title="Operations Coordinator",
        company_name="Acme",
        description_raw="Administrative routines and coordination.",
    )
    accepted_posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/accepted",
        title="Senior Python Automation Engineer",
        company_name="Beta",
        description_raw="Python automation with FastAPI.",
    )

    class FakeFetcher(JobFetcher):
        async def fetch(self, settings):
            del settings
            return [rejected_posting, accepted_posting]

    class FakeScorer(JobScorer):
        async def score(self, settings, posting):
            del settings
            if posting.id == rejected_posting.id:
                return ScoredJobPosting(
                    posting=posting,
                    selected=False,
                    score=0.2,
                    reason="Rejected with score 0.20 < 0.55",
                )
            return ScoredJobPosting(posting=posting, selected=True, score=0.91, reason="accepted")

    class FakeSubmitter(JobSubmitter):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def submit(self, settings, posting, *, execution_id, origin):
            del settings, execution_id
            self.calls.append(posting.title)
            return SubmissionAttempt(
                submission=ApplicationSubmission(
                    job_posting_id=posting.id,
                    execution_origin=origin,
                ),
            )

    submitter = FakeSubmitter()
    orchestrator = AgentExecutionOrchestrator(
        panel_store=panel_store,
        execution_store=execution_store,
        successful_submission_store=submission_store,
        job_fetcher=FakeFetcher(),
        job_scorer=FakeScorer(),
        job_submitter=submitter,
    )

    summary = asyncio.run(orchestrator.run_execution(origin=ExecutionOrigin.MANUAL))
    events = execution_store.list_events(summary.execution_id)

    assert summary.status is AgentExecutionStatus.COMPLETED
    assert summary.jobs_seen == 2
    assert summary.jobs_selected == 1
    assert summary.successful_submissions == 1
    assert submitter.calls == ["Senior Python Automation Engineer"]
    assert any(
        event["event_type"] == ExecutionEventType.STEP_REACHED.value
        and event["payload_json"].find("score_rejected") != -1
        for event in events
    )


def test_orchestrator_test_limit_stops_after_first_selected_job(tmp_path: Path) -> None:
    panel_store = build_ready_panel_store(tmp_path / "panel")
    execution_store = LocalExecutionStore(root_dir=tmp_path / "executions")
    submission_store = InMemorySuccessfulSubmissionStore()

    postings = [
        JobPosting(
            platform=Platform.LINKEDIN,
            url=f"https://www.linkedin.com/jobs/view/{index}",
            title=f"Python Automation Engineer {index}",
            company_name=f"Company {index}",
            description_raw="Python automation with FastAPI.",
        )
        for index in range(1, 4)
    ]

    class FakeFetcher(JobFetcher):
        async def fetch(self, settings):
            del settings
            return postings

    class FakeScorer(JobScorer):
        async def score(self, settings, posting):
            del settings
            return ScoredJobPosting(posting=posting, selected=True, score=0.91, reason="accepted")

    class FakeSubmitter(JobSubmitter):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def submit(self, settings, posting, *, execution_id, origin):
            del settings, execution_id
            self.calls.append(posting.title)
            return SubmissionAttempt(
                submission=ApplicationSubmission(
                    job_posting_id=posting.id,
                    execution_origin=origin,
                ),
            )

    submitter = FakeSubmitter()
    orchestrator = AgentExecutionOrchestrator(
        panel_store=panel_store,
        execution_store=execution_store,
        successful_submission_store=submission_store,
        job_fetcher=FakeFetcher(),
        job_scorer=FakeScorer(),
        job_submitter=submitter,
        max_selected_jobs_per_run=1,
    )

    summary = asyncio.run(orchestrator.run_execution(origin=ExecutionOrigin.MANUAL))
    events = execution_store.list_events(summary.execution_id)

    assert summary.status is AgentExecutionStatus.COMPLETED
    assert summary.jobs_seen == 3
    assert summary.jobs_selected == 1
    assert summary.successful_submissions == 1
    assert submitter.calls == ["Python Automation Engineer 1"]
    assert any(
        event["event_type"] == ExecutionEventType.STEP_REACHED.value
        and "selected_job_limit_reached" in event["payload_json"]
        for event in events
    )


def test_orchestrator_can_override_score_threshold_in_test_mode(tmp_path: Path) -> None:
    panel_store = build_ready_panel_store(tmp_path / "panel")
    execution_store = LocalExecutionStore(root_dir=tmp_path / "executions")
    submission_store = InMemorySuccessfulSubmissionStore()

    posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/test-threshold",
        title="Automation Engineer",
        company_name="Acme",
        description_raw="Python automation with FastAPI.",
    )

    class FakeFetcher(JobFetcher):
        async def fetch(self, settings):
            del settings
            return [posting]

    class ThresholdAwareScorer(JobScorer):
        async def score(self, settings, posting):
            selected = settings.search.minimum_score_threshold <= 0.5
            score = 0.5
            reason = (
                "accepted in test mode override" if selected else "Rejected with score 0.50 < 0.55"
            )
            return ScoredJobPosting(
                posting=posting,
                selected=selected,
                score=score,
                reason=reason,
            )

    class FakeSubmitter(JobSubmitter):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def submit(self, settings, posting, *, execution_id, origin):
            del settings, execution_id
            self.calls.append(posting.title)
            return SubmissionAttempt(
                submission=ApplicationSubmission(
                    job_posting_id=posting.id,
                    execution_origin=origin,
                ),
            )

    submitter = FakeSubmitter()
    orchestrator = AgentExecutionOrchestrator(
        panel_store=panel_store,
        execution_store=execution_store,
        successful_submission_store=submission_store,
        job_fetcher=FakeFetcher(),
        job_scorer=ThresholdAwareScorer(),
        job_submitter=submitter,
        test_minimum_score_threshold=0.5,
    )

    summary = asyncio.run(orchestrator.run_execution(origin=ExecutionOrigin.MANUAL))
    assert summary.status is AgentExecutionStatus.COMPLETED
    assert summary.jobs_seen == 1
    assert summary.jobs_selected == 1
    assert summary.successful_submissions == 1
    assert submitter.calls == ["Automation Engineer"]


def test_orchestrator_skips_already_applied_jobs_before_submit(tmp_path: Path) -> None:
    panel_store = build_ready_panel_store(tmp_path / "panel")
    execution_store = LocalExecutionStore(root_dir=tmp_path / "executions")
    submission_store = InMemorySuccessfulSubmissionStore()

    posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/already-applied",
        title="Senior Python Automation Engineer",
        company_name="Acme",
        description_raw="Python automation with FastAPI.",
    )
    existing_submission = ApplicationSubmission(
        id=uuid4(),
        job_posting_id=posting.id,
        status=SubmissionStatus.SUBMITTED,
        started_at=datetime(2026, 3, 28, 22, 55, tzinfo=UTC),
        submitted_at=datetime(2026, 3, 28, 23, 0, tzinfo=UTC),
        profile_snapshot_id=uuid4(),
        ruleset_version="ruleset-v1",
        execution_origin=ExecutionOrigin.MANUAL,
        notes="Already applied successfully.",
    )

    class FakeFetcher(JobFetcher):
        async def fetch(self, settings):
            del settings
            return [posting]

    class FakeScorer(JobScorer):
        async def score(self, settings, posting):
            del settings
            return ScoredJobPosting(posting=posting, selected=True, score=0.95, reason="accepted")

    class FakeSubmitter(JobSubmitter):
        def __init__(self) -> None:
            self.calls = 0

        async def submit(self, settings, posting, *, execution_id, origin):
            del settings, posting, execution_id, origin
            self.calls += 1
            raise AssertionError("submit should not be called for already-applied jobs")

    class FakeSubmissionRepository:
        def find_latest_successful_for_job_posting(self, job_posting_id):
            assert job_posting_id == posting.id
            return existing_submission

        def save(self, entity):
            return entity

        def get(self, entity_id):
            del entity_id
            return None

        def list(self, *, limit=100, offset=0):
            del limit, offset
            return []

        def delete(self, entity_id):
            del entity_id

        def list_by_submitted_at(
            self,
            *,
            submitted_from=None,
            submitted_to=None,
            limit=100,
            offset=0,
        ):
            del submitted_from, submitted_to, limit, offset
            return []

    submitter = FakeSubmitter()
    orchestrator = AgentExecutionOrchestrator(
        panel_store=panel_store,
        execution_store=execution_store,
        successful_submission_store=submission_store,
        submission_repository=FakeSubmissionRepository(),
        job_fetcher=FakeFetcher(),
        job_scorer=FakeScorer(),
        job_submitter=submitter,
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
        and '"reason": "already_applied"' in event["payload_json"]
        for event in events
    )
    assert any(
        event["event_type"] == ExecutionEventType.JOB_PROCESSED.value
        and '"status": "skipped"' in event["payload_json"]
        for event in events
    )


def test_orchestrator_persists_running_summary_updates_during_execution(tmp_path: Path) -> None:
    panel_store = build_ready_panel_store(tmp_path / "panel")
    execution_store = LocalExecutionStore(root_dir=tmp_path / "executions")
    submission_store = InMemorySuccessfulSubmissionStore()

    postings = [
        JobPosting(
            platform=Platform.LINKEDIN,
            url="https://www.linkedin.com/jobs/view/1",
            title="Python Engineer",
            company_name="Acme",
            description_raw="Python automation.",
        ),
        JobPosting(
            platform=Platform.LINKEDIN,
            url="https://www.linkedin.com/jobs/view/2",
            title="Automation Engineer",
            company_name="Beta",
            description_raw="Workflow automation.",
        ),
    ]

    class FakeFetcher(JobFetcher):
        async def fetch(self, settings):
            del settings
            return postings

    class FakeScorer(JobScorer):
        async def score(self, settings, posting):
            del settings
            return ScoredJobPosting(posting=posting, selected=True, score=0.91, reason="accepted")

    class FakeSubmitter(JobSubmitter):
        def __init__(self) -> None:
            self.calls = 0

        async def submit(self, settings, posting, *, execution_id, origin):
            del settings, posting, execution_id
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("submit failure on second posting")
            return SubmissionAttempt(
                submission=ApplicationSubmission(
                    job_posting_id=postings[0].id,
                    execution_origin=origin,
                ),
            )

    class RecordingOrchestrator(AgentExecutionOrchestrator):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.persisted_summaries: list[ExecutionRunSummary] = []

        def _persist_run_summary(self, summary: ExecutionRunSummary) -> None:
            self.persisted_summaries.append(summary)

    orchestrator = RecordingOrchestrator(
        panel_store=panel_store,
        execution_store=execution_store,
        successful_submission_store=submission_store,
        job_fetcher=FakeFetcher(),
        job_scorer=FakeScorer(),
        job_submitter=FakeSubmitter(),
    )

    summary = asyncio.run(orchestrator.run_execution(origin=ExecutionOrigin.MANUAL))

    assert summary.status is AgentExecutionStatus.COMPLETED
    assert any(
        persisted.status is AgentExecutionStatus.RUNNING
        and persisted.jobs_selected == 1
        and persisted.successful_submissions == 0
        and persisted.error_count == 0
        for persisted in orchestrator.persisted_summaries
    )
    assert any(
        persisted.status is AgentExecutionStatus.RUNNING
        and persisted.jobs_selected == 1
        and persisted.successful_submissions == 1
        and persisted.error_count == 0
        for persisted in orchestrator.persisted_summaries
    )
    assert any(
        persisted.status is AgentExecutionStatus.RUNNING
        and persisted.jobs_selected == 2
        and persisted.successful_submissions == 1
        and persisted.error_count == 1
        and persisted.last_error == "submit failure on second posting"
        for persisted in orchestrator.persisted_summaries
    )


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
