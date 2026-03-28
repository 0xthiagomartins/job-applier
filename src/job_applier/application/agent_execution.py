"""Execution orchestration for scheduled and manual agent runs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
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
from job_applier.application.snapshotting import (
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
from job_applier.infrastructure.in_memory.audit_store import InMemorySuccessfulSubmissionStore
from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore

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
        origin: ExecutionOrigin,
    ) -> ApplicationSubmission:
        """Attempt to apply to the selected job posting."""


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
        origin: ExecutionOrigin,
    ) -> ApplicationSubmission:
        logger.info(
            "job_submit_stage",
            extra={
                "job_posting_id": str(posting.id),
                "origin": origin.value,
                "auto_connect_with_recruiter": settings.agent.auto_connect_with_recruiter,
            },
        )
        return ApplicationSubmission(
            job_posting_id=posting.id,
            status=SubmissionStatus.SKIPPED,
            execution_origin=origin,
            notes="Application submitter is not configured yet.",
        )


def build_user_agent_settings(document: PanelSettingsDocument) -> UserAgentSettings:
    """Build validated execution settings from the persisted panel document."""

    missing_fields = [
        field_name
        for field_name, field_value in (
            ("profile.email", document.profile.email),
            ("profile.linkedin_url", document.profile.linkedin_url),
        )
        if field_value is None
    ]
    if missing_fields:
        joined = ", ".join(missing_fields)
        msg = f"Missing required panel fields: {joined}."
        raise PanelSettingsConfigurationError(msg)

    if document.ai.api_key is None:
        msg = "AI API key is required before the agent can run."
        raise PanelSettingsConfigurationError(msg)

    email = document.profile.email
    linkedin_url = document.profile.linkedin_url
    assert email is not None
    assert linkedin_url is not None

    try:
        return UserAgentSettings(
            config_version="config-v1",
            profile=UserProfileConfig(
                name=document.profile.name,
                email=email,
                phone=document.profile.phone,
                city=document.profile.city,
                linkedin_url=linkedin_url,
                github_url=document.profile.github_url,
                portfolio_url=document.profile.portfolio_url,
                years_experience_by_stack=document.profile.years_experience_by_stack,
                work_authorized=document.profile.work_authorized,
                needs_sponsorship=document.profile.needs_sponsorship,
                salary_expectation=document.profile.salary_expectation,
                availability=document.profile.availability,
                default_responses=document.profile.default_responses,
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
        successful_submission_store: InMemorySuccessfulSubmissionStore,
        job_fetcher: JobFetcher | None = None,
        job_scorer: JobScorer | None = None,
        job_submitter: JobSubmitter | None = None,
    ) -> None:
        self._panel_store = panel_store
        self._execution_store = execution_store
        self._successful_submission_store = successful_submission_store
        self._job_fetcher = job_fetcher or EmptyJobFetcher()
        self._job_scorer = job_scorer or PassThroughJobScorer()
        self._job_submitter = job_submitter or NoOpJobSubmitter()

    async def run_execution(self, *, origin: ExecutionOrigin) -> ExecutionRunSummary:
        """Run one agent execution from config load to application attempts."""

        execution_id = uuid4()
        started_at = datetime.now().astimezone()

        try:
            settings = build_user_agent_settings(self._panel_store.load())
        except PanelSettingsConfigurationError as exc:
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

        snapshot = build_profile_snapshot(settings)
        summary = ExecutionRunSummary(
            execution_id=execution_id,
            origin=origin,
            status=AgentExecutionStatus.RUNNING,
            started_at=started_at,
            snapshot_id=snapshot.id,
        )
        self._execution_store.save_execution(summary)
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

        try:
            jobs = await self._job_fetcher.fetch(settings)
        except Exception as exc:  # noqa: BLE001
            return self._finalize_fatal_error(
                summary=summary,
                snapshot=snapshot,
                stage="fetch_jobs",
                error=exc,
            )

        summary = summary.model_copy(update={"jobs_seen": len(jobs)})
        self._execution_store.save_execution(summary)
        self._emit_event(
            execution_id=execution_id,
            event_type=ExecutionEventType.STEP_REACHED,
            payload={
                "stage": "fetch_jobs",
                "jobs_seen": len(jobs),
            },
        )

        latest_error: str | None = None
        error_count = 0
        jobs_selected = 0
        successful_submissions = 0

        for posting in jobs:
            try:
                scored_job = await self._job_scorer.score(settings, posting)
            except Exception as exc:  # noqa: BLE001
                latest_error = str(exc)
                error_count += 1
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
                continue

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
                continue

            jobs_selected += 1

            try:
                submission = await self._job_submitter.submit(settings, posting, origin=origin)
            except Exception as exc:  # noqa: BLE001
                latest_error = str(exc)
                error_count += 1
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
                continue

            if submission.status is SubmissionStatus.SKIPPED:
                self._emit_event(
                    execution_id=execution_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    submission_id=submission.id,
                    payload={
                        "stage": "submit_skipped",
                        "job_posting_id": str(posting.id),
                        "notes": submission.notes,
                    },
                )
                continue

            if submission.status is SubmissionStatus.FAILED:
                latest_error = submission.notes or "Submission failed."
                error_count += 1
                self._emit_event(
                    execution_id=execution_id,
                    event_type=ExecutionEventType.EXECUTION_FAILED,
                    submission_id=submission.id,
                    payload={
                        "stage": "submit_job",
                        "job_posting_id": str(posting.id),
                        "message": latest_error,
                    },
                )
                continue

            record = create_successful_submission_record(submission, settings=settings)
            self._successful_submission_store.save(record)
            successful_submissions += 1
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
        return final_summary

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
        self._emit_event(
            execution_id=summary.execution_id,
            event_type=ExecutionEventType.EXECUTION_FAILED,
            payload={
                "stage": stage,
                "message": message,
            },
        )
        logger.exception(
            "agent_execution_fatal_error",
            extra={"execution_id": str(summary.execution_id), "stage": stage},
        )
        return failed_summary

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
