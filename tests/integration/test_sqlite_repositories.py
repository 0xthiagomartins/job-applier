from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from job_applier.domain import ApplicationSubmission, ExecutionOrigin, SubmissionStatus
from job_applier.infrastructure.sqlite import create_session_factory
from job_applier.infrastructure.sqlite.repositories import (
    SqliteAnswerRepository,
    SqliteArtifactSnapshotRepository,
    SqliteExecutionEventRepository,
    SqliteJobPostingRepository,
    SqliteProfileSnapshotRepository,
    SqliteRecruiterInteractionRepository,
    SqliteSubmissionRepository,
)
from tests.integration.sqlite_helpers import (
    build_answer,
    build_artifact,
    build_execution_event,
    build_posting,
    build_recruiter_interaction,
    build_snapshot,
    build_submission,
    upgrade_to_head,
    utc_dt,
)


def test_sqlite_repositories_support_roundtrip_crud(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'repositories.db').resolve()}"
    upgrade_to_head(database_url)
    session_factory = create_session_factory(database_url)

    posting_repo = SqliteJobPostingRepository(session_factory)
    snapshot_repo = SqliteProfileSnapshotRepository(session_factory)
    submission_repo = SqliteSubmissionRepository(session_factory)
    answer_repo = SqliteAnswerRepository(session_factory)
    recruiter_repo = SqliteRecruiterInteractionRepository(session_factory)
    event_repo = SqliteExecutionEventRepository(session_factory)
    artifact_repo = SqliteArtifactSnapshotRepository(session_factory)

    posting = build_posting(
        company_name="Acme",
        title="Senior Backend Engineer",
        external_job_id="job-001",
        captured_at=utc_dt(28, 10),
    )
    saved_posting = posting_repo.save(posting)

    snapshot = build_snapshot(created_at=utc_dt(28, 10))
    saved_snapshot = snapshot_repo.save(snapshot)

    submission = build_submission(
        job_posting_id=saved_posting.id,
        profile_snapshot_id=saved_snapshot.id,
        submitted_at=utc_dt(28, 11),
    )
    saved_submission = submission_repo.save(submission)

    answer = answer_repo.save(build_answer(submission_id=saved_submission.id))
    recruiter = recruiter_repo.save(
        build_recruiter_interaction(submission_id=saved_submission.id, sent_at=utc_dt(28, 11)),
    )
    execution_id = uuid4()
    event = event_repo.save(
        build_execution_event(
            execution_id=execution_id,
            submission_id=saved_submission.id,
            timestamp=utc_dt(28, 11),
        ),
    )
    artifact = artifact_repo.save(
        build_artifact(submission_id=saved_submission.id, created_at=utc_dt(28, 11)),
    )

    assert posting_repo.get(saved_posting.id) == saved_posting
    assert snapshot_repo.get(saved_snapshot.id) == saved_snapshot
    assert submission_repo.get(saved_submission.id) == saved_submission
    assert answer_repo.list_for_submission(saved_submission.id) == [answer]
    assert recruiter_repo.list_for_submission(saved_submission.id) == [recruiter]
    assert event_repo.list_for_submission(saved_submission.id) == [event]
    assert event_repo.list_for_execution(execution_id) == [event]
    assert artifact_repo.list_for_submission(saved_submission.id) == [artifact]
    assert submission_repo.list_by_submitted_at(
        submitted_from=utc_dt(28, 0),
        submitted_to=utc_dt(28, 23),
    ) == [saved_submission]
    assert (
        submission_repo.find_latest_successful_for_job_posting(saved_posting.id) == saved_submission
    )

    updated_submission = submission_repo.save(
        replace(saved_submission, notes="updated after recruiter follow-up"),
    )
    assert updated_submission.notes == "updated after recruiter follow-up"

    answer_repo.delete(answer.id)
    recruiter_repo.delete(recruiter.id)
    event_repo.delete(event.id)
    artifact_repo.delete(artifact.id)
    submission_repo.delete(saved_submission.id)
    snapshot_repo.delete(saved_snapshot.id)
    posting_repo.delete(saved_posting.id)

    assert answer_repo.get(answer.id) is None
    assert recruiter_repo.get(recruiter.id) is None
    assert event_repo.get(event.id) is None
    assert artifact_repo.get(artifact.id) is None
    assert submission_repo.get(saved_submission.id) is None
    assert snapshot_repo.get(saved_snapshot.id) is None
    assert posting_repo.get(saved_posting.id) is None


def test_sqlite_repositories_enforce_foreign_keys_for_submission_links(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'repository-fks.db').resolve()}"
    upgrade_to_head(database_url)
    session_factory = create_session_factory(database_url)

    submission_repo = SqliteSubmissionRepository(session_factory)
    artifact_repo = SqliteArtifactSnapshotRepository(session_factory)
    recruiter_repo = SqliteRecruiterInteractionRepository(session_factory)
    event_repo = SqliteExecutionEventRepository(session_factory)

    missing_submission_id = uuid4()

    with pytest.raises(IntegrityError):
        submission_repo.save(
            ApplicationSubmission(
                job_posting_id=uuid4(),
                execution_origin=ExecutionOrigin.MANUAL,
            ),
        )

    with pytest.raises(IntegrityError):
        artifact_repo.save(
            build_artifact(submission_id=missing_submission_id, created_at=utc_dt(28, 11))
        )

    with pytest.raises(IntegrityError):
        recruiter_repo.save(
            build_recruiter_interaction(
                submission_id=missing_submission_id, sent_at=utc_dt(28, 11)
            ),
        )

    with pytest.raises(IntegrityError):
        event_repo.save(
            build_execution_event(
                execution_id=uuid4(),
                submission_id=missing_submission_id,
                timestamp=utc_dt(28, 11),
            ),
        )


def test_sqlite_submission_repository_returns_latest_successful_by_job(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'repository-successful-lookup.db').resolve()}"
    upgrade_to_head(database_url)
    session_factory = create_session_factory(database_url)

    posting_repo = SqliteJobPostingRepository(session_factory)
    snapshot_repo = SqliteProfileSnapshotRepository(session_factory)
    submission_repo = SqliteSubmissionRepository(session_factory)

    posting = posting_repo.save(
        build_posting(
            company_name="Acme",
            title="Senior Backend Engineer",
            external_job_id="job-success-lookup",
            captured_at=utc_dt(28, 10),
        ),
    )
    snapshot = snapshot_repo.save(build_snapshot(created_at=utc_dt(28, 10)))

    older_submission = submission_repo.save(
        build_submission(
            job_posting_id=posting.id,
            profile_snapshot_id=snapshot.id,
            submitted_at=utc_dt(28, 11),
            note_suffix=" older",
        ),
    )
    failed_submission = submission_repo.save(
        ApplicationSubmission(
            job_posting_id=posting.id,
            execution_origin=ExecutionOrigin.MANUAL,
            status=SubmissionStatus.FAILED,
            notes="validation error",
        )
    )
    newer_submission = submission_repo.save(
        build_submission(
            job_posting_id=posting.id,
            profile_snapshot_id=snapshot.id,
            submitted_at=utc_dt(28, 13),
            note_suffix=" newer",
        ),
    )

    assert older_submission.id != newer_submission.id
    assert failed_submission is not None
    assert submission_repo.find_latest_successful_for_job_posting(posting.id) == newer_submission
