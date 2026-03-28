import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any, cast

from httpx import ASGITransport, AsyncClient

from job_applier.domain import ApplicationSubmission, ExecutionOrigin, SubmissionStatus
from job_applier.infrastructure.sqlite import create_session_factory
from job_applier.infrastructure.sqlite.repositories import (
    SqliteAnswerRepository,
    SqliteArtifactSnapshotRepository,
    SqliteExecutionEventRepository,
    SqliteJobPostingRepository,
    SqliteProfileSnapshotRepository,
    SqliteSubmissionHistoryRepository,
    SqliteSubmissionRepository,
)
from job_applier.interface.http.dependencies import get_submission_history_repository
from job_applier.main import app

TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT) not in sys.path:
    sys.path.append(str(TESTS_ROOT))

sqlite_helpers = importlib.import_module("integration.sqlite_helpers")
build_answer = sqlite_helpers.build_answer
build_artifact = sqlite_helpers.build_artifact
build_execution_event = sqlite_helpers.build_execution_event
build_posting = sqlite_helpers.build_posting
build_snapshot = sqlite_helpers.build_snapshot
build_submission = sqlite_helpers.build_submission
upgrade_to_head = sqlite_helpers.upgrade_to_head
utc_dt = sqlite_helpers.utc_dt


def test_applications_api_lists_and_returns_detail(tmp_path: Path) -> None:
    history_repository, submission_id = build_history_repository(tmp_path)

    async def override() -> SqliteSubmissionHistoryRepository:
        return history_repository

    app.dependency_overrides[get_submission_history_repository] = override

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            list_response = await client.get(
                "/api/applications",
                params={
                    "company": "Acme",
                    "title": "Automation",
                    "submitted_from": "2026-03-28",
                    "submitted_to": "2026-03-28",
                    "limit": "10",
                    "offset": "0",
                },
            )
            detail_response = await client.get(f"/api/applications/{submission_id}")
            return (
                cast(dict[str, Any], list_response.json()),
                cast(dict[str, Any], detail_response.json()),
            )

    try:
        list_payload, detail_payload = asyncio.run(exercise())
    finally:
        app.dependency_overrides.clear()

    assert list_payload["total"] == 1
    assert len(list_payload["items"]) == 1
    assert list_payload["items"][0]["company_name"] == "Acme"
    assert list_payload["items"][0]["job_title"] == "Automation Engineer"

    application = detail_payload["application"]
    assert application["submission"]["id"] == str(submission_id)
    assert application["job_posting"]["external_job_id"] == "job-123"
    assert application["answers"][0]["normalized_key"] == "work_authorization"
    assert application["profile_snapshot"]["data"]["profile"]["name"] == "Thiago Martins"
    assert application["execution_events"][0]["payload"]["stage"] == "submit_job"
    assert len(application["artifacts"]) == 1


def build_history_repository(tmp_path: Path) -> tuple[SqliteSubmissionHistoryRepository, str]:
    database_url = f"sqlite:///{(tmp_path / 'applications-api.db').resolve()}"
    upgrade_to_head(database_url)
    session_factory = create_session_factory(database_url)

    posting_repo = SqliteJobPostingRepository(session_factory)
    snapshot_repo = SqliteProfileSnapshotRepository(session_factory)
    submission_repo = SqliteSubmissionRepository(session_factory)
    answer_repo = SqliteAnswerRepository(session_factory)
    event_repo = SqliteExecutionEventRepository(session_factory)
    artifact_repo = SqliteArtifactSnapshotRepository(session_factory)
    history_repo = SqliteSubmissionHistoryRepository(session_factory)

    posting = posting_repo.save(
        build_posting(
            company_name="Acme",
            title="Automation Engineer",
            external_job_id="job-123",
            captured_at=utc_dt(28, 9),
        ),
    )
    snapshot = snapshot_repo.save(build_snapshot(created_at=utc_dt(28, 9)))
    submission = submission_repo.save(
        build_submission(
            job_posting_id=posting.id,
            profile_snapshot_id=snapshot.id,
            submitted_at=utc_dt(28, 12),
        ),
    )
    answer_repo.save(build_answer(submission_id=submission.id))
    artifact_repo.save(build_artifact(submission_id=submission.id, created_at=utc_dt(28, 12)))
    event_repo.save(
        build_execution_event(
            execution_id=posting.id,
            submission_id=submission.id,
            timestamp=utc_dt(28, 12),
        ),
    )

    ignored_submission = build_submission(
        job_posting_id=posting.id,
        profile_snapshot_id=snapshot.id,
        submitted_at=utc_dt(28, 13),
        note_suffix=" failed",
    )
    submission_repo.save(
        ApplicationSubmission(
            id=ignored_submission.id,
            job_posting_id=posting.id,
            status=SubmissionStatus.FAILED,
            started_at=ignored_submission.started_at,
            execution_origin=ExecutionOrigin.MANUAL,
            notes="validation error",
        ),
    )

    return history_repo, str(submission.id)
