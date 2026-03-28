from pathlib import Path

from job_applier.application.history import SubmissionHistoryFilters
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
from tests.integration.sqlite_helpers import (
    build_answer,
    build_artifact,
    build_execution_event,
    build_posting,
    build_snapshot,
    build_submission,
    upgrade_to_head,
    utc_dt,
)


def test_submission_history_queries_filter_and_paginate(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'history.db').resolve()}"
    upgrade_to_head(database_url)
    session_factory = create_session_factory(database_url)

    posting_repo = SqliteJobPostingRepository(session_factory)
    snapshot_repo = SqliteProfileSnapshotRepository(session_factory)
    submission_repo = SqliteSubmissionRepository(session_factory)
    answer_repo = SqliteAnswerRepository(session_factory)
    event_repo = SqliteExecutionEventRepository(session_factory)
    artifact_repo = SqliteArtifactSnapshotRepository(session_factory)
    history_repo = SqliteSubmissionHistoryRepository(session_factory)

    execution_counter = 0

    for company_name, title, external_job_id, submitted_day in (
        ("Acme", "Senior Backend Engineer", "job-001", 26),
        ("Beta", "Automation Engineer", "job-002", 27),
        ("Acme", "Python Platform Engineer", "job-003", 28),
    ):
        execution_counter += 1
        posting = posting_repo.save(
            build_posting(
                company_name=company_name,
                title=title,
                external_job_id=external_job_id,
                captured_at=utc_dt(submitted_day, 10),
            ),
        )
        snapshot = snapshot_repo.save(build_snapshot(created_at=utc_dt(submitted_day, 10)))
        submission = submission_repo.save(
            build_submission(
                job_posting_id=posting.id,
                profile_snapshot_id=snapshot.id,
                submitted_at=utc_dt(submitted_day, 12),
                note_suffix=f" #{external_job_id}",
            ),
        )
        answer_repo.save(build_answer(submission_id=submission.id, step_index=execution_counter))
        artifact_repo.save(
            build_artifact(submission_id=submission.id, created_at=utc_dt(submitted_day, 12)),
        )
        event_repo.save(
            build_execution_event(
                execution_id=posting.id,
                submission_id=submission.id,
                timestamp=utc_dt(submitted_day, 12),
            ),
        )

    ignored_posting = posting_repo.save(
        build_posting(
            company_name="Ignored",
            title="Pending Submission",
            external_job_id="job-999",
            captured_at=utc_dt(28, 13),
        ),
    )
    ignored_snapshot = snapshot_repo.save(build_snapshot(created_at=utc_dt(28, 13)))
    pending_seed = build_submission(
        job_posting_id=ignored_posting.id,
        profile_snapshot_id=ignored_snapshot.id,
        submitted_at=utc_dt(28, 14),
        note_suffix=" pending",
    )
    submission_repo.save(
        ApplicationSubmission(
            id=pending_seed.id,
            job_posting_id=ignored_posting.id,
            status=SubmissionStatus.PENDING,
            started_at=pending_seed.started_at,
            execution_origin=ExecutionOrigin.SCHEDULED,
        ),
    )

    by_company = history_repo.query(SubmissionHistoryFilters(company_name="Acme"))
    assert by_company.total == 2
    assert [item.job_posting.company_name for item in by_company.items] == ["Acme", "Acme"]

    by_title = history_repo.query(SubmissionHistoryFilters(title="Automation"))
    assert by_title.total == 1
    assert by_title.items[0].job_posting.title == "Automation Engineer"

    by_job = history_repo.query(SubmissionHistoryFilters(external_job_id="job-003"))
    assert by_job.total == 1
    assert by_job.items[0].submission.cv_version == "resume-v1.pdf"
    assert len(by_job.items[0].answers) == 1
    assert len(by_job.items[0].artifacts) == 1
    assert len(by_job.items[0].execution_events) == 1
    assert by_job.items[0].profile_snapshot is not None

    detail = history_repo.get(by_job.items[0].submission.id)
    assert detail is not None
    assert detail.job_posting.external_job_id == "job-003"
    assert detail.answers[0].normalized_key == "work_authorization"

    by_date = history_repo.query(
        SubmissionHistoryFilters(
            submitted_from=utc_dt(27, 0),
            submitted_to=utc_dt(28, 23),
        ),
    )
    assert by_date.total == 2

    paged = history_repo.query(SubmissionHistoryFilters(limit=1, offset=1))
    assert paged.total == 3
    assert len(paged.items) == 1
