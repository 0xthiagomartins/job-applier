from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

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
