"""SQLite repository implementations and history queries."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, TypeVar, cast
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import selectinload

from job_applier.application.history import (
    SubmissionHistoryEntry,
    SubmissionHistoryFilters,
    SubmissionHistoryPage,
)
from job_applier.application.repositories import (
    AnswerRepository,
    ArtifactSnapshotRepository,
    ExecutionEventRepository,
    JobPostingRepository,
    ProfileSnapshotRepository,
    RecruiterInteractionRepository,
    SubmissionHistoryRepository,
    SubmissionRepository,
)
from job_applier.domain.entities import (
    ApplicationAnswer,
    ApplicationSubmission,
    ArtifactSnapshot,
    ExecutionEvent,
    JobPosting,
    ProfileSnapshot,
    RecruiterInteraction,
)
from job_applier.domain.enums import (
    AnswerSource,
    ArtifactType,
    ExecutionEventType,
    ExecutionOrigin,
    FillStrategy,
    Platform,
    QuestionType,
    RecruiterAction,
    RecruiterInteractionStatus,
    SeniorityLevel,
    SubmissionStatus,
    WorkplaceType,
)
from job_applier.infrastructure.sqlite.database import SessionProvider
from job_applier.infrastructure.sqlite.models import (
    ApplicationAnswerModel,
    ApplicationSubmissionModel,
    ArtifactSnapshotModel,
    ExecutionEventModel,
    JobPostingModel,
    ProfileSnapshotModel,
    RecruiterInteractionModel,
)

ModelT = TypeVar("ModelT")
EntityT = TypeVar("EntityT")


def _db_to_utc(value: datetime) -> datetime:
    """Normalize persisted timestamps back to aware UTC datetimes."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_text_from_value(value: Any) -> str:
    """Serialize a JSON-capable database value back into the domain string field."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_value_from_text(value: str) -> Any:
    """Deserialize a domain JSON string before persistence."""

    return json.loads(value)


def _posting_to_model(entity: JobPosting) -> JobPostingModel:
    return JobPostingModel(
        id=entity.id,
        platform=entity.platform.value,
        url=entity.url,
        external_job_id=entity.external_job_id,
        title=entity.title,
        company_name=entity.company_name,
        location=entity.location,
        workplace_type=entity.workplace_type.value if entity.workplace_type else None,
        seniority=entity.seniority.value if entity.seniority else None,
        easy_apply=entity.easy_apply,
        description_raw=entity.description_raw,
        description_hash=entity.description_hash,
        captured_at=entity.captured_at,
    )


def _posting_from_model(model: JobPostingModel) -> JobPosting:
    return JobPosting(
        id=model.id,
        platform=Platform(model.platform),
        url=model.url,
        external_job_id=model.external_job_id,
        title=model.title,
        company_name=model.company_name,
        location=model.location,
        workplace_type=WorkplaceType(model.workplace_type) if model.workplace_type else None,
        seniority=SeniorityLevel(model.seniority) if model.seniority else None,
        easy_apply=model.easy_apply,
        description_raw=model.description_raw,
        description_hash=model.description_hash,
        captured_at=_db_to_utc(model.captured_at),
    )


def _submission_to_model(entity: ApplicationSubmission) -> ApplicationSubmissionModel:
    return ApplicationSubmissionModel(
        id=entity.id,
        job_posting_id=entity.job_posting_id,
        status=entity.status.value,
        started_at=entity.started_at,
        submitted_at=entity.submitted_at,
        cv_version=entity.cv_version,
        cover_letter_version=entity.cover_letter_version,
        profile_snapshot_id=entity.profile_snapshot_id,
        ruleset_version=entity.ruleset_version,
        ai_model_used=entity.ai_model_used,
        execution_origin=entity.execution_origin.value,
        notes=entity.notes,
    )


def _submission_from_model(model: ApplicationSubmissionModel) -> ApplicationSubmission:
    return ApplicationSubmission(
        id=model.id,
        job_posting_id=model.job_posting_id,
        status=SubmissionStatus(model.status),
        started_at=_db_to_utc(model.started_at),
        submitted_at=_db_to_utc(model.submitted_at) if model.submitted_at else None,
        cv_version=model.cv_version,
        cover_letter_version=model.cover_letter_version,
        profile_snapshot_id=model.profile_snapshot_id,
        ruleset_version=model.ruleset_version,
        ai_model_used=model.ai_model_used,
        execution_origin=ExecutionOrigin(model.execution_origin),
        notes=model.notes,
    )


def _answer_to_model(entity: ApplicationAnswer) -> ApplicationAnswerModel:
    return ApplicationAnswerModel(
        id=entity.id,
        submission_id=entity.submission_id,
        step_index=entity.step_index,
        question_raw=entity.question_raw,
        question_type=entity.question_type.value,
        normalized_key=entity.normalized_key,
        answer_raw=entity.answer_raw,
        answer_source=entity.answer_source.value,
        fill_strategy=entity.fill_strategy.value,
        ambiguity_flag=entity.ambiguity_flag,
    )


def _answer_from_model(model: ApplicationAnswerModel) -> ApplicationAnswer:
    return ApplicationAnswer(
        id=model.id,
        submission_id=model.submission_id,
        step_index=model.step_index,
        question_raw=model.question_raw,
        question_type=QuestionType(model.question_type),
        normalized_key=model.normalized_key,
        answer_raw=model.answer_raw,
        answer_source=AnswerSource(model.answer_source),
        fill_strategy=FillStrategy(model.fill_strategy),
        ambiguity_flag=model.ambiguity_flag,
    )


def _snapshot_to_model(entity: ProfileSnapshot) -> ProfileSnapshotModel:
    return ProfileSnapshotModel(
        id=entity.id,
        data_json=cast(dict[str, Any], _json_value_from_text(entity.data_json)),
        created_at=entity.created_at,
    )


def _snapshot_from_model(model: ProfileSnapshotModel) -> ProfileSnapshot:
    return ProfileSnapshot(
        id=model.id,
        data_json=_json_text_from_value(model.data_json),
        created_at=_db_to_utc(model.created_at),
    )


def _recruiter_to_model(entity: RecruiterInteraction) -> RecruiterInteractionModel:
    return RecruiterInteractionModel(
        id=entity.id,
        submission_id=entity.submission_id,
        recruiter_name=entity.recruiter_name,
        recruiter_profile_url=entity.recruiter_profile_url,
        action=entity.action.value,
        status=entity.status.value,
        message_sent=entity.message_sent,
        sent_at=entity.sent_at,
    )


def _recruiter_from_model(model: RecruiterInteractionModel) -> RecruiterInteraction:
    return RecruiterInteraction(
        id=model.id,
        submission_id=model.submission_id,
        recruiter_name=model.recruiter_name,
        recruiter_profile_url=model.recruiter_profile_url,
        action=RecruiterAction(model.action),
        status=RecruiterInteractionStatus(model.status),
        message_sent=model.message_sent,
        sent_at=_db_to_utc(model.sent_at) if model.sent_at else None,
    )


def _event_to_model(entity: ExecutionEvent) -> ExecutionEventModel:
    return ExecutionEventModel(
        id=entity.id,
        execution_id=entity.execution_id,
        submission_id=entity.submission_id,
        event_type=entity.event_type.value,
        payload_json=cast(dict[str, Any], _json_value_from_text(entity.payload_json)),
        timestamp=entity.timestamp,
    )


def _event_from_model(model: ExecutionEventModel) -> ExecutionEvent:
    return ExecutionEvent(
        id=model.id,
        execution_id=model.execution_id,
        submission_id=model.submission_id,
        event_type=ExecutionEventType(model.event_type),
        payload_json=_json_text_from_value(model.payload_json),
        timestamp=_db_to_utc(model.timestamp),
    )


def _artifact_to_model(entity: ArtifactSnapshot) -> ArtifactSnapshotModel:
    return ArtifactSnapshotModel(
        id=entity.id,
        submission_id=entity.submission_id,
        artifact_type=entity.artifact_type.value,
        path=entity.path,
        sha256=entity.sha256,
        created_at=entity.created_at,
    )


def _artifact_from_model(model: ArtifactSnapshotModel) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        id=model.id,
        submission_id=model.submission_id,
        artifact_type=ArtifactType(model.artifact_type),
        path=model.path,
        sha256=model.sha256,
        created_at=_db_to_utc(model.created_at),
    )


class SqliteRepository[EntityT, ModelT]:
    """Small reusable base for concrete SQLite repositories."""

    def __init__(self, session_provider: SessionProvider) -> None:
        self._session_provider = session_provider

    @property
    def model_type(self) -> type[ModelT]:
        """Return the SQLAlchemy model handled by the repository."""

        raise NotImplementedError

    def _to_model(self, entity: EntityT) -> ModelT:
        raise NotImplementedError

    def _from_model(self, model: ModelT) -> EntityT:
        raise NotImplementedError

    def _base_query(self) -> Select[tuple[ModelT]]:
        return select(self.model_type)

    def _apply_ordering(self, statement: Select[tuple[ModelT]]) -> Select[tuple[ModelT]]:
        return statement.order_by(cast(Any, self.model_type).id)

    def save(self, entity: EntityT) -> EntityT:
        """Persist a new or updated entity."""

        with self._session_provider() as session:
            model = self._to_model(entity)
            merged = session.merge(model)
            session.commit()
            session.refresh(merged)
            return self._from_model(merged)

    def get(self, entity_id: UUID) -> EntityT | None:
        """Load one entity by id."""

        with self._session_provider() as session:
            model = session.get(self.model_type, entity_id)
            return None if model is None else self._from_model(model)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[EntityT]:
        """List persisted entities in a stable order."""

        statement = self._apply_ordering(self._base_query()).limit(limit).offset(offset)
        with self._session_provider() as session:
            models = session.scalars(statement).all()
            return [self._from_model(model) for model in models]

    def delete(self, entity_id: UUID) -> None:
        """Delete one entity when present."""

        with self._session_provider() as session:
            model = session.get(self.model_type, entity_id)
            if model is not None:
                session.delete(model)
                session.commit()


class SqliteJobPostingRepository(
    SqliteRepository[JobPosting, JobPostingModel],
    JobPostingRepository,
):
    @property
    def model_type(self) -> type[JobPostingModel]:
        return JobPostingModel

    def _to_model(self, entity: JobPosting) -> JobPostingModel:
        return _posting_to_model(entity)

    def _from_model(self, model: JobPostingModel) -> JobPosting:
        return _posting_from_model(model)

    def _apply_ordering(
        self,
        statement: Select[tuple[JobPostingModel]],
    ) -> Select[tuple[JobPostingModel]]:
        return statement.order_by(JobPostingModel.captured_at.desc(), JobPostingModel.id)

    def save(self, entity: JobPosting) -> JobPosting:
        """Persist a new or updated posting while deduplicating on external id."""

        if entity.external_job_id:
            existing = self.find_by_external_job_id(
                platform=entity.platform.value,
                external_job_id=entity.external_job_id,
            )
            if existing is not None:
                entity = replace(entity, id=existing.id)
        return super().save(entity)

    def find_by_external_job_id(
        self,
        *,
        platform: str,
        external_job_id: str,
    ) -> JobPosting | None:
        statement = self._base_query().where(
            JobPostingModel.platform == platform,
            JobPostingModel.external_job_id == external_job_id,
        )
        with self._session_provider() as session:
            model = session.scalar(statement)
            return None if model is None else self._from_model(model)


class SqliteSubmissionRepository(
    SqliteRepository[ApplicationSubmission, ApplicationSubmissionModel],
    SubmissionRepository,
):
    @property
    def model_type(self) -> type[ApplicationSubmissionModel]:
        return ApplicationSubmissionModel

    def _to_model(self, entity: ApplicationSubmission) -> ApplicationSubmissionModel:
        return _submission_to_model(entity)

    def _from_model(self, model: ApplicationSubmissionModel) -> ApplicationSubmission:
        return _submission_from_model(model)

    def _apply_ordering(
        self,
        statement: Select[tuple[ApplicationSubmissionModel]],
    ) -> Select[tuple[ApplicationSubmissionModel]]:
        return statement.order_by(
            ApplicationSubmissionModel.submitted_at.desc(),
            ApplicationSubmissionModel.started_at.desc(),
            ApplicationSubmissionModel.id,
        )

    def list_by_submitted_at(
        self,
        *,
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ApplicationSubmission]:
        statement = self._base_query()
        if submitted_from is not None:
            statement = statement.where(ApplicationSubmissionModel.submitted_at >= submitted_from)
        if submitted_to is not None:
            statement = statement.where(ApplicationSubmissionModel.submitted_at <= submitted_to)
        statement = self._apply_ordering(statement).limit(limit).offset(offset)
        with self._session_provider() as session:
            models = session.scalars(statement).all()
            return [self._from_model(model) for model in models]


class SqliteAnswerRepository(
    SqliteRepository[ApplicationAnswer, ApplicationAnswerModel],
    AnswerRepository,
):
    @property
    def model_type(self) -> type[ApplicationAnswerModel]:
        return ApplicationAnswerModel

    def _to_model(self, entity: ApplicationAnswer) -> ApplicationAnswerModel:
        return _answer_to_model(entity)

    def _from_model(self, model: ApplicationAnswerModel) -> ApplicationAnswer:
        return _answer_from_model(model)

    def _apply_ordering(
        self,
        statement: Select[tuple[ApplicationAnswerModel]],
    ) -> Select[tuple[ApplicationAnswerModel]]:
        return statement.order_by(
            ApplicationAnswerModel.submission_id,
            ApplicationAnswerModel.step_index,
        )

    def list_for_submission(self, submission_id: UUID) -> list[ApplicationAnswer]:
        statement = self._apply_ordering(
            self._base_query().where(ApplicationAnswerModel.submission_id == submission_id),
        )
        with self._session_provider() as session:
            models = session.scalars(statement).all()
            return [self._from_model(model) for model in models]


class SqliteProfileSnapshotRepository(
    SqliteRepository[ProfileSnapshot, ProfileSnapshotModel],
    ProfileSnapshotRepository,
):
    @property
    def model_type(self) -> type[ProfileSnapshotModel]:
        return ProfileSnapshotModel

    def _to_model(self, entity: ProfileSnapshot) -> ProfileSnapshotModel:
        return _snapshot_to_model(entity)

    def _from_model(self, model: ProfileSnapshotModel) -> ProfileSnapshot:
        return _snapshot_from_model(model)

    def _apply_ordering(
        self,
        statement: Select[tuple[ProfileSnapshotModel]],
    ) -> Select[tuple[ProfileSnapshotModel]]:
        return statement.order_by(ProfileSnapshotModel.created_at.desc(), ProfileSnapshotModel.id)


class SqliteRecruiterInteractionRepository(
    SqliteRepository[RecruiterInteraction, RecruiterInteractionModel],
    RecruiterInteractionRepository,
):
    @property
    def model_type(self) -> type[RecruiterInteractionModel]:
        return RecruiterInteractionModel

    def _to_model(self, entity: RecruiterInteraction) -> RecruiterInteractionModel:
        return _recruiter_to_model(entity)

    def _from_model(self, model: RecruiterInteractionModel) -> RecruiterInteraction:
        return _recruiter_from_model(model)

    def _apply_ordering(
        self,
        statement: Select[tuple[RecruiterInteractionModel]],
    ) -> Select[tuple[RecruiterInteractionModel]]:
        return statement.order_by(
            RecruiterInteractionModel.sent_at.desc(),
            RecruiterInteractionModel.id,
        )

    def list_for_submission(self, submission_id: UUID) -> list[RecruiterInteraction]:
        statement = self._apply_ordering(
            self._base_query().where(RecruiterInteractionModel.submission_id == submission_id),
        )
        with self._session_provider() as session:
            models = session.scalars(statement).all()
            return [self._from_model(model) for model in models]


class SqliteExecutionEventRepository(
    SqliteRepository[ExecutionEvent, ExecutionEventModel],
    ExecutionEventRepository,
):
    @property
    def model_type(self) -> type[ExecutionEventModel]:
        return ExecutionEventModel

    def _to_model(self, entity: ExecutionEvent) -> ExecutionEventModel:
        return _event_to_model(entity)

    def _from_model(self, model: ExecutionEventModel) -> ExecutionEvent:
        return _event_from_model(model)

    def _apply_ordering(
        self,
        statement: Select[tuple[ExecutionEventModel]],
    ) -> Select[tuple[ExecutionEventModel]]:
        return statement.order_by(ExecutionEventModel.timestamp.asc(), ExecutionEventModel.id)

    def list_for_submission(self, submission_id: UUID) -> list[ExecutionEvent]:
        statement = self._apply_ordering(
            self._base_query().where(ExecutionEventModel.submission_id == submission_id),
        )
        with self._session_provider() as session:
            models = session.scalars(statement).all()
            return [self._from_model(model) for model in models]

    def list_for_execution(self, execution_id: UUID) -> list[ExecutionEvent]:
        statement = self._apply_ordering(
            self._base_query().where(ExecutionEventModel.execution_id == execution_id),
        )
        with self._session_provider() as session:
            models = session.scalars(statement).all()
            return [self._from_model(model) for model in models]


class SqliteArtifactSnapshotRepository(
    SqliteRepository[ArtifactSnapshot, ArtifactSnapshotModel],
    ArtifactSnapshotRepository,
):
    @property
    def model_type(self) -> type[ArtifactSnapshotModel]:
        return ArtifactSnapshotModel

    def _to_model(self, entity: ArtifactSnapshot) -> ArtifactSnapshotModel:
        return _artifact_to_model(entity)

    def _from_model(self, model: ArtifactSnapshotModel) -> ArtifactSnapshot:
        return _artifact_from_model(model)

    def _apply_ordering(
        self,
        statement: Select[tuple[ArtifactSnapshotModel]],
    ) -> Select[tuple[ArtifactSnapshotModel]]:
        return statement.order_by(ArtifactSnapshotModel.created_at.desc(), ArtifactSnapshotModel.id)

    def list_for_submission(self, submission_id: UUID) -> list[ArtifactSnapshot]:
        statement = self._apply_ordering(
            self._base_query().where(ArtifactSnapshotModel.submission_id == submission_id),
        )
        with self._session_provider() as session:
            models = session.scalars(statement).all()
            return [self._from_model(model) for model in models]


class SqliteSubmissionHistoryRepository(SubmissionHistoryRepository):
    """Query successful submissions with related audit context."""

    def __init__(self, session_provider: SessionProvider) -> None:
        self._session_provider = session_provider

    def query(self, filters: SubmissionHistoryFilters) -> SubmissionHistoryPage:
        """Return paginated successful application history."""

        statement = self._with_history_loads(self._filtered_submissions(filters))
        statement = statement.order_by(
            ApplicationSubmissionModel.submitted_at.desc(),
            ApplicationSubmissionModel.id,
        )
        paged_statement = statement.limit(filters.limit).offset(filters.offset)

        with self._session_provider() as session:
            total = session.scalar(
                select(func.count()).select_from(self._filtered_submissions(filters).subquery()),
            )
            rows = session.scalars(paged_statement).all()

        return SubmissionHistoryPage(
            items=tuple(self._build_history_entry(row) for row in rows),
            total=total or 0,
            limit=filters.limit,
            offset=filters.offset,
        )

    def get(self, submission_id: UUID) -> SubmissionHistoryEntry | None:
        """Return one successful submission with all audit relationships loaded."""

        statement = self._with_history_loads(
            select(ApplicationSubmissionModel).where(
                ApplicationSubmissionModel.id == submission_id,
                ApplicationSubmissionModel.status == SubmissionStatus.SUBMITTED.value,
            ),
        )
        with self._session_provider() as session:
            model = session.scalar(statement)

        if model is None:
            return None
        return self._build_history_entry(model)

    def _with_history_loads(
        self,
        statement: Select[tuple[ApplicationSubmissionModel]],
    ) -> Select[tuple[ApplicationSubmissionModel]]:
        return statement.options(
            selectinload(ApplicationSubmissionModel.job_posting),
            selectinload(ApplicationSubmissionModel.profile_snapshot),
            selectinload(ApplicationSubmissionModel.answers),
            selectinload(ApplicationSubmissionModel.recruiter_interactions),
            selectinload(ApplicationSubmissionModel.execution_events),
            selectinload(ApplicationSubmissionModel.artifact_snapshots),
        )

    def _filtered_submissions(
        self,
        filters: SubmissionHistoryFilters,
    ) -> Select[tuple[ApplicationSubmissionModel]]:
        statement = select(ApplicationSubmissionModel).join(ApplicationSubmissionModel.job_posting)
        statement = statement.where(
            ApplicationSubmissionModel.status == SubmissionStatus.SUBMITTED.value,
        )

        if filters.company_name:
            statement = statement.where(
                JobPostingModel.company_name.ilike(f"%{filters.company_name}%"),
            )
        if filters.title:
            statement = statement.where(JobPostingModel.title.ilike(f"%{filters.title}%"))
        if filters.external_job_id:
            statement = statement.where(
                or_(
                    JobPostingModel.external_job_id == filters.external_job_id,
                    JobPostingModel.title.ilike(f"%{filters.external_job_id}%"),
                ),
            )
        if filters.submitted_from:
            statement = statement.where(
                ApplicationSubmissionModel.submitted_at >= filters.submitted_from,
            )
        if filters.submitted_to:
            statement = statement.where(
                ApplicationSubmissionModel.submitted_at <= filters.submitted_to,
            )
        return statement

    def _build_history_entry(self, model: ApplicationSubmissionModel) -> SubmissionHistoryEntry:
        answers = tuple(_answer_from_model(answer) for answer in model.answers)
        recruiters = tuple(
            _recruiter_from_model(interaction) for interaction in model.recruiter_interactions
        )
        events = tuple(_event_from_model(event) for event in model.execution_events)
        artifacts = tuple(_artifact_from_model(artifact) for artifact in model.artifact_snapshots)

        return SubmissionHistoryEntry(
            submission=_submission_from_model(model),
            job_posting=_posting_from_model(model.job_posting),
            answers=answers,
            profile_snapshot=(
                _snapshot_from_model(model.profile_snapshot) if model.profile_snapshot else None
            ),
            recruiter_interactions=recruiters,
            execution_events=events,
            artifacts=artifacts,
        )


def build_sqlite_repositories(
    session_provider: SessionProvider,
) -> tuple[
    SqliteJobPostingRepository,
    SqliteSubmissionRepository,
    SqliteAnswerRepository,
    SqliteProfileSnapshotRepository,
    SqliteRecruiterInteractionRepository,
    SqliteExecutionEventRepository,
    SqliteArtifactSnapshotRepository,
]:
    """Build the seven concrete repositories expected by the MVP."""

    return (
        SqliteJobPostingRepository(session_provider),
        SqliteSubmissionRepository(session_provider),
        SqliteAnswerRepository(session_provider),
        SqliteProfileSnapshotRepository(session_provider),
        SqliteRecruiterInteractionRepository(session_provider),
        SqliteExecutionEventRepository(session_provider),
        SqliteArtifactSnapshotRepository(session_provider),
    )
