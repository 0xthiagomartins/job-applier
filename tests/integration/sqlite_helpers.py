from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config

from job_applier.domain import (
    AnswerSource,
    ApplicationAnswer,
    ApplicationSubmission,
    ArtifactSnapshot,
    ArtifactType,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionOrigin,
    FillStrategy,
    JobPosting,
    Platform,
    ProfileSnapshot,
    QuestionType,
    RecruiterAction,
    RecruiterInteraction,
    RecruiterInteractionStatus,
    SeniorityLevel,
    SubmissionStatus,
    WorkplaceType,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_to_head(database_url: str) -> None:
    command.upgrade(make_alembic_config(database_url), "head")


def downgrade_to_base(database_url: str) -> None:
    command.downgrade(make_alembic_config(database_url), "base")


def build_posting(
    *,
    company_name: str,
    title: str,
    external_job_id: str,
    captured_at: datetime,
) -> JobPosting:
    return JobPosting(
        platform=Platform.LINKEDIN,
        url=f"https://www.linkedin.com/jobs/view/{external_job_id}",
        external_job_id=external_job_id,
        title=title,
        company_name=company_name,
        location="Remote",
        workplace_type=WorkplaceType.REMOTE,
        seniority=SeniorityLevel.SENIOR,
        description_raw=f"{title} at {company_name}",
        captured_at=captured_at,
    )


def build_snapshot(*, snapshot_id: UUID | None = None, created_at: datetime) -> ProfileSnapshot:
    return ProfileSnapshot(
        id=snapshot_id or uuid4(),
        data_json=json.dumps(
            {
                "config_version": "config-v1",
                "profile": {"name": "Thiago Martins"},
                "ruleset": {
                    "version": "ruleset-v1",
                    "allow_best_effort_autofill": True,
                    "auto_connect_with_recruiter": False,
                },
            },
            sort_keys=True,
        ),
        created_at=created_at,
    )


def build_submission(
    *,
    submission_id: UUID | None = None,
    job_posting_id: UUID,
    profile_snapshot_id: UUID,
    submitted_at: datetime,
    note_suffix: str = "",
) -> ApplicationSubmission:
    return ApplicationSubmission(
        id=submission_id or uuid4(),
        job_posting_id=job_posting_id,
        status=SubmissionStatus.SUBMITTED,
        started_at=submitted_at - timedelta(minutes=5),
        submitted_at=submitted_at,
        cv_version="resume-v1.pdf",
        cover_letter_version="cover-v1",
        profile_snapshot_id=profile_snapshot_id,
        ruleset_version="ruleset-v1",
        ai_model_used="o3-mini",
        execution_origin=ExecutionOrigin.SCHEDULED,
        notes=f"submitted successfully{note_suffix}",
    )


def build_answer(*, submission_id: UUID, step_index: int = 0) -> ApplicationAnswer:
    return ApplicationAnswer(
        submission_id=submission_id,
        step_index=step_index,
        question_raw="Are you authorized to work in Brazil?",
        question_type=QuestionType.WORK_AUTHORIZATION,
        normalized_key="work_authorization",
        answer_raw="Yes",
        answer_source=AnswerSource.RULE,
        fill_strategy=FillStrategy.DETERMINISTIC,
    )


def build_recruiter_interaction(*, submission_id: UUID, sent_at: datetime) -> RecruiterInteraction:
    return RecruiterInteraction(
        submission_id=submission_id,
        recruiter_name="Maria Recruiter",
        recruiter_profile_url="https://www.linkedin.com/in/maria-recruiter",
        action=RecruiterAction.CONNECT,
        status=RecruiterInteractionStatus.SENT,
        message_sent="Hi Maria, I just applied through LinkedIn Easy Apply.",
        sent_at=sent_at,
    )


def build_execution_event(
    *,
    execution_id: UUID,
    submission_id: UUID,
    timestamp: datetime,
) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id=execution_id,
        submission_id=submission_id,
        event_type=ExecutionEventType.SUBMISSION_COMPLETED,
        payload_json=json.dumps({"stage": "submit_job", "status": "submitted"}, sort_keys=True),
        timestamp=timestamp,
    )


def build_artifact(*, submission_id: UUID, created_at: datetime) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        submission_id=submission_id,
        artifact_type=ArtifactType.CV_METADATA,
        path="artifacts/runtime/cv/resume-v1.pdf",
        sha256="b" * 64,
        created_at=created_at,
    )


def utc_dt(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 3, day, hour, 0, tzinfo=UTC)
