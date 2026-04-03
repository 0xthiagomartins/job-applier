"""Execution orchestration for scheduled and manual agent runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from job_applier.application.config import (
    AgentConfig,
    AIConfig,
    RulesetConfig,
    ScheduleConfig,
    SearchConfig,
    UserAgentSettings,
    UserProfileConfig,
)
from job_applier.application.panel import PanelSettingsDocument
from job_applier.application.repositories import SubmissionRepository
from job_applier.application.snapshotting import (
    SuccessfulSubmissionRecord,
    build_profile_snapshot,
    create_successful_submission_record,
)
from job_applier.domain.entities import (
    ApplicationSubmission,
    ExecutionEvent,
    JobPosting,
    ProfileSnapshot,
)
from job_applier.domain.enums import (
    AgentExecutionStatus,
    ExecutionEventType,
    ExecutionOrigin,
    SubmissionStatus,
)
from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore
from job_applier.observability import (
    append_output_jsonl,
    append_timeline_event,
    bind_execution_context,
    bind_run_output,
    reset_run_output,
    update_progress_snapshot,
    write_output_json,
)

logger = logging.getLogger(__name__)


class PanelSettingsConfigurationError(ValueError):
    """Raised when the persisted panel data is not ready for agent execution."""


class ExecutionRunSummary(BaseModel):
    """Serializable summary returned by orchestrated agent runs."""

    model_config = ConfigDict(frozen=True)

    execution_id: UUID
    origin: ExecutionOrigin
    status: AgentExecutionStatus
    started_at: datetime
    finished_at: datetime | None = None
    snapshot_id: UUID | None = None
    jobs_seen: int = 0
    jobs_selected: int = 0
    successful_submissions: int = 0
    error_count: int = 0
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ScoredJobPosting:
    """Result of the qualification stage for a single posting."""

    posting: JobPosting
    selected: bool = True
    score: float | None = None
    reason: str | None = None


class JobFetcher(Protocol):
    """Port used to fetch recent jobs for an execution."""

    async def fetch(self, settings: UserAgentSettings) -> list[JobPosting]:
        """Return recently found jobs according to the user settings."""


@runtime_checkable
class IncrementalJobFetcher(Protocol):
    """Optional fetcher contract that yields persisted jobs incrementally."""

    async def fetch_incremental(
        self,
        settings: UserAgentSettings,
        on_job: Callable[[JobPosting], Awaitable[bool]],
    ) -> int:
        """Persist jobs incrementally and call ``on_job`` for each unique posting.

        Return the total number of unique persisted jobs seen during the run.
        Returning ``False`` from ``on_job`` asks the fetcher to stop early.
        """


class JobScorer(Protocol):
    """Port used to qualify a posting before application."""

    async def score(self, settings: UserAgentSettings, posting: JobPosting) -> ScoredJobPosting:
        """Return the qualification result for a posting."""


class JobSubmitter(Protocol):
    """Port used to execute the application step for a selected posting."""

    async def submit(
        self,
        settings: UserAgentSettings,
        posting: JobPosting,
        *,
        execution_id: UUID,
        origin: ExecutionOrigin,
    ) -> SubmissionAttempt:
        """Attempt to apply to the selected job posting."""


class SuccessfulSubmissionStore(Protocol):
    """Persistence contract used to look up successful submission audit bundles."""

    def save(self, record: SuccessfulSubmissionRecord) -> None:
        """Persist the audit bundle for a successful submission."""


@dataclass(frozen=True, slots=True)
class SubmissionAttempt:
    """Structured result returned by a job submitter."""

    submission: ApplicationSubmission
    successful_record: SuccessfulSubmissionRecord | None = None


class ExecutionStore(Protocol):
    """Persistence protocol for execution summaries and events."""

    def save_execution(self, summary: ExecutionRunSummary) -> None:
        """Persist the latest summary for an execution."""

    def append_event(self, event: ExecutionEvent) -> None:
        """Append a single execution event."""

    def list_recent_executions(self, *, limit: int = 10) -> list[ExecutionRunSummary]:
        """Return recent executions in reverse chronological order."""


class EmptyJobFetcher:
    """Default fetcher used until real platform integration lands."""

    async def fetch(self, settings: UserAgentSettings) -> list[JobPosting]:
        logger.info(
            "job_fetch_stage",
            extra={
                "keywords": list(settings.search.keywords),
                "location": settings.search.location,
                "posted_within_hours": settings.search.posted_within_hours,
            },
        )
        return []


class PassThroughJobScorer:
    """Default scorer that keeps orchestration functional without custom ranking."""

    async def score(self, settings: UserAgentSettings, posting: JobPosting) -> ScoredJobPosting:
        logger.info(
            "job_score_stage",
            extra={
                "job_posting_id": str(posting.id),
                "ai_model": settings.ai.model,
            },
        )
        return ScoredJobPosting(posting=posting, selected=True, score=1.0, reason="default-pass")


class NoOpJobSubmitter:
    """Default submitter that keeps the flow explicit until browser automation is wired."""

    async def submit(
        self,
        settings: UserAgentSettings,
        posting: JobPosting,
        *,
        execution_id: UUID,
        origin: ExecutionOrigin,
    ) -> SubmissionAttempt:
        del execution_id
        logger.info(
            "job_submit_stage",
            extra={
                "job_posting_id": str(posting.id),
                "origin": origin.value,
                "auto_connect_with_recruiter": settings.agent.auto_connect_with_recruiter,
            },
        )
        return SubmissionAttempt(
            submission=ApplicationSubmission(
                job_posting_id=posting.id,
                status=SubmissionStatus.SKIPPED,
                execution_origin=origin,
                notes="Application submitter is not configured yet.",
            ),
        )


def build_user_agent_settings(document: PanelSettingsDocument) -> UserAgentSettings:
    """Build validated execution settings from the persisted panel document."""

    missing_fields = [
        field_name
        for field_name, field_value in (("profile.email", document.profile.email),)
        if field_value is None
    ]
    if missing_fields:
        joined = ", ".join(missing_fields)
        msg = f"Missing required panel fields: {joined}."
        raise PanelSettingsConfigurationError(msg)

    email = document.profile.email
    assert email is not None

    try:
        return UserAgentSettings(
            config_version="config-v1",
            profile=UserProfileConfig(
                name=document.profile.name,
                email=email,
                phone=document.profile.phone,
                city=document.profile.city,
                linkedin_url=document.profile.linkedin_url,
                github_url=document.profile.github_url,
                portfolio_url=document.profile.portfolio_url,
                years_experience_by_stack=document.profile.years_experience_by_stack,
                work_authorized=document.profile.work_authorized,
                needs_sponsorship=document.profile.needs_sponsorship,
                salary_expectation=document.profile.salary_expectation,
                availability=document.profile.availability,
                default_responses=document.profile.default_responses,
                cv_path=document.profile.cv_path,
                cv_filename=document.profile.cv_filename,
                positive_filters=document.preferences.positive_keywords,
                blacklist=document.preferences.negative_keywords,
            ),
            search=SearchConfig(
                keywords=document.preferences.keywords,
                location=document.preferences.location,
                posted_within_hours=document.preferences.posted_within_hours,
                workplace_types=document.preferences.workplace_types,
                seniority=document.preferences.seniority,
                easy_apply_only=document.preferences.easy_apply_only,
                minimum_score_threshold=document.preferences.minimum_score_threshold,
            ),
            agent=AgentConfig(
                schedule=ScheduleConfig(
                    frequency=document.schedule.frequency,
                    run_at=document.schedule.run_at,
                    timezone=document.schedule.timezone,
                ),
                auto_connect_with_recruiter=document.preferences.auto_connect_with_recruiter,
            ),
            ai=AIConfig(
                api_key=document.ai.api_key,
                model=document.ai.model,
            ),
            ruleset=RulesetConfig(
                version="ruleset-v1",
                allow_best_effort_autofill=True,
                auto_connect_with_recruiter=document.preferences.auto_connect_with_recruiter,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        msg = "Panel settings are incomplete for agent execution."
        raise PanelSettingsConfigurationError(msg) from exc


class AgentExecutionOrchestrator:
    """Coordinates one full execution run with structured events and logging."""

    def __init__(
        self,
        *,
        panel_store: LocalPanelSettingsStore,
        execution_store: ExecutionStore,
        successful_submission_store: SuccessfulSubmissionStore,
        submission_repository: SubmissionRepository | None = None,
        job_fetcher: JobFetcher | None = None,
        job_scorer: JobScorer | None = None,
        job_submitter: JobSubmitter | None = None,
        output_dir: Path | None = None,
        max_selected_jobs_per_run: int | None = None,
        test_minimum_score_threshold: float | None = None,
    ) -> None:
        self._panel_store = panel_store
        self._execution_store = execution_store
        self._successful_submission_store = successful_submission_store
        self._submission_repository = submission_repository
        self._job_fetcher = job_fetcher or EmptyJobFetcher()
        self._job_scorer = job_scorer or PassThroughJobScorer()
        self._job_submitter = job_submitter or NoOpJobSubmitter()
        self._output_dir = output_dir
        self._max_selected_jobs_per_run = (
            max(1, max_selected_jobs_per_run) if max_selected_jobs_per_run is not None else None
        )
        self._test_minimum_score_threshold = (
            min(1.0, max(0.0, test_minimum_score_threshold))
            if test_minimum_score_threshold is not None
            else None
        )

    async def run_execution(self, *, origin: ExecutionOrigin) -> ExecutionRunSummary:
        """Run one agent execution from config load to application attempts."""

        execution_id = uuid4()
        started_at = datetime.now().astimezone()
        if self._output_dir is not None:
            reset_run_output(
                self._output_dir,
                execution_id=execution_id,
                origin=origin.value,
                started_at=started_at,
            )

        with bind_execution_context(execution_id), bind_run_output(self._output_dir):
            return await self._run_execution_bound(
                execution_id=execution_id, started_at=started_at, origin=origin
            )

    async def _run_execution_bound(
        self,
        *,
        execution_id: UUID,
        started_at: datetime,
        origin: ExecutionOrigin,
    ) -> ExecutionRunSummary:
        """Run one agent execution with a bound structured logging context."""

        try:
            settings = build_user_agent_settings(self._panel_store.load())
        except PanelSettingsConfigurationError as exc:
            update_progress_snapshot(
                {
                    "status": AgentExecutionStatus.FAILED.value,
                    "current_stage": "load_config",
                    "current_job": None,
                    "current_step": None,
                    "last_error": str(exc),
                    "error_count": 1,
                },
            )
            append_timeline_event(
                "config_load_failed",
                {
                    "execution_id": str(execution_id),
                    "message": str(exc),
                },
            )
            summary = ExecutionRunSummary(
                execution_id=execution_id,
                origin=origin,
                status=AgentExecutionStatus.FAILED,
                started_at=started_at,
                finished_at=datetime.now().astimezone(),
                last_error=str(exc),
                error_count=1,
            )
            self._execution_store.save_execution(summary)
            self._persist_run_summary(summary)
            self._emit_event(
                execution_id=execution_id,
                event_type=ExecutionEventType.EXECUTION_FAILED,
                payload={
                    "stage": "load_config",
                    "message": str(exc),
                },
            )
            logger.exception("agent_execution_failed", extra={"execution_id": str(execution_id)})
            return summary

        if self._test_minimum_score_threshold is not None:
            original_threshold = settings.search.minimum_score_threshold
            settings = settings.model_copy(
                update={
                    "search": settings.search.model_copy(
                        update={
                            "minimum_score_threshold": self._test_minimum_score_threshold,
                        }
                    )
                }
            )
            append_timeline_event(
                "test_mode_score_threshold_override_applied",
                {
                    "execution_id": str(execution_id),
                    "original_threshold": original_threshold,
                    "effective_threshold": self._test_minimum_score_threshold,
                },
            )
            append_output_jsonl(
                "run.log",
                {
                    "source": "agent_execution",
                    "kind": "test_mode_score_threshold_override_applied",
                    "execution_id": str(execution_id),
                    "original_threshold": original_threshold,
                    "effective_threshold": self._test_minimum_score_threshold,
                },
            )

        update_progress_snapshot(
            {
                "status": "running",
                "current_stage": "config_loaded",
                "current_job": None,
                "current_step": None,
            },
        )
        append_timeline_event("config_loaded", {"execution_id": str(execution_id)})

        snapshot = build_profile_snapshot(settings)
        summary = ExecutionRunSummary(
            execution_id=execution_id,
            origin=origin,
            status=AgentExecutionStatus.RUNNING,
            started_at=started_at,
            snapshot_id=snapshot.id,
        )
        self._execution_store.save_execution(summary)
        self._persist_run_settings(settings)
        self._persist_run_summary(summary)
        self._emit_event(
            execution_id=execution_id,
            event_type=ExecutionEventType.EXECUTION_STARTED,
            payload={
                "origin": origin.value,
                "snapshot_id": str(snapshot.id),
                "schedule": settings.agent.schedule.model_dump(mode="json"),
            },
        )
        logger.info("agent_execution_started", extra={"execution_id": str(execution_id)})
        update_progress_snapshot(
            {
                "status": "running",
                "current_stage": "fetch_jobs",
                "current_job": None,
                "current_step": None,
                "jobs_seen": 0,
                "jobs_selected": 0,
                "successful_submissions": 0,
                "error_count": 0,
            },
        )
        append_timeline_event("fetch_jobs_started")
        latest_error: str | None = None
        error_count = 0
        jobs: list[JobPosting] = []
        jobs_seen = 0
        jobs_selected = 0
        successful_submissions = 0
        fetch_stage_emitted = False

        async def process_posting(posting: JobPosting) -> bool:
            nonlocal summary, latest_error, error_count, jobs_selected, successful_submissions

            current_job = {
                "job_posting_id": str(posting.id),
                "company_name": posting.company_name,
                "title": posting.title,
                "url": posting.url,
            }
            update_progress_snapshot(
                {
                    "status": "running",
                    "current_stage": "score_job",
                    "current_job": current_job,
                    "jobs_seen": jobs_seen,
                    "jobs_selected": jobs_selected,
                    "successful_submissions": successful_submissions,
                    "error_count": error_count,
                },
            )
            append_timeline_event("score_job_started", current_job)
            try:
                scored_job = await self._job_scorer.score(settings, posting)
            except Exception as exc:  # noqa: BLE001
                latest_error = str(exc)
                error_count += 1
                summary = self._persist_running_summary(
                    summary,
                    jobs_selected=jobs_selected,
                    successful_submissions=successful_submissions,
                    error_count=error_count,
                    last_error=latest_error,
                )
                self._emit_event(
                    execution_id=execution_id,
                    event_type=ExecutionEventType.EXECUTION_FAILED,
                    payload={
                        "stage": "score_job",
                        "job_posting_id": str(posting.id),
                        "message": str(exc),
                    },
                )
                logger.exception(
                    "agent_execution_score_error",
                    extra={"execution_id": str(execution_id), "job_posting_id": str(posting.id)},
                )
                update_progress_snapshot(
                    {
                        "status": "running",
                        "current_stage": "score_job_failed",
                        "current_job": current_job,
                        "last_error": latest_error,
                        "jobs_seen": jobs_seen,
                        "jobs_selected": jobs_selected,
                        "successful_submissions": successful_submissions,
                        "error_count": error_count,
                    },
                )
                return not self._emit_selected_job_limit_if_needed(
                    execution_id=execution_id,
                    jobs=jobs_seen,
                    jobs_selected=jobs_selected,
                    successful_submissions=successful_submissions,
                    error_count=error_count,
                )

            if not scored_job.selected:
                self._emit_event(
                    execution_id=execution_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "score_rejected",
                        "job_posting_id": str(posting.id),
                        "reason": scored_job.reason,
                        "score": scored_job.score,
                    },
                )
                update_progress_snapshot(
                    {
                        "status": "running",
                        "current_stage": "score_rejected",
                        "current_job": {**current_job, "score": scored_job.score},
                        "jobs_seen": jobs_seen,
                        "jobs_selected": jobs_selected,
                        "successful_submissions": successful_submissions,
                        "error_count": error_count,
                    },
                )
                return True

            jobs_selected += 1
            summary = self._persist_running_summary(
                summary,
                jobs_selected=jobs_selected,
                successful_submissions=successful_submissions,
                error_count=error_count,
                last_error=latest_error,
            )
            update_progress_snapshot(
                {
                    "status": "running",
                    "current_stage": "submit_job",
                    "current_job": {**current_job, "score": scored_job.score},
                    "jobs_seen": jobs_seen,
                    "jobs_selected": jobs_selected,
                    "successful_submissions": successful_submissions,
                    "error_count": error_count,
                },
            )
            append_timeline_event("submit_job_started", {**current_job, "score": scored_job.score})

            existing_submission = self._find_existing_successful_submission(posting)
            if existing_submission is not None:
                skip_notes = (
                    "A successful application for this job posting already exists in the audit "
                    "history."
                )
                self._emit_event(
                    execution_id=execution_id,
                    submission_id=existing_submission.id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "submit_skipped",
                        "reason": "already_applied",
                        "job_posting_id": str(posting.id),
                        "submission_id": str(existing_submission.id),
                        "notes": skip_notes,
                    },
                )
                self._emit_event(
                    execution_id=execution_id,
                    submission_id=existing_submission.id,
                    event_type=ExecutionEventType.JOB_PROCESSED,
                    payload={
                        "job_posting_id": str(posting.id),
                        "status": SubmissionStatus.SKIPPED.value,
                        "submission_id": str(existing_submission.id),
                        "reason": "already_applied",
                    },
                )
                update_progress_snapshot(
                    {
                        "status": "running",
                        "current_stage": "submit_skipped",
                        "current_job": {
                            **current_job,
                            "submission_id": str(existing_submission.id),
                            "skip_reason": "already_applied",
                        },
                        "jobs_seen": jobs_seen,
                        "jobs_selected": jobs_selected,
                        "successful_submissions": successful_submissions,
                        "error_count": error_count,
                    },
                )
                append_timeline_event(
                    "submit_skipped",
                    {
                        **current_job,
                        "submission_id": str(existing_submission.id),
                        "reason": "already_applied",
                    },
                )
                return not self._emit_selected_job_limit_if_needed(
                    execution_id=execution_id,
                    jobs=jobs_seen,
                    jobs_selected=jobs_selected,
                    successful_submissions=successful_submissions,
                    error_count=error_count,
                )

            try:
                attempt = await self._job_submitter.submit(
                    settings,
                    posting,
                    execution_id=execution_id,
                    origin=origin,
                )
            except Exception as exc:  # noqa: BLE001
                latest_error = str(exc)
                error_count += 1
                summary = self._persist_running_summary(
                    summary,
                    jobs_selected=jobs_selected,
                    successful_submissions=successful_submissions,
                    error_count=error_count,
                    last_error=latest_error,
                )
                self._emit_event(
                    execution_id=execution_id,
                    event_type=ExecutionEventType.EXECUTION_FAILED,
                    payload={
                        "stage": "submit_job",
                        "job_posting_id": str(posting.id),
                        "message": str(exc),
                    },
                )
                logger.exception(
                    "agent_execution_submit_error",
                    extra={"execution_id": str(execution_id), "job_posting_id": str(posting.id)},
                )
                update_progress_snapshot(
                    {
                        "status": "running",
                        "current_stage": "submit_job_failed",
                        "current_job": current_job,
                        "last_error": latest_error,
                        "jobs_seen": jobs_seen,
                        "jobs_selected": jobs_selected,
                        "successful_submissions": successful_submissions,
                        "error_count": error_count,
                    },
                )
                return not self._emit_selected_job_limit_if_needed(
                    execution_id=execution_id,
                    jobs=jobs_seen,
                    jobs_selected=jobs_selected,
                    successful_submissions=successful_submissions,
                    error_count=error_count,
                )
            submission = attempt.submission

            if submission.status is SubmissionStatus.SKIPPED:
                self._emit_event(
                    execution_id=execution_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "submit_skipped",
                        "job_posting_id": str(posting.id),
                        "submission_id": str(submission.id),
                        "notes": submission.notes,
                    },
                )
                self._emit_event(
                    execution_id=execution_id,
                    event_type=ExecutionEventType.JOB_PROCESSED,
                    payload={
                        "job_posting_id": str(posting.id),
                        "status": submission.status.value,
                        "submission_id": str(submission.id),
                    },
                )
                update_progress_snapshot(
                    {
                        "status": "running",
                        "current_stage": "submit_skipped",
                        "current_job": {**current_job, "submission_id": str(submission.id)},
                        "jobs_seen": jobs_seen,
                        "jobs_selected": jobs_selected,
                        "successful_submissions": successful_submissions,
                        "error_count": error_count,
                    },
                )
                return not self._emit_selected_job_limit_if_needed(
                    execution_id=execution_id,
                    jobs=jobs_seen,
                    jobs_selected=jobs_selected,
                    successful_submissions=successful_submissions,
                    error_count=error_count,
                )

            if submission.status is SubmissionStatus.FAILED:
                latest_error = submission.notes or "Submission failed."
                error_count += 1
                summary = self._persist_running_summary(
                    summary,
                    jobs_selected=jobs_selected,
                    successful_submissions=successful_submissions,
                    error_count=error_count,
                    last_error=latest_error,
                )
                self._emit_event(
                    execution_id=execution_id,
                    event_type=ExecutionEventType.EXECUTION_FAILED,
                    payload={
                        "stage": "submit_job",
                        "job_posting_id": str(posting.id),
                        "submission_id": str(submission.id),
                        "message": latest_error,
                    },
                )
                self._emit_event(
                    execution_id=execution_id,
                    event_type=ExecutionEventType.JOB_PROCESSED,
                    payload={
                        "job_posting_id": str(posting.id),
                        "status": submission.status.value,
                        "submission_id": str(submission.id),
                    },
                )
                update_progress_snapshot(
                    {
                        "status": "running",
                        "current_stage": "submit_failed",
                        "current_job": {**current_job, "submission_id": str(submission.id)},
                        "last_error": latest_error,
                        "jobs_seen": jobs_seen,
                        "jobs_selected": jobs_selected,
                        "successful_submissions": successful_submissions,
                        "error_count": error_count,
                    },
                )
                return not self._emit_selected_job_limit_if_needed(
                    execution_id=execution_id,
                    jobs=jobs_seen,
                    jobs_selected=jobs_selected,
                    successful_submissions=successful_submissions,
                    error_count=error_count,
                )

            record = attempt.successful_record or create_successful_submission_record(
                submission,
                settings=settings,
            )
            self._successful_submission_store.save(record)
            successful_submissions += 1
            summary = self._persist_running_summary(
                summary,
                jobs_selected=jobs_selected,
                successful_submissions=successful_submissions,
                error_count=error_count,
                last_error=latest_error,
            )
            self._emit_event(
                execution_id=execution_id,
                submission_id=record.submission.id,
                event_type=ExecutionEventType.SUBMISSION_COMPLETED,
                payload={
                    "job_posting_id": str(posting.id),
                    "company_name": posting.company_name,
                    "title": posting.title,
                },
            )
            self._emit_event(
                execution_id=execution_id,
                submission_id=record.submission.id,
                event_type=ExecutionEventType.JOB_PROCESSED,
                payload={
                    "job_posting_id": str(posting.id),
                    "status": record.submission.status.value,
                    "company_name": posting.company_name,
                    "title": posting.title,
                },
            )
            update_progress_snapshot(
                {
                    "status": "running",
                    "current_stage": "submission_completed",
                    "current_job": {**current_job, "submission_id": str(record.submission.id)},
                    "jobs_seen": jobs_seen,
                    "jobs_selected": jobs_selected,
                    "successful_submissions": successful_submissions,
                    "error_count": error_count,
                },
            )
            return not self._emit_selected_job_limit_if_needed(
                execution_id=execution_id,
                jobs=jobs_seen,
                jobs_selected=jobs_selected,
                successful_submissions=successful_submissions,
                error_count=error_count,
            )

        if isinstance(self._job_fetcher, IncrementalJobFetcher):
            summary = summary.model_copy(update={"jobs_seen": 0})
            self._execution_store.save_execution(summary)
            self._persist_run_summary(summary)

            async def process_incremental_job(posting: JobPosting) -> bool:
                nonlocal summary, jobs_seen

                jobs_seen += 1
                summary = summary.model_copy(update={"jobs_seen": jobs_seen})
                self._execution_store.save_execution(summary)
                self._persist_run_summary(summary)
                update_progress_snapshot(
                    {
                        "status": "running",
                        "current_stage": "job_fetched",
                        "current_job": {
                            "job_posting_id": str(posting.id),
                            "company_name": posting.company_name,
                            "title": posting.title,
                            "url": posting.url,
                        },
                        "jobs_seen": jobs_seen,
                        "jobs_selected": jobs_selected,
                        "successful_submissions": successful_submissions,
                        "error_count": error_count,
                    },
                )
                append_timeline_event(
                    "job_fetched",
                    {
                        "job_posting_id": str(posting.id),
                        "company_name": posting.company_name,
                        "title": posting.title,
                        "url": posting.url,
                        "jobs_seen": jobs_seen,
                    },
                )
                return await process_posting(posting)

            try:
                jobs_seen = await self._job_fetcher.fetch_incremental(
                    settings,
                    process_incremental_job,
                )
            except Exception as exc:  # noqa: BLE001
                return self._finalize_fatal_error(
                    summary=summary.model_copy(update={"jobs_seen": jobs_seen}),
                    snapshot=snapshot,
                    stage="fetch_jobs",
                    error=exc,
                )
        else:
            try:
                jobs = await self._job_fetcher.fetch(settings)
            except Exception as exc:  # noqa: BLE001
                return self._finalize_fatal_error(
                    summary=summary,
                    snapshot=snapshot,
                    stage="fetch_jobs",
                    error=exc,
                )

            jobs_seen = len(jobs)
            summary = summary.model_copy(update={"jobs_seen": jobs_seen})
            self._execution_store.save_execution(summary)
            self._persist_run_summary(summary)
            self._emit_event(
                execution_id=execution_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "fetch_jobs",
                    "jobs_seen": jobs_seen,
                },
            )
            update_progress_snapshot(
                {
                    "status": "running",
                    "current_stage": "jobs_fetched",
                    "jobs_seen": jobs_seen,
                    "jobs_selected": jobs_selected,
                    "successful_submissions": successful_submissions,
                    "error_count": error_count,
                    "current_job": None,
                },
            )
            append_timeline_event(
                "jobs_fetched",
                {
                    "execution_id": str(execution_id),
                    "jobs_seen": jobs_seen,
                },
            )
            fetch_stage_emitted = True
            for posting in jobs:
                should_continue = await process_posting(posting)
                if not should_continue:
                    break

        if not fetch_stage_emitted:
            self._emit_event(
                execution_id=execution_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "fetch_jobs",
                    "jobs_seen": jobs_seen,
                },
            )
            update_progress_snapshot(
                {
                    "status": "running",
                    "current_stage": "jobs_fetched",
                    "jobs_seen": jobs_seen,
                    "jobs_selected": jobs_selected,
                    "successful_submissions": successful_submissions,
                    "error_count": error_count,
                    "current_job": None,
                },
            )
            append_timeline_event(
                "jobs_fetched",
                {
                    "execution_id": str(execution_id),
                    "jobs_seen": jobs_seen,
                },
            )

        final_summary = summary.model_copy(
            update={
                "status": AgentExecutionStatus.COMPLETED,
                "finished_at": datetime.now().astimezone(),
                "jobs_selected": jobs_selected,
                "successful_submissions": successful_submissions,
                "error_count": error_count,
                "last_error": latest_error,
            },
        )
        self._execution_store.save_execution(final_summary)
        self._persist_run_summary(final_summary)
        self._emit_event(
            execution_id=execution_id,
            event_type=ExecutionEventType.EXECUTION_COMPLETED,
            payload={
                "jobs_seen": final_summary.jobs_seen,
                "jobs_selected": final_summary.jobs_selected,
                "successful_submissions": final_summary.successful_submissions,
                "error_count": final_summary.error_count,
            },
        )
        logger.info(
            "agent_execution_completed",
            extra={
                "execution_id": str(execution_id),
                "jobs_seen": final_summary.jobs_seen,
                "jobs_selected": final_summary.jobs_selected,
                "successful_submissions": final_summary.successful_submissions,
                "error_count": final_summary.error_count,
            },
        )
        update_progress_snapshot(
            {
                "status": final_summary.status.value,
                "current_stage": "execution_completed",
                "current_job": None,
                "current_step": None,
                "jobs_seen": final_summary.jobs_seen,
                "jobs_selected": final_summary.jobs_selected,
                "successful_submissions": final_summary.successful_submissions,
                "error_count": final_summary.error_count,
                "last_error": final_summary.last_error,
            },
        )
        return final_summary

    def _find_existing_successful_submission(
        self,
        posting: JobPosting,
    ) -> ApplicationSubmission | None:
        if self._submission_repository is None:
            return None
        return self._submission_repository.find_latest_successful_for_job_posting(posting.id)

    def list_recent_executions(self, *, limit: int = 10) -> list[ExecutionRunSummary]:
        """Return recent execution summaries."""

        return self._execution_store.list_recent_executions(limit=limit)

    def _finalize_fatal_error(
        self,
        *,
        summary: ExecutionRunSummary,
        snapshot: ProfileSnapshot,
        stage: str,
        error: Exception,
    ) -> ExecutionRunSummary:
        message = str(error)
        failed_summary = summary.model_copy(
            update={
                "status": AgentExecutionStatus.FAILED,
                "finished_at": datetime.now().astimezone(),
                "snapshot_id": snapshot.id,
                "error_count": 1,
                "last_error": message,
            },
        )
        self._execution_store.save_execution(failed_summary)
        self._persist_run_summary(failed_summary)
        self._emit_event(
            execution_id=summary.execution_id,
            event_type=ExecutionEventType.EXECUTION_FAILED,
            payload={
                "stage": stage,
                "message": message,
            },
        )
        update_progress_snapshot(
            {
                "status": failed_summary.status.value,
                "current_stage": stage,
                "current_job": None,
                "current_step": None,
                "last_error": message,
                "error_count": failed_summary.error_count,
            },
        )
        append_timeline_event(
            "execution_failed",
            {
                "execution_id": str(summary.execution_id),
                "stage": stage,
                "message": message,
            },
        )
        logger.exception(
            "agent_execution_fatal_error",
            extra={"execution_id": str(summary.execution_id), "stage": stage},
        )
        return failed_summary

    def _emit_selected_job_limit_if_needed(
        self,
        *,
        execution_id: UUID,
        jobs: int,
        jobs_selected: int,
        successful_submissions: int,
        error_count: int,
    ) -> bool:
        if (
            self._max_selected_jobs_per_run is None
            or jobs_selected < self._max_selected_jobs_per_run
        ):
            return False
        self._emit_event(
            execution_id=execution_id,
            event_type=ExecutionEventType.STEP_REACHED,
            payload={
                "stage": "selected_job_limit_reached",
                "jobs_selected": jobs_selected,
                "max_selected_jobs_per_run": self._max_selected_jobs_per_run,
            },
        )
        update_progress_snapshot(
            {
                "status": "running",
                "current_stage": "selected_job_limit_reached",
                "jobs_seen": jobs,
                "jobs_selected": jobs_selected,
                "successful_submissions": successful_submissions,
                "error_count": error_count,
                "current_job": None,
            },
        )
        append_timeline_event(
            "selected_job_limit_reached",
            {
                "execution_id": str(execution_id),
                "jobs_selected": jobs_selected,
                "max_selected_jobs_per_run": self._max_selected_jobs_per_run,
            },
        )
        return True

    def _emit_event(
        self,
        *,
        execution_id: UUID,
        event_type: ExecutionEventType,
        payload: dict[str, object],
        submission_id: UUID | None = None,
    ) -> None:
        event = ExecutionEvent(
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=event_type,
            payload_json=json.dumps(payload, sort_keys=True),
        )
        self._execution_store.append_event(event)
        if self._output_dir is not None:
            append_output_jsonl(
                "run.log",
                {
                    "source": "agent_execution",
                    "id": str(event.id),
                    "event_type": event.event_type.value,
                    "execution_id": str(event.execution_id),
                    "submission_id": str(event.submission_id) if event.submission_id else None,
                    "timestamp": event.timestamp.isoformat(),
                    "payload": payload,
                },
            )
            append_timeline_event(
                event.event_type.value,
                {
                    "id": str(event.id),
                    "execution_id": str(event.execution_id),
                    "submission_id": str(event.submission_id) if event.submission_id else None,
                    "payload": payload,
                },
            )

    def _persist_run_settings(self, settings: UserAgentSettings) -> None:
        if self._output_dir is None:
            return
        write_output_json(
            "settings-summary.json",
            {
                "config_version": settings.config_version,
                "profile": {
                    "email": settings.profile.email,
                    "city": settings.profile.city,
                    "has_cv_path": settings.profile.cv_path is not None,
                    "positive_filters": list(settings.profile.positive_filters),
                    "blacklist": list(settings.profile.blacklist),
                },
                "search": settings.search.model_dump(mode="json"),
                "agent": {
                    "schedule": settings.agent.schedule.model_dump(mode="json"),
                    "auto_connect_with_recruiter": settings.agent.auto_connect_with_recruiter,
                },
                "ai": {
                    "model": settings.ai.model,
                    "has_api_key": settings.ai.api_key is not None,
                },
                "ruleset": settings.ruleset.model_dump(mode="json"),
            },
        )

    def _persist_run_summary(self, summary: ExecutionRunSummary) -> None:
        if self._output_dir is None:
            return
        write_output_json("summary.json", summary.model_dump(mode="json"))

    def _persist_running_summary(
        self,
        summary: ExecutionRunSummary,
        *,
        jobs_selected: int,
        successful_submissions: int,
        error_count: int,
        last_error: str | None,
    ) -> ExecutionRunSummary:
        running_summary = summary.model_copy(
            update={
                "status": AgentExecutionStatus.RUNNING,
                "jobs_selected": jobs_selected,
                "successful_submissions": successful_submissions,
                "error_count": error_count,
                "last_error": last_error,
            },
        )
        self._execution_store.save_execution(running_summary)
        self._persist_run_summary(running_summary)
        return running_summary
