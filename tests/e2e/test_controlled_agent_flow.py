from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Generator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from job_applier.application.agent_execution import AgentExecutionOrchestrator
from job_applier.application.agent_scheduler import AgentScheduler
from job_applier.application.job_scoring import RuleBasedJobScorer
from job_applier.domain import (
    AnswerSource,
    ApplicationAnswer,
    ArtifactSnapshot,
    ArtifactType,
    ExecutionEvent,
    ExecutionEventType,
    FillStrategy,
    JobPosting,
    Platform,
    QuestionType,
    RecruiterAction,
    RecruiterInteraction,
    RecruiterInteractionStatus,
    SeniorityLevel,
    SubmissionStatus,
    WorkplaceType,
)
from job_applier.infrastructure import (
    InMemorySuccessfulSubmissionStore,
    LocalPanelSettingsStore,
    MirroredExecutionStore,
)
from job_applier.infrastructure.linkedin.easy_apply import (
    EasyApplyExecutionResult,
    LinkedInEasyApplySubmitter,
)
from job_applier.infrastructure.sqlite import create_session_factory
from job_applier.infrastructure.sqlite.repositories import (
    SqliteAnswerRepository,
    SqliteArtifactSnapshotRepository,
    SqliteExecutionEventRepository,
    SqliteJobPostingRepository,
    SqliteProfileSnapshotRepository,
    SqliteRecruiterInteractionRepository,
    SqliteSubmissionHistoryRepository,
    SqliteSubmissionRepository,
)
from job_applier.interface.http.dependencies import (
    get_agent_orchestrator,
    get_agent_scheduler,
    get_execution_store,
    get_panel_settings_store,
    get_submission_history_repository,
)
from job_applier.main import app

TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT) not in sys.path:
    sys.path.append(str(TESTS_ROOT))

sqlite_helpers = importlib.import_module("integration.sqlite_helpers")
upgrade_to_head = sqlite_helpers.upgrade_to_head


@pytest.fixture
def controlled_runtime(tmp_path: Path) -> Generator[dict[str, object]]:
    database_url = f"sqlite:///{(tmp_path / 'controlled-flow.db').resolve()}"
    upgrade_to_head(database_url)
    session_factory = create_session_factory(database_url)

    panel_store = LocalPanelSettingsStore(root_dir=tmp_path / "panel")
    history_repository = SqliteSubmissionHistoryRepository(session_factory)
    event_repository = SqliteExecutionEventRepository(session_factory)
    execution_store = MirroredExecutionStore(
        event_repository=event_repository,
        root_dir=tmp_path / "executions",
    )

    job_repository = SqliteJobPostingRepository(session_factory)
    submission_repository = SqliteSubmissionRepository(session_factory)
    answer_repository = SqliteAnswerRepository(session_factory)
    snapshot_repository = SqliteProfileSnapshotRepository(session_factory)
    recruiter_repository = SqliteRecruiterInteractionRepository(session_factory)
    artifact_repository = SqliteArtifactSnapshotRepository(session_factory)

    class PersistingFetcher:
        async def fetch(self, settings):
            del settings
            posting = job_repository.save(
                JobPosting(
                    platform=Platform.LINKEDIN,
                    url="https://www.linkedin.com/jobs/view/job-e2e-001",
                    external_job_id="job-e2e-001",
                    title="Senior Python Automation Engineer",
                    company_name="Acme",
                    location="Remote",
                    workplace_type=WorkplaceType.REMOTE,
                    seniority=SeniorityLevel.SENIOR,
                    description_raw="Build Python automation and FastAPI services.",
                    easy_apply=True,
                ),
            )
            return [posting]

    class FakeExecutor:
        def __init__(self) -> None:
            self._artifacts_dir = tmp_path / "fake-artifacts"
            self._artifacts_dir.mkdir(parents=True, exist_ok=True)

        async def execute(self, settings, posting, *, execution_id, origin):
            del origin
            submission_id = uuid4()
            submitted_at = datetime(2026, 3, 29, 23, 0, tzinfo=UTC)

            screenshot_path = self._write_artifact("submission.png", b"screenshot-binary")
            html_path = self._write_artifact("submission.html", b"<html>success</html>")
            cv_path = Path(str(settings.profile.cv_path or "resume.pdf"))
            if not cv_path.exists():
                cv_path.parent.mkdir(parents=True, exist_ok=True)
                cv_path.write_bytes(b"resume-pdf")

            return EasyApplyExecutionResult(
                submission_id=submission_id,
                started_at=datetime(2026, 3, 29, 22, 58, tzinfo=UTC),
                status=SubmissionStatus.SUBMITTED,
                notes="Controlled Easy Apply submission.",
                answers=(
                    ApplicationAnswer(
                        submission_id=submission_id,
                        step_index=0,
                        question_raw="Are you authorized to work in Brazil?",
                        question_type=QuestionType.WORK_AUTHORIZATION,
                        normalized_key="work_authorization",
                        answer_raw="Yes",
                        answer_source=AnswerSource.RULE,
                        fill_strategy=FillStrategy.DETERMINISTIC,
                    ),
                    ApplicationAnswer(
                        submission_id=submission_id,
                        step_index=1,
                        question_raw="Are you willing to relocate?",
                        question_type=QuestionType.YES_NO_GENERIC,
                        normalized_key="are_you_willing_to_relocate",
                        answer_raw="No",
                        answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
                        fill_strategy=FillStrategy.BEST_EFFORT,
                        ambiguity_flag=True,
                    ),
                ),
                execution_events=(
                    ExecutionEvent(
                        execution_id=execution_id,
                        submission_id=submission_id,
                        event_type=ExecutionEventType.SUBMIT_TRIGGERED,
                        payload_json='{"stage":"submit","job_posting_id":"job-e2e-001"}',
                        timestamp=submitted_at,
                    ),
                    ExecutionEvent(
                        execution_id=execution_id,
                        submission_id=submission_id,
                        event_type=ExecutionEventType.AUTOFILL_APPLIED,
                        payload_json='{"normalized_key":"are_you_willing_to_relocate"}',
                        timestamp=submitted_at,
                    ),
                ),
                artifacts=(
                    ArtifactSnapshot(
                        submission_id=submission_id,
                        artifact_type=ArtifactType.SCREENSHOT,
                        path=str(screenshot_path),
                        sha256=sha256(screenshot_path.read_bytes()).hexdigest(),
                        created_at=submitted_at,
                    ),
                    ArtifactSnapshot(
                        submission_id=submission_id,
                        artifact_type=ArtifactType.HTML_DUMP,
                        path=str(html_path),
                        sha256=sha256(html_path.read_bytes()).hexdigest(),
                        created_at=submitted_at,
                    ),
                    ArtifactSnapshot(
                        submission_id=submission_id,
                        artifact_type=ArtifactType.CV_METADATA,
                        path=str(cv_path),
                        sha256=sha256(cv_path.read_bytes()).hexdigest(),
                        created_at=submitted_at,
                    ),
                ),
                recruiter_interactions=(
                    RecruiterInteraction(
                        submission_id=submission_id,
                        recruiter_name="Maria Recruiter",
                        recruiter_profile_url="https://www.linkedin.com/in/maria-recruiter",
                        action=RecruiterAction.CONNECT,
                        status=RecruiterInteractionStatus.SENT,
                        message_sent="Hi Maria, I just applied for the role at Acme.",
                        sent_at=submitted_at,
                    ),
                ),
                submitted_at=submitted_at,
                cv_version=settings.profile.cv_filename or "resume.pdf",
            )

        def _write_artifact(self, name: str, content: bytes) -> Path:
            path = self._artifacts_dir / name
            path.write_bytes(content)
            return path

    submitter = LinkedInEasyApplySubmitter(
        executor=FakeExecutor(),
        submission_repository=submission_repository,
        answer_repository=answer_repository,
        profile_snapshot_repository=snapshot_repository,
        recruiter_repository=recruiter_repository,
        artifact_repository=artifact_repository,
        execution_event_repository=event_repository,
    )
    orchestrator = AgentExecutionOrchestrator(
        panel_store=panel_store,
        execution_store=execution_store,
        successful_submission_store=InMemorySuccessfulSubmissionStore(),
        job_fetcher=PersistingFetcher(),
        job_scorer=RuleBasedJobScorer(),
        job_submitter=submitter,
    )
    scheduler = AgentScheduler(
        panel_store=panel_store,
        orchestrator=orchestrator,
        poll_interval_seconds=1,
    )

    async def override_panel_store() -> LocalPanelSettingsStore:
        return panel_store

    async def override_execution_store() -> MirroredExecutionStore:
        return execution_store

    async def override_history_repository() -> SqliteSubmissionHistoryRepository:
        return history_repository

    async def override_orchestrator() -> AgentExecutionOrchestrator:
        return orchestrator

    async def override_scheduler() -> AgentScheduler:
        return scheduler

    app.dependency_overrides[get_panel_settings_store] = override_panel_store
    app.dependency_overrides[get_execution_store] = override_execution_store
    app.dependency_overrides[get_submission_history_repository] = override_history_repository
    app.dependency_overrides[get_agent_orchestrator] = override_orchestrator
    app.dependency_overrides[get_agent_scheduler] = override_scheduler

    yield {
        "panel_store": panel_store,
        "history_repository": history_repository,
    }

    app.dependency_overrides.clear()


def test_controlled_manual_run_persists_full_successful_flow(
    controlled_runtime: dict[str, object],
) -> None:
    del controlled_runtime

    async def exercise() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            profile_response = await client.post(
                "/api/panel/profile",
                data={
                    "name": "Thiago Martins",
                    "email": "thiago@example.com",
                    "phone": "+5511999999999",
                    "city": "Sao Paulo",
                    "linkedin_url": "https://www.linkedin.com/in/thiago",
                    "github_url": "https://github.com/0xthiagomartins",
                    "portfolio_url": "https://thiago.example.com",
                    "years_experience_by_stack": "python=8\nfastapi=4\nautomation=6",
                    "work_authorized": "true",
                    "availability": "Immediate",
                    "default_responses": "work_authorization=Yes\ncover_letter=Open to discuss.",
                },
                files={"cv_file": ("resume.pdf", b"fake-pdf-content", "application/pdf")},
            )
            assert profile_response.status_code == 200

            preferences_response = await client.put(
                "/api/panel/preferences",
                data={
                    "keywords": "python, automation",
                    "location": "Remote",
                    "posted_within_hours": "24",
                    "workplace_types": ["remote"],
                    "seniority": ["senior"],
                    "easy_apply_only": "true",
                    "minimum_score_threshold": "0.55",
                    "positive_keywords": "fastapi, automation",
                    "negative_keywords": "internship",
                    "auto_connect_with_recruiter": "false",
                },
            )
            assert preferences_response.status_code == 200

            schedule_response = await client.put(
                "/api/panel/schedule",
                data={
                    "frequency": "daily",
                    "run_at": "23:00",
                    "timezone": "America/Sao_Paulo",
                },
            )
            assert schedule_response.status_code == 200

            ai_response = await client.put(
                "/api/panel/ai",
                data={"api_key": "sk-test-12345", "model": "o3-mini"},
            )
            assert ai_response.status_code == 200

            run_response = await client.post("/api/agent/run")
            execution_payload = cast(dict[str, Any], run_response.json())

            execution_id = execution_payload["execution"]["execution_id"]
            executions_response = await client.get("/api/agent/executions")
            events_response = await client.get(f"/api/agent/executions/{execution_id}/events")
            history_response = await client.get("/api/applications")
            history_payload = cast(dict[str, Any], history_response.json())
            submission_id = str(history_payload["items"][0]["id"])
            detail_response = await client.get(f"/api/applications/{submission_id}")

            return (
                execution_payload,
                cast(dict[str, Any], executions_response.json()),
                cast(dict[str, Any], events_response.json()),
                cast(dict[str, Any], detail_response.json()),
            )

    run_payload, executions_payload, events_payload, detail_payload = asyncio.run(exercise())

    execution = run_payload["execution"]
    assert execution["origin"] == "manual"
    assert execution["status"] == "completed"
    assert execution["jobs_seen"] == 1
    assert execution["jobs_selected"] == 1
    assert execution["successful_submissions"] == 1

    assert executions_payload["executions"][0]["execution_id"] == execution["execution_id"]

    event_types = {event["event_type"] for event in events_payload["events"]}
    assert {
        "execution_started",
        "step_reached",
        "submission_completed",
        "job_processed",
        "execution_completed",
    }.issubset(event_types)

    application = detail_payload["application"]
    assert application["job_posting"]["external_job_id"] == "job-e2e-001"
    assert application["job_posting"]["title"] == "Senior Python Automation Engineer"
    assert len(application["answers"]) == 2
    assert application["profile_snapshot"]["data"]["profile"]["name"] == "Thiago Martins"
    assert len(application["recruiter_interactions"]) == 1
    assert application["recruiter_interactions"][0]["status"] == "sent"

    artifact_types = {artifact["artifact_type"] for artifact in application["artifacts"]}
    assert {"screenshot", "html_dump", "cv_metadata"}.issubset(artifact_types)

    history_event_types = {event["event_type"] for event in application["execution_events"]}
    assert {"submit_triggered", "autofill_applied", "submission_completed"}.issubset(
        history_event_types,
    )
