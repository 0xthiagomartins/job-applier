"""LinkedIn Easy Apply automation and submission persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import traceback
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from playwright.async_api import BrowserContext, Locator, Page, async_playwright

from job_applier.application.agent_execution import JobSubmitter, SubmissionAttempt
from job_applier.application.config import UserAgentSettings
from job_applier.application.repositories import (
    AnswerRepository,
    ArtifactSnapshotRepository,
    ExecutionEventRepository,
    ProfileSnapshotRepository,
    RecruiterInteractionRepository,
    SubmissionRepository,
)
from job_applier.application.snapshotting import (
    SuccessfulSubmissionRecord,
    create_successful_submission_record,
)
from job_applier.domain.entities import (
    ApplicationAnswer,
    ApplicationSubmission,
    ArtifactSnapshot,
    ExecutionEvent,
    JobPosting,
    RecruiterInteraction,
    utc_now,
)
from job_applier.domain.enums import (
    ArtifactType,
    ExecutionEventType,
    ExecutionOrigin,
    QuestionType,
    SubmissionStatus,
)
from job_applier.infrastructure.linkedin.auth import (
    LinkedInAuthError,
    LinkedInCredentials,
    LinkedInSessionManager,
)
from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAgentAction,
    BrowserAutomationError,
    BrowserDomSnapshotter,
    BrowserTaskAssessment,
    OpenAIResponsesBrowserAgent,
)
from job_applier.infrastructure.linkedin.question_resolution import (
    EasyApplyField,
    LinkedInAnswerResolver,
    LinkedInQuestionExtractor,
    ResolvedFieldValue,
    normalize_text,
)
from job_applier.infrastructure.linkedin.recruiter_connect import (
    LinkedInRecruiterCandidateFinder,
    PlaywrightRecruiterConnector,
)
from job_applier.observability import bind_submission_context
from job_applier.settings import RuntimeSettings

logger = logging.getLogger(__name__)


class LinkedInEasyApplyError(RuntimeError):
    """Raised when the Easy Apply execution cannot continue."""


@dataclass(frozen=True, slots=True)
class EasyApplyStep:
    """Current Easy Apply step metadata and discovered controls."""

    step_index: int
    total_steps: int
    fields: tuple[EasyApplyField, ...]


@dataclass(frozen=True, slots=True)
class EasyApplyExecutionResult:
    """Structured result produced by the Playwright Easy Apply executor."""

    submission_id: UUID
    started_at: datetime
    status: SubmissionStatus
    notes: str | None = None
    answers: tuple[ApplicationAnswer, ...] = ()
    execution_events: tuple[ExecutionEvent, ...] = ()
    artifacts: tuple[ArtifactSnapshot, ...] = ()
    recruiter_interactions: tuple[RecruiterInteraction, ...] = ()
    submitted_at: datetime | None = None
    cv_version: str | None = None
    cover_letter_version: str | None = None

    def __post_init__(self) -> None:
        if self.status is SubmissionStatus.SUBMITTED and self.submitted_at is None:
            msg = "submitted executions require submitted_at"
            raise ValueError(msg)
        if self.status is not SubmissionStatus.SUBMITTED and self.submitted_at is not None:
            msg = "submitted_at can only be set for submitted executions"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TextFieldInteractionState:
    """Interactive state for a filled text-like control."""

    current_value: str
    focused: bool
    role: str | None
    aria_autocomplete: str | None
    aria_expanded: bool
    has_popup_binding: bool
    active_descendant: str | None
    visible_option_count: int
    visible_option_texts: tuple[str, ...] = ()
    invalid: bool = False
    validation_message: str | None = None

    @property
    def has_value(self) -> bool:
        return bool(normalize_text(self.current_value))

    @property
    def needs_agentic_follow_up(self) -> bool:
        return bool(
            self.visible_option_count
            or self.aria_expanded
            or self.active_descendant
            or self.invalid
            or (
                self.focused
                and (
                    self.role == "combobox"
                    or self.aria_autocomplete is not None
                    or self.has_popup_binding
                )
            )
        )


class EasyApplyExecutor(Protocol):
    """Boundary used by the submitter to run the browser automation."""

    async def execute(
        self,
        settings: UserAgentSettings,
        posting: JobPosting,
        *,
        execution_id: UUID,
        origin: ExecutionOrigin,
    ) -> EasyApplyExecutionResult:
        """Run the Easy Apply flow for one posting."""


class PlaywrightLinkedInEasyApplyExecutor:
    """Use Playwright to run the LinkedIn Easy Apply modal end to end."""

    def __init__(
        self,
        runtime_settings: RuntimeSettings,
        *,
        answer_resolver: LinkedInAnswerResolver | None = None,
        execution_event_repository: ExecutionEventRepository | None = None,
    ) -> None:
        self._runtime_settings = runtime_settings
        self._answer_resolver = answer_resolver or LinkedInAnswerResolver()
        self._question_extractor = LinkedInQuestionExtractor()
        self._recruiter_candidate_finder = LinkedInRecruiterCandidateFinder()
        self._recruiter_connector = PlaywrightRecruiterConnector(runtime_settings)
        self._execution_event_repository = execution_event_repository
        self._session_manager: LinkedInSessionManager | None = None

    async def execute(
        self,
        settings: UserAgentSettings,
        posting: JobPosting,
        *,
        execution_id: UUID,
        origin: ExecutionOrigin,
    ) -> EasyApplyExecutionResult:
        submission_id = uuid4()
        started_at = utc_now()
        run_dir = self._build_run_dir(posting, submission_id)
        answers: list[ApplicationAnswer] = []
        execution_events: list[ExecutionEvent] = []
        artifacts: list[ArtifactSnapshot] = []
        uploaded_cv_paths: set[str] = set()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self._runtime_settings.playwright_headless,
            )
            try:
                context = await self._create_session_manager(settings).create_authenticated_context(
                    browser
                )
                trace_started = await self._start_trace(context)
                try:
                    result = await self._execute_once(
                        context,
                        settings,
                        posting,
                        execution_id=execution_id,
                        origin=origin,
                        submission_id=submission_id,
                        started_at=started_at,
                        run_dir=run_dir,
                        answers=answers,
                        execution_events=execution_events,
                        artifacts=artifacts,
                        uploaded_cv_paths=uploaded_cv_paths,
                    )
                    trace_artifact = await self._stop_trace(
                        context,
                        trace_started=trace_started,
                        run_dir=run_dir,
                        submission_id=submission_id,
                        preserve=result.status is SubmissionStatus.FAILED
                        or self._runtime_settings.playwright_trace_enabled,
                    )
                    if trace_artifact is not None:
                        artifacts.append(trace_artifact)
                    return replace(
                        result,
                        execution_events=tuple(execution_events),
                        artifacts=tuple(artifacts),
                    )
                finally:
                    await context.close()
            finally:
                await browser.close()

    async def _execute_once(
        self,
        context: BrowserContext,
        settings: UserAgentSettings,
        posting: JobPosting,
        *,
        execution_id: UUID,
        origin: ExecutionOrigin,
        submission_id: UUID,
        started_at: datetime,
        run_dir: Path,
        answers: list[ApplicationAnswer],
        execution_events: list[ExecutionEvent],
        artifacts: list[ArtifactSnapshot],
        uploaded_cv_paths: set[str],
    ) -> EasyApplyExecutionResult:
        page = await context.new_page()
        page.set_default_timeout(self._runtime_settings.linkedin_default_timeout_ms)
        recruiter_interactions: list[RecruiterInteraction] = []

        try:
            with bind_submission_context(submission_id):
                await self._pause_before_navigation(page, reason="job_detail_open")
                await page.goto(posting.url, wait_until="domcontentloaded")
                await self._ensure_authenticated_page(page)
                artifacts.extend(
                    await self._capture_debug_bundle(
                        page,
                        run_dir=run_dir,
                        submission_id=submission_id,
                        label="job_opened",
                    ),
                )
                recruiter_candidate = await self._recruiter_candidate_finder.find(page, settings)

                try:
                    await self._open_easy_apply_modal_with_agent(
                        page,
                        settings=settings,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        execution_events=execution_events,
                    )
                except LinkedInEasyApplyError as exc:
                    notes = str(exc) or "Browser agent could not open the Easy Apply modal."
                    logger.info(
                        "linkedin_easy_apply_unavailable",
                        extra={
                            "job_posting_id": str(posting.id),
                            "origin": origin.value,
                            "notes": notes,
                        },
                    )
                    artifacts.extend(
                        await self._capture_debug_bundle(
                            page,
                            run_dir=run_dir,
                            submission_id=submission_id,
                            label="failure_open_easy_apply",
                        ),
                    )
                    self._record_event(
                        execution_events,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        event_type=ExecutionEventType.JOB_PROCESSED,
                        payload={
                            "job_posting_id": str(posting.id),
                            "origin": origin.value,
                            "reason": "easy_apply_modal_open_failed",
                            "status": SubmissionStatus.FAILED.value,
                            "notes": notes,
                        },
                    )
                    return EasyApplyExecutionResult(
                        submission_id=submission_id,
                        started_at=started_at,
                        status=SubmissionStatus.FAILED,
                        notes=notes,
                    )

                max_steps = 10
                last_known_step_index = 0
                last_known_total_steps = 1
                for _ in range(max_steps):
                    step = await self._extract_step(
                        page,
                        last_known_step_index=last_known_step_index,
                        last_known_total_steps=last_known_total_steps,
                    )
                    last_known_step_index = step.step_index
                    last_known_total_steps = step.total_steps
                    self._record_event(
                        execution_events,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        event_type=ExecutionEventType.STEP_REACHED,
                        payload={
                            "stage": "easy_apply_step",
                            "job_posting_id": str(posting.id),
                            "step_index": step.step_index,
                            "total_steps": step.total_steps,
                            "field_count": len(step.fields),
                        },
                    )
                    logger.info(
                        "linkedin_easy_apply_step",
                        extra={
                            "job_posting_id": str(posting.id),
                            "step_index": step.step_index,
                            "total_steps": step.total_steps,
                            "field_count": len(step.fields),
                        },
                    )
                    artifacts.extend(
                        await self._capture_debug_bundle(
                            page,
                            run_dir=run_dir,
                            submission_id=submission_id,
                            label=f"step_{step.step_index + 1:02d}",
                        ),
                    )
                    step_answers, step_artifacts = await self._fill_step_fields(
                        page,
                        step,
                        settings,
                        posting=posting,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        execution_events=execution_events,
                        uploaded_cv_paths=uploaded_cv_paths,
                    )
                    answers.extend(step_answers)
                    artifacts.extend(step_artifacts)

                    try:
                        action = await self._progress_easy_apply_step_with_agent(
                            page,
                            settings=settings,
                            step=step,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            execution_events=execution_events,
                        )
                    except LinkedInEasyApplyError as exc:
                        notes = str(exc) or "Browser agent could not progress the Easy Apply flow."
                        artifacts.extend(
                            await self._capture_debug_bundle(
                                page,
                                run_dir=run_dir,
                                submission_id=submission_id,
                                label="failure_missing_action",
                            ),
                        )
                        self._record_event(
                            execution_events,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            event_type=ExecutionEventType.JOB_PROCESSED,
                            payload={
                                "job_posting_id": str(posting.id),
                                "reason": "agentic_primary_action_failed",
                                "status": SubmissionStatus.FAILED.value,
                                "notes": notes,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.FAILED,
                            notes=notes,
                        )

                    if action.action_intent == "submit_application":
                        self._record_event(
                            execution_events,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            event_type=ExecutionEventType.SUBMIT_TRIGGERED,
                            payload={
                                "job_posting_id": str(posting.id),
                                "step_index": step.step_index,
                                "reasoning": action.reasoning,
                            },
                        )
                        success, outcome_notes = await self._await_submission_outcome(
                            page,
                            settings=settings,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            execution_events=execution_events,
                            recent_actions=(
                                {
                                    "action_type": action.action_type,
                                    "action_intent": action.action_intent,
                                    "reasoning": action.reasoning,
                                },
                            ),
                        )
                        artifacts.extend(
                            await self._capture_debug_bundle(
                                page,
                                run_dir=run_dir,
                                submission_id=submission_id,
                                label="post_submit",
                            ),
                        )
                        if success:
                            if recruiter_candidate is not None:
                                self._record_event(
                                    execution_events,
                                    execution_id=execution_id,
                                    submission_id=submission_id,
                                    event_type=ExecutionEventType.RECRUITER_CONNECT_ATTEMPTED,
                                    payload={
                                        "job_posting_id": str(posting.id),
                                        "recruiter_name": recruiter_candidate.name,
                                        "recruiter_profile_url": recruiter_candidate.profile_url,
                                    },
                                )
                                try:
                                    recruiter_attempt = await self._recruiter_connector.connect(
                                        context,
                                        recruiter=recruiter_candidate,
                                        settings=settings,
                                        posting=posting,
                                        submission_id=submission_id,
                                        screenshot_path=self._artifact_path(
                                            run_dir,
                                            submission_id=submission_id,
                                            label="recruiter_connect",
                                            extension="png",
                                        ),
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    self._record_exception_event(
                                        execution_events,
                                        execution_id=execution_id,
                                        submission_id=submission_id,
                                        stage="recruiter_connect",
                                        error=exc,
                                    )
                                    logger.exception(
                                        "linkedin_recruiter_connect_error",
                                        extra={
                                            "job_posting_id": str(posting.id),
                                            "submission_id": str(submission_id),
                                        },
                                    )
                                else:
                                    recruiter_interactions.append(recruiter_attempt.interaction)
                                    if recruiter_attempt.screenshot_path is not None:
                                        artifacts.append(
                                            _build_file_artifact(
                                                submission_id=submission_id,
                                                path=recruiter_attempt.screenshot_path,
                                                artifact_type=ArtifactType.SCREENSHOT,
                                            ),
                                        )
                                    self._record_event(
                                        execution_events,
                                        execution_id=execution_id,
                                        submission_id=submission_id,
                                        event_type=ExecutionEventType.RECRUITER_CONNECT_ATTEMPTED,
                                        payload={
                                            "job_posting_id": str(posting.id),
                                            "recruiter_name": (
                                                recruiter_attempt.interaction.recruiter_name
                                            ),
                                            "status": recruiter_attempt.interaction.status.value,
                                        },
                                    )
                                    logger.info(
                                        "linkedin_recruiter_connect_result",
                                        extra={
                                            "job_posting_id": str(posting.id),
                                            "submission_id": str(submission_id),
                                            "status": recruiter_attempt.interaction.status.value,
                                            "recruiter_name": (
                                                recruiter_attempt.interaction.recruiter_name
                                            ),
                                        },
                                    )
                            self._record_event(
                                execution_events,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                event_type=ExecutionEventType.JOB_PROCESSED,
                                payload={
                                    "job_posting_id": str(posting.id),
                                    "status": SubmissionStatus.SUBMITTED.value,
                                },
                            )
                            return EasyApplyExecutionResult(
                                submission_id=submission_id,
                                started_at=started_at,
                                status=SubmissionStatus.SUBMITTED,
                                notes=outcome_notes
                                or "LinkedIn Easy Apply submitted successfully.",
                                answers=tuple(answers),
                                recruiter_interactions=tuple(recruiter_interactions),
                                submitted_at=utc_now(),
                                cv_version=settings.profile.cv_filename,
                            )
                        self._record_event(
                            execution_events,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            event_type=ExecutionEventType.JOB_PROCESSED,
                            payload={
                                "job_posting_id": str(posting.id),
                                "reason": "submit_not_confirmed",
                                "status": SubmissionStatus.FAILED.value,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.FAILED,
                            notes=outcome_notes or "LinkedIn did not confirm the application.",
                        )

                    assessment_status, assessment_notes = await self._assess_easy_apply_step_state(
                        page,
                        settings=settings,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        execution_events=execution_events,
                        step=step,
                        step_answers=tuple(step_answers),
                        recent_actions=(
                            {
                                "action_type": action.action_type,
                                "action_intent": action.action_intent,
                                "reasoning": action.reasoning,
                            },
                        ),
                    )
                    if assessment_status == "blocked":
                        artifacts.extend(
                            await self._capture_debug_bundle(
                                page,
                                run_dir=run_dir,
                                submission_id=submission_id,
                                label=f"failure_step_{step.step_index + 1:02d}",
                            ),
                        )
                        self._record_event(
                            execution_events,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            event_type=ExecutionEventType.JOB_PROCESSED,
                            payload={
                                "job_posting_id": str(posting.id),
                                "reason": "agentic_step_blocked",
                                "status": SubmissionStatus.FAILED.value,
                                "notes": assessment_notes,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.FAILED,
                            notes=assessment_notes,
                        )

                artifacts.extend(
                    await self._capture_debug_bundle(
                        page,
                        run_dir=run_dir,
                        submission_id=submission_id,
                        label="failure_max_steps",
                    ),
                )
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.JOB_PROCESSED,
                    payload={
                        "job_posting_id": str(posting.id),
                        "reason": "max_steps_exceeded",
                        "status": SubmissionStatus.FAILED.value,
                    },
                )
                return EasyApplyExecutionResult(
                    submission_id=submission_id,
                    started_at=started_at,
                    status=SubmissionStatus.FAILED,
                    notes="LinkedIn Easy Apply exceeded the maximum number of steps.",
                )
        except Exception as exc:  # noqa: BLE001
            self._record_exception_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                stage="easy_apply_execute",
                error=exc,
            )
            self._record_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=ExecutionEventType.JOB_PROCESSED,
                payload={
                    "job_posting_id": str(posting.id),
                    "reason": "unhandled_exception",
                    "status": SubmissionStatus.FAILED.value,
                },
            )
            if not page.is_closed():
                artifacts.extend(
                    await self._capture_debug_bundle(
                        page,
                        run_dir=run_dir,
                        submission_id=submission_id,
                        label="failure_exception",
                    ),
                )
            logger.exception(
                "linkedin_easy_apply_unhandled_error",
                extra={"job_posting_id": str(posting.id), "submission_id": str(submission_id)},
            )
            return EasyApplyExecutionResult(
                submission_id=submission_id,
                started_at=started_at,
                status=SubmissionStatus.FAILED,
                notes=str(exc),
            )
        finally:
            await page.close()

    async def _fill_step_fields(
        self,
        page: Page,
        step: EasyApplyStep,
        settings: UserAgentSettings,
        *,
        posting: JobPosting,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
        uploaded_cv_paths: set[str],
    ) -> tuple[list[ApplicationAnswer], list[ArtifactSnapshot]]:
        root = await self._easy_apply_root(page)
        answers: list[ApplicationAnswer] = []
        artifacts: list[ArtifactSnapshot] = []

        for field in step.fields:
            if field.question_type is QuestionType.UNKNOWN:
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.QUESTION_CLASSIFICATION_FAILED,
                    payload={
                        "step_index": step.step_index,
                        "question_raw": field.question_raw,
                        "normalized_key": field.normalized_key,
                        "control_kind": field.control_kind,
                        "classification_confidence": field.classification_confidence,
                    },
                )
            resolution = await self._answer_resolver.resolve(field, settings, posting=posting)
            if resolution is None:
                continue

            applied_value = await self._apply_field_value(
                page,
                root,
                field,
                resolution,
                settings,
            )
            if applied_value is None:
                continue

            if resolution.ambiguity_flag:
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.AUTOFILL_APPLIED,
                    payload={
                        "step_index": step.step_index,
                        "normalized_key": field.normalized_key,
                        "question_type": field.question_type.value,
                        "answer_source": resolution.answer_source.value,
                        "fill_strategy": resolution.fill_strategy.value,
                        "confidence": resolution.confidence,
                        "reasoning": resolution.reasoning,
                    },
                )

            logger.info(
                "linkedin_easy_apply_field_filled",
                extra={
                    "step_index": step.step_index,
                    "normalized_key": field.normalized_key,
                    "question_type": field.question_type.value,
                    "fill_strategy": resolution.fill_strategy.value,
                },
            )
            answers.append(
                ApplicationAnswer(
                    submission_id=submission_id,
                    step_index=step.step_index,
                    question_raw=field.question_raw,
                    question_type=field.question_type,
                    normalized_key=field.normalized_key,
                    answer_raw=applied_value,
                    answer_source=resolution.answer_source,
                    fill_strategy=resolution.fill_strategy,
                    ambiguity_flag=resolution.ambiguity_flag,
                ),
            )

            if field.question_type is QuestionType.RESUME_UPLOAD and settings.profile.cv_path:
                cv_path = settings.profile.cv_path
                if cv_path not in uploaded_cv_paths:
                    uploaded_cv_paths.add(cv_path)
                    artifacts.append(
                        _build_file_artifact(
                            submission_id=submission_id,
                            path=Path(cv_path),
                            artifact_type=ArtifactType.CV_METADATA,
                        ),
                    )

        return answers, artifacts

    async def _apply_field_value(
        self,
        page: Page,
        root: Locator,
        field: EasyApplyField,
        resolution: ResolvedFieldValue,
        settings: UserAgentSettings,
    ) -> str | None:
        match field.control_kind:
            case "text" | "textarea":
                locator = await self._find_control_locator(root, field)
                if locator is None:
                    return None
                current_value = await locator.input_value()
                if normalize_text(current_value) != normalize_text(resolution.value):
                    await locator.click()
                    await locator.fill(resolution.value)
                    await page.wait_for_timeout(250)
                return await self._complete_text_field_interaction(
                    page=page,
                    field=field,
                    target_value=resolution.value,
                    settings=settings,
                )
            case "select":
                locator = await self._find_control_locator(root, field)
                if locator is None:
                    return None
                option_index = _pick_option_index(field.options, preferred=resolution.value)
                if option_index is None:
                    return None
                option = field.options[option_index]
                if await self._select_field_already_matches(
                    locator,
                    field,
                    option_index=option_index,
                ):
                    return option
                await self._select_field_option(locator, field, option_index=option_index)
                return option
            case "checkbox":
                locator = await self._find_control_locator(root, field)
                if locator is None:
                    return None
                should_check = normalize_text(resolution.value) in {"yes", "true", "1"}
                if (await locator.is_checked()) == should_check:
                    return "Yes" if should_check else "No"
                if should_check:
                    await locator.check()
                    return "Yes"
                await locator.uncheck()
                return "No"
            case "radio":
                option_index = _pick_option_index(field.options, preferred=resolution.value)
                if option_index is None:
                    return None
                option = field.options[option_index]
                if await self._check_radio_option(root, field, option):
                    return option
                return None
            case "file":
                locator = await self._find_control_locator(root, field)
                if locator is None or settings.profile.cv_path is None:
                    return None
                await locator.set_input_files(settings.profile.cv_path)
                return settings.profile.cv_filename or Path(settings.profile.cv_path).name

    async def _complete_text_field_interaction(
        self,
        *,
        page: Page,
        field: EasyApplyField,
        target_value: str,
        settings: UserAgentSettings,
    ) -> str:
        browser_agent = self._create_browser_agent(settings)
        recent_actions: list[dict[str, object]] = []

        for attempt_index in range(6):
            root = await self._easy_apply_root(page)
            locator = await self._find_control_locator(root, field)
            if locator is None:
                msg = (
                    "LinkedIn removed the current form field while the agent was trying to "
                    "finalize it."
                )
                raise LinkedInEasyApplyError(msg)
            state = await self._inspect_text_field_interaction(locator)
            if self._text_field_interaction_complete(state):
                return state.current_value or target_value
            focus_locator = await self._field_interaction_focus_locator(root, field)

            action = await browser_agent.perform_single_task_action(
                page=page,
                available_values={"intended_field_value": target_value},
                goal=(
                    "Finish the interaction for the already-filled LinkedIn Easy Apply field "
                    "so the current step accepts the value without advancing or closing the form."
                ),
                task_name="linkedin_easy_apply_finalize_field_interaction",
                extra_rules=(
                    f"The already-filled field label is {field.question_raw!r}.",
                    f"The intended accepted value is {target_value!r}.",
                    f"The current field value is {state.current_value!r}.",
                    (
                        "The field is currently invalid and still rejected by the page."
                        if state.invalid
                        else "The field does not currently show a visible validation error."
                    ),
                    (
                        f"Visible field validation feedback: {state.validation_message!r}."
                        if state.validation_message
                        else "No explicit validation text is currently visible for this field."
                    ),
                    (
                        f"Visible chooser options: {', '.join(state.visible_option_texts)!r}."
                        if state.visible_option_texts
                        else "No explicit chooser options are visible right now."
                    ),
                    (
                        "Do not replace the field with an unrelated value. "
                        "You may adjust the typed query only when that helps surface or select "
                        "a valid option semantically matching the intended value."
                    ),
                    (
                        "If an autocomplete, combobox, suggestion list, or dropdown choice is "
                        "visible for the current field, select the best matching option for the "
                        "intended value."
                    ),
                    (
                        "A visible option may still be correct even when it is not an exact "
                        "string match. Abbreviations, state or country suffixes, and nearby "
                        "regional formats can still represent the intended location."
                    ),
                    (
                        "If the current field likely needs keyboard confirmation or blur, use "
                        "press with Enter, Tab, ArrowDown, or ArrowUp before Enter when needed."
                    ),
                    (
                        "Do not click Continue, Next, Review, Submit, Dismiss, Close, Back, or "
                        "other step-level controls in this task."
                    ),
                    (
                        "If choices for the current field are hidden below the visible area of "
                        "the blocking surface, scroll the active surface rather than the page."
                    ),
                    (
                        "Use done only when the current field appears accepted and no chooser "
                        "for it still needs attention."
                    ),
                ),
                allowed_action_types=("click", "fill", "press", "scroll", "wait", "done", "fail"),
                recent_actions=recent_actions[-6:],
                step_index=attempt_index,
                focus_locator=focus_locator or root,
                priority_locator=locator,
            )
            recent_actions.append(
                {
                    "attempt_index": attempt_index,
                    "action_type": action.action_type,
                    "action_intent": action.action_intent,
                    "reasoning": action.reasoning,
                    "field_key": field.normalized_key,
                    "field_invalid_before_action": state.invalid,
                    "field_focused_before_action": state.focused,
                    "visible_option_count_before_action": state.visible_option_count,
                    "validation_message_before_action": state.validation_message,
                }
            )
            if action.action_type in {"done", "fail"}:
                break

        root = await self._easy_apply_root(page)
        locator = await self._find_control_locator(root, field)
        if locator is None:
            msg = "LinkedIn changed the current field before the interaction could finish."
            raise LinkedInEasyApplyError(msg)
        final_state = await self._inspect_text_field_interaction(locator)
        if final_state.needs_agentic_follow_up or not final_state.has_value:
            msg = (
                "Browser agent could not finish the interactive field flow for the current "
                "LinkedIn Easy Apply step."
            )
            raise LinkedInEasyApplyError(msg)
        return final_state.current_value or target_value

    async def _inspect_text_field_interaction(self, locator: Locator) -> TextFieldInteractionState:
        payload = await locator.evaluate(
            """
            (node) => {
              const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const isVisible = (candidate) => {
                if (!candidate || candidate.nodeType !== 1) {
                  return false;
                }
                const style = window.getComputedStyle(candidate);
                const rect = candidate.getBoundingClientRect();
                if (
                  rect.width <= 0 ||
                  rect.height <= 0 ||
                  rect.bottom <= 0 ||
                  rect.right <= 0 ||
                  rect.top >= window.innerHeight ||
                  rect.left >= window.innerWidth
                ) {
                  return false;
                }
                return style.visibility !== "hidden" && style.display !== "none";
              };
              const splitIds = (value) =>
                collapse(value)
                  .split(/\\s+/)
                  .map((item) => item.trim())
                  .filter(Boolean);
              const fieldRect = node.getBoundingClientRect();
              const overlapsHorizontally = (candidateRect) => {
                const overlap = Math.min(fieldRect.right, candidateRect.right)
                  - Math.max(fieldRect.left, candidateRect.left);
                return overlap > Math.min(fieldRect.width, candidateRect.width) * 0.25;
              };
              const nearbyValidationTexts = [];
              const seenValidationTexts = new Set();
              const pushValidationText = (candidate) => {
                if (!candidate || candidate.nodeType !== 1) {
                  return;
                }
                if (!isVisible(candidate)) {
                  return;
                }
                if (candidate === node || candidate.contains(node) || node.contains(candidate)) {
                  return;
                }
                const text = collapse(candidate.innerText || candidate.textContent || "");
                if (!text || text.length > 180 || seenValidationTexts.has(text)) {
                  return;
                }
                const rect = candidate.getBoundingClientRect();
                const verticalGap = rect.top - fieldRect.bottom;
                if (verticalGap < -6 || verticalGap > 96) {
                  return;
                }
                if (!overlapsHorizontally(rect)) {
                  return;
                }
                seenValidationTexts.add(text);
                nearbyValidationTexts.push({ text, verticalGap });
              };
              const relatedRoots = [];
              const seenRoots = new Set();
              const pushRoot = (candidate) => {
                if (!candidate || candidate.nodeType !== 1 || seenRoots.has(candidate)) {
                  return;
                }
                seenRoots.add(candidate);
                relatedRoots.push(candidate);
              };
              for (const attributeName of ["aria-controls", "aria-owns", "list"]) {
                for (const id of splitIds(node.getAttribute(attributeName))) {
                  pushRoot(document.getElementById(id));
                }
              }
              for (const attributeName of ["aria-describedby", "aria-errormessage"]) {
                for (const id of splitIds(node.getAttribute(attributeName))) {
                  pushValidationText(document.getElementById(id));
                }
              }
              for (const id of splitIds(node.getAttribute("aria-activedescendant"))) {
                pushRoot(document.getElementById(id));
              }
              let ancestor = node.parentElement;
              let depth = 0;
              while (ancestor && depth < 4) {
                for (const candidate of ancestor.querySelectorAll(
                  "[role='alert'], [aria-live='assertive'], [aria-live='polite']"
                )) {
                  pushValidationText(candidate);
                }
                ancestor = ancestor.parentElement;
                depth += 1;
              }
              for (const candidate of document.querySelectorAll("div, p, span, small, li")) {
                pushValidationText(candidate);
              }
              const optionTexts = [];
              const seenOptions = new Set();
              const pushOption = (candidate) => {
                if (!candidate || candidate.nodeType !== 1 || seenOptions.has(candidate)) {
                  return;
                }
                if (!isVisible(candidate)) {
                  return;
                }
                const text = collapse(
                  candidate.innerText
                  || candidate.textContent
                  || candidate.getAttribute("aria-label")
                  || ""
                );
                if (!text) {
                  return;
                }
                seenOptions.add(candidate);
                optionTexts.push(text);
              };
              for (const root of relatedRoots) {
                pushOption(root);
                for (const optionNode of root.querySelectorAll(
                  "[role='option'], li, button, div"
                )) {
                  pushOption(optionNode);
                }
              }
              if (
                optionTexts.length === 0 &&
                document.activeElement === node
              ) {
                for (const optionNode of document.querySelectorAll("[role='option']")) {
                  pushOption(optionNode);
                }
              }
              nearbyValidationTexts.sort((left, right) => {
                if (left.verticalGap !== right.verticalGap) {
                  return left.verticalGap - right.verticalGap;
                }
                return left.text.length - right.text.length;
              });
              const nativeValidity = typeof node.checkValidity === "function"
                ? node.checkValidity()
                : true;
              const invalidPseudoClass = typeof node.matches === "function"
                ? node.matches(":invalid")
                : false;
              const validationMessage = collapse(node.validationMessage || "");
              const ariaInvalid = node.getAttribute("aria-invalid") === "true";
              const combinedValidationTexts = [
                validationMessage,
                ...nearbyValidationTexts.map((item) => item.text),
              ].filter(Boolean);
              return {
                current_value: collapse(
                  node.value
                  || node.textContent
                  || node.getAttribute("value")
                  || ""
                ),
                focused: document.activeElement === node,
                role: collapse(node.getAttribute("role")),
                aria_autocomplete: collapse(node.getAttribute("aria-autocomplete")),
                aria_expanded: node.getAttribute("aria-expanded") === "true",
                has_popup_binding: ["aria-controls", "aria-owns", "list"].some(
                  (attributeName) => splitIds(node.getAttribute(attributeName)).length > 0,
                ),
                active_descendant: collapse(node.getAttribute("aria-activedescendant")),
                visible_option_count: optionTexts.length,
                visible_option_texts: optionTexts.slice(0, 6),
                invalid: ariaInvalid || invalidPseudoClass || !nativeValidity
                  || combinedValidationTexts.length > 0,
                validation_message: combinedValidationTexts[0] || "",
              };
            }
            """
        )
        return TextFieldInteractionState(
            current_value=str(payload.get("current_value") or "").strip(),
            focused=bool(payload.get("focused")),
            role=normalize_text(payload.get("role") or "") or None,
            aria_autocomplete=normalize_text(payload.get("aria_autocomplete") or "") or None,
            aria_expanded=bool(payload.get("aria_expanded")),
            has_popup_binding=bool(payload.get("has_popup_binding")),
            active_descendant=normalize_text(payload.get("active_descendant") or "") or None,
            visible_option_count=int(payload.get("visible_option_count") or 0),
            visible_option_texts=tuple(
                str(item).strip()
                for item in (payload.get("visible_option_texts") or ())
                if str(item).strip()
            ),
            invalid=bool(payload.get("invalid")),
            validation_message=str(payload.get("validation_message") or "").strip() or None,
        )

    def _text_field_interaction_complete(self, state: TextFieldInteractionState) -> bool:
        return state.has_value and not state.needs_agentic_follow_up

    async def _check_radio_option(
        self,
        root: Locator,
        field: EasyApplyField,
        option: str,
    ) -> bool:
        option_index = _pick_option_index(field.options, preferred=option)
        if option_index is None:
            return False
        option_ref = (
            field.option_refs[option_index] if len(field.option_refs) > option_index else None
        )
        if option_ref:
            locator = root.locator(_attribute_selector("data-job-applier-option-ref", option_ref))
            if await locator.count():
                await locator.first.check()
                return True

        if field.name:
            group = root.locator(
                f'input[type="radio"]{_attribute_selector("name", field.name)}',
            )
            if await group.count() > option_index:
                await group.nth(option_index).check()
                return True
        return False

    async def _find_control_locator(self, root: Locator, field: EasyApplyField) -> Locator | None:
        if field.dom_ref:
            locator = root.locator(_attribute_selector("data-job-applier-field-ref", field.dom_ref))
            if await locator.count():
                return locator.first
        if field.dom_id:
            locator = root.locator(_attribute_selector("id", field.dom_id))
            if await locator.count():
                return locator.first
        if field.name:
            locator = root.locator(_attribute_selector("name", field.name))
            if await locator.count():
                return locator.first
        return None

    async def _field_interaction_focus_locator(
        self,
        root: Locator,
        field: EasyApplyField,
    ) -> Locator | None:
        locator = await self._find_control_locator(root, field)
        if locator is None:
            return None
        container = locator.locator(
            "xpath=ancestor-or-self::*["
            "@role='group' or @role='listbox' or @role='dialog' "
            "or self::fieldset or self::section or self::form or self::div"
            "][1]"
        )
        if await container.count():
            return container.first
        return locator

    async def _select_field_option(
        self,
        locator: Locator,
        field: EasyApplyField,
        *,
        option_index: int,
    ) -> None:
        option_ref = (
            field.option_refs[option_index] if len(field.option_refs) > option_index else None
        )
        if option_ref is not None:
            if option_ref.startswith("value:"):
                await locator.select_option(value=option_ref.removeprefix("value:"))
                return
            if option_ref.startswith("index:"):
                await locator.select_option(index=int(option_ref.removeprefix("index:")))
                return
        await locator.select_option(index=option_index)

    async def _select_field_already_matches(
        self,
        locator: Locator,
        field: EasyApplyField,
        *,
        option_index: int,
    ) -> bool:
        option_ref = (
            field.option_refs[option_index] if len(field.option_refs) > option_index else None
        )
        option = field.options[option_index]
        if option_ref is not None:
            if option_ref.startswith("value:"):
                return (await locator.input_value()) == option_ref.removeprefix("value:")
            if option_ref.startswith("index:"):
                selected_index = await locator.evaluate(
                    "(node) => node instanceof HTMLSelectElement ? node.selectedIndex : -1"
                )
                return isinstance(selected_index, int) and selected_index == int(
                    option_ref.removeprefix("index:")
                )
        selected_text = await locator.evaluate(
            """
            (node) => {
              if (!(node instanceof HTMLSelectElement)) {
                return "";
              }
              return (node.selectedOptions[0]?.textContent || "").trim();
            }
            """
        )
        return normalize_text(str(selected_text or "")) == normalize_text(option)

    async def _extract_step(
        self,
        page: Page,
        *,
        last_known_step_index: int,
        last_known_total_steps: int,
    ) -> EasyApplyStep:
        root = await self._easy_apply_root(page)
        payload = await root.evaluate(
            """
            (node) => {
              const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const labels = Array.from(node.querySelectorAll("label"));
              let refCounter = 1;

              const ensureRef = (element, attributeName) => {
                const existing = collapse(element.getAttribute(attributeName));
                if (existing) {
                  return existing;
                }
                const ref = `job-applier-${refCounter}`;
                refCounter += 1;
                element.setAttribute(attributeName, ref);
                return ref;
              };

              const questionFor = (element) => {
                const ariaLabel = collapse(element.getAttribute("aria-label"));
                if (ariaLabel) {
                  return ariaLabel;
                }

                const id = element.getAttribute("id");
                if (id) {
                  const explicit = labels.find((label) => label.htmlFor === id);
                  if (explicit && collapse(explicit.innerText)) {
                    return collapse(explicit.innerText);
                  }
                }

                const wrappingLabel = element.closest("label");
                if (wrappingLabel && collapse(wrappingLabel.innerText)) {
                  return collapse(wrappingLabel.innerText);
                }

                const fieldset = element.closest("fieldset");
                if (fieldset) {
                  const legend = fieldset.querySelector("legend");
                  if (legend && collapse(legend.innerText)) {
                    return collapse(legend.innerText);
                  }
                }

                const container = element.closest([
                  ".fb-form-element",
                  ".jobs-easy-apply-form-section__grouping",
                  ".jobs-easy-apply-form-element",
                ].join(", "));
                if (container) {
                  const textLabel = container.querySelector(
                    "label, legend, .fb-form-element-label, [data-test-form-element-label]",
                  );
                  if (textLabel && collapse(textLabel.innerText)) {
                    return collapse(textLabel.innerText);
                  }
                }

                return collapse(
                  element.getAttribute("name")
                    || element.getAttribute("placeholder")
                    || element.getAttribute("type")
                    || element.tagName,
                );
              };

              const optionLabel = (element) => {
                const wrappingLabel = element.closest("label");
                if (wrappingLabel && collapse(wrappingLabel.innerText)) {
                  return collapse(wrappingLabel.innerText);
                }
                const id = element.getAttribute("id");
                if (id) {
                  const explicit = labels.find((label) => label.htmlFor === id);
                  if (explicit && collapse(explicit.innerText)) {
                    return collapse(explicit.innerText);
                  }
                }
                return collapse(element.getAttribute("value") || element.textContent || "");
              };

              const fields = [];
              const textControls = node.querySelectorAll([
                "input:not([type=radio]):not([type=checkbox]):not([type=hidden]):not([disabled])",
                "select:not([disabled])",
                "textarea:not([disabled])",
              ].join(", "));
              for (const element of textControls) {
                const tag = element.tagName.toLowerCase();
                const type =
                  tag === "textarea"
                    ? "textarea"
                    : (element.getAttribute("type") || tag);
                const controlKind =
                  tag === "select"
                    ? "select"
                    : type === "file"
                      ? "file"
                      : tag === "textarea"
                        ? "textarea"
                        : "text";
                const currentValue =
                  tag === "select"
                    ? collapse(element.options[element.selectedIndex]?.text || "")
                    : collapse(element.value || "");
                const domRef = ensureRef(element, "data-job-applier-field-ref");
                const selectOptionRefs =
                  tag === "select"
                    ? Array.from(element.options).map((option, index) => {
                        const optionValue = collapse(option.value);
                        if (optionValue) {
                          return `value:${optionValue}`;
                        }
                        return `index:${index}`;
                      })
                    : [];

                fields.push({
                  dom_ref: domRef,
                  dom_id: element.getAttribute("id"),
                  name: element.getAttribute("name"),
                  input_type: type,
                  control_kind: controlKind,
                  question_raw: questionFor(element),
                  required:
                    element.required
                    || element.getAttribute("aria-required") === "true"
                    || /\\*/.test(questionFor(element)),
                  prefilled: Boolean(currentValue),
                  current_value: currentValue,
                  options:
                    tag === "select"
                      ? Array.from(element.options)
                          .map((option) => collapse(option.textContent))
                          .filter(Boolean)
                      : [],
                  option_refs: selectOptionRefs,
                });
              }

              const radioInputs = Array.from(
                node.querySelectorAll("input[type=radio]:not([disabled])"),
              );
              const seenRadioNames = new Set();
              for (const input of radioInputs) {
                const groupName = input.getAttribute("name") || input.getAttribute("id");
                if (!groupName || seenRadioNames.has(groupName)) {
                  continue;
                }
                seenRadioNames.add(groupName);
                const group = radioInputs.filter(
                  (candidate) =>
                    (candidate.getAttribute("name") || candidate.getAttribute("id")) === groupName,
                );
                const selected = group.find((candidate) => candidate.checked);
                const optionRefs = group.map((candidate) =>
                  ensureRef(candidate, "data-job-applier-option-ref"),
                );
                fields.push({
                  dom_ref: ensureRef(input, "data-job-applier-field-ref"),
                  dom_id: input.getAttribute("id"),
                  name: input.getAttribute("name"),
                  input_type: "radio",
                  control_kind: "radio",
                  question_raw: questionFor(input),
                  required:
                    input.required
                    || input.getAttribute("aria-required") === "true"
                    || /\\*/.test(questionFor(input)),
                  prefilled: Boolean(selected),
                  current_value: selected ? optionLabel(selected) : "",
                  options: group.map((candidate) => optionLabel(candidate)).filter(Boolean),
                  option_refs: optionRefs,
                });
              }

              const checkboxes = node.querySelectorAll("input[type=checkbox]:not([disabled])");
              for (const input of checkboxes) {
                fields.push({
                  dom_ref: ensureRef(input, "data-job-applier-field-ref"),
                  dom_id: input.getAttribute("id"),
                  name: input.getAttribute("name"),
                  input_type: "checkbox",
                  control_kind: "checkbox",
                  question_raw: questionFor(input),
                  required:
                    input.required
                    || input.getAttribute("aria-required") === "true"
                    || /\\*/.test(questionFor(input)),
                  prefilled: input.checked,
                  current_value: input.checked ? "Yes" : "",
                  options: ["Yes", "No"],
                });
              }

              const text = collapse(node.innerText);
              const match = text.match(/step\\s*(\\d+)\\s*of\\s*(\\d+)/i);
              return {
                current_step: match ? Number(match[1]) : null,
                total_steps: match ? Number(match[2]) : null,
                fields,
              };
            }
            """,
        )

        current_step = payload.get("current_step")
        total_steps = payload.get("total_steps")
        if isinstance(current_step, int) and current_step >= 1:
            step_index = int(current_step) - 1
        else:
            step_index = max(0, last_known_step_index)
        if isinstance(total_steps, int) and total_steps >= 1:
            total = int(total_steps)
        else:
            total = max(last_known_total_steps, step_index + 1, 1)
        raw_fields = payload.get("fields", [])
        fields = tuple(
            self._question_extractor.build_field(item)
            for item in raw_fields
            if isinstance(item, dict)
        )
        return EasyApplyStep(step_index=step_index, total_steps=total, fields=fields)

    async def _open_easy_apply_modal_with_agent(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
    ) -> None:
        browser_agent = self._create_browser_agent(settings)
        recent_actions: list[dict[str, object]] = []

        try:
            for step_index in range(6):
                if await self._easy_apply_modal_visible(page):
                    return

                action = await browser_agent.perform_single_task_action(
                    page=page,
                    available_values={},
                    goal=(
                        "Open the LinkedIn Easy Apply modal for the current job posting. "
                        "Click the control that starts the application flow. "
                        "Do not click Save, share, close, or unrelated page navigation."
                    ),
                    task_name="linkedin_open_easy_apply",
                    extra_rules=(
                        "If the Easy Apply modal is already visible, choose done.",
                        (
                            "If the job page does not currently expose a way to start an "
                            "Easy Apply flow, choose fail."
                        ),
                        (
                            "If the start-application control is not visible yet and the page "
                            "can reveal more content, use scroll before giving up."
                        ),
                        (
                            "When clicking the control that starts the application flow, "
                            "set action_intent to open_easy_apply."
                        ),
                    ),
                    allowed_action_types=("click", "scroll", "wait", "done", "fail"),
                    recent_actions=recent_actions[-4:],
                    step_index=step_index,
                )
                recent_actions.append(
                    {
                        "step_index": step_index,
                        "action_type": action.action_type,
                        "action_intent": action.action_intent,
                        "reasoning": action.reasoning,
                    }
                )
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_open_action",
                        "step_index": step_index,
                        "action_type": action.action_type,
                        "action_intent": action.action_intent,
                        "reasoning": action.reasoning,
                    },
                )
                if await self._easy_apply_modal_visible(page):
                    return
                if action.action_type == "done":
                    break
        except BrowserAutomationError as exc:
            raise LinkedInEasyApplyError(str(exc)) from exc

        msg = (
            "Browser agent could not open the LinkedIn Easy Apply modal from the current job page."
        )
        raise LinkedInEasyApplyError(msg)

    async def _progress_easy_apply_step_with_agent(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
        step: EasyApplyStep,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
    ) -> BrowserAgentAction:
        browser_agent = self._create_browser_agent(settings)
        recent_actions: list[dict[str, object]] = []
        last_action: BrowserAgentAction | None = None
        try:
            for action_round in range(4):
                root = await self._easy_apply_root(page)
                action = await browser_agent.perform_single_task_action(
                    page=page,
                    available_values={},
                    goal=(
                        "Advance the current LinkedIn Easy Apply step. "
                        "If this is the final step, submit the application. "
                        "Do not click dismiss, close, save, or unrelated controls."
                    ),
                    task_name="linkedin_easy_apply_primary_action",
                    extra_rules=(
                        (
                            "Do not fill any field in this task. "
                            "The form fields are already handled elsewhere."
                        ),
                        (
                            "Keep working toward the macro goal until you either click the visible "
                            "primary advance or submit control, or no safe next move exists."
                        ),
                        (
                            "If the current step is complete and a primary button "
                            "advances the flow, click it."
                        ),
                        (
                            "If the current step shows the final application send action, "
                            "click it and set action_intent to submit_application."
                        ),
                        (
                            "If the current step shows a review or next action, click it and set "
                            "action_intent to advance_step."
                        ),
                        (
                            "If the active surface can scroll and the primary advance control "
                            "is not visible yet, scroll the active surface downward instead "
                            "of the page."
                        ),
                        (
                            "After a successful scroll, re-evaluate the visible controls and "
                            "click the primary CTA if it is now present instead of "
                            "scrolling again."
                        ),
                        (
                            "If the current screen is unchanged after filling fields, do not "
                            "keep guessing hidden CTAs. Scroll first when more modal content "
                            "is available."
                        ),
                        "If the page is still updating, choose wait.",
                    ),
                    allowed_action_types=("click", "scroll", "wait", "done", "fail"),
                    recent_actions=recent_actions[-6:],
                    step_index=step.step_index,
                    focus_locator=root,
                )
                last_action = action
                recent_actions.append(
                    {
                        "action_round": action_round,
                        "action_type": action.action_type,
                        "action_intent": action.action_intent,
                        "reasoning": action.reasoning,
                    }
                )
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_primary_action",
                        "step_index": step.step_index,
                        "action_round": action_round,
                        "action_type": action.action_type,
                        "action_intent": action.action_intent,
                        "reasoning": action.reasoning,
                    },
                )
                if action.action_type == "click" and action.action_intent in {
                    "advance_step",
                    "submit_application",
                }:
                    if action.action_intent == "advance_step":
                        await self._wait_for_easy_apply_surface(page)
                    return action
                if action.action_type in {"done", "fail"}:
                    return action
        except BrowserAutomationError as exc:
            raise LinkedInEasyApplyError(str(exc)) from exc
        if last_action is not None:
            return last_action
        msg = "Browser agent could not determine how to advance the Easy Apply step."
        raise LinkedInEasyApplyError(msg)

    async def _wait_for_easy_apply_surface(self, page: Page, *, timeout_ms: int = 6_000) -> None:
        deadline = asyncio.get_running_loop().time() + max(1, timeout_ms) / 1_000
        while True:
            if await self._easy_apply_modal_visible(page):
                return
            if asyncio.get_running_loop().time() >= deadline:
                break
            await page.wait_for_timeout(350)
        msg = "LinkedIn Easy Apply dialog is not visible after the last step action."
        raise LinkedInEasyApplyError(msg)

    async def _await_submission_outcome(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
        recent_actions: tuple[dict[str, object], ...] = (),
    ) -> tuple[bool, str | None]:
        for attempt_index in range(20):
            assessment = await self._assess_browser_state_with_agent(
                page,
                settings=settings,
                task_name="linkedin_easy_apply_submission_state",
                goal=(
                    "Determine whether the LinkedIn Easy Apply flow has already submitted the "
                    "application, is still processing, or is blocked by a visible issue."
                ),
                extra_rules=(
                    (
                        "Use complete only when the visible screen strongly indicates that the "
                        "application was already sent or finished."
                    ),
                    (
                        "Use blocked when the page shows a missing field, validation issue, or "
                        "another visible problem preventing completion."
                    ),
                    "Use pending while the UI is still transitioning or loading.",
                ),
                recent_actions=recent_actions,
                step_index=attempt_index,
            )
            self._record_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "easy_apply_submission_assessment",
                    "attempt_index": attempt_index,
                    "status": assessment.status,
                    "confidence": assessment.confidence,
                    "summary": assessment.summary,
                    "evidence": list(assessment.evidence),
                },
            )
            if assessment.status == "complete":
                return True, assessment.summary or "LinkedIn Easy Apply submitted successfully."
            if assessment.status == "blocked":
                return False, assessment.summary or "LinkedIn blocked the application flow."
            await page.wait_for_timeout(750)
        return False, "LinkedIn did not confirm the application result in time."

    async def _assess_easy_apply_step_state(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
        step: EasyApplyStep,
        step_answers: tuple[ApplicationAnswer, ...] = (),
        recent_actions: tuple[dict[str, object], ...] = (),
    ) -> tuple[str, str | None]:
        await self._retry_invalid_fields_after_primary_action(
            page,
            settings=settings,
            execution_id=execution_id,
            submission_id=submission_id,
            execution_events=execution_events,
            previous_step=step,
            step_answers=step_answers,
        )
        root = await self._easy_apply_root(page)
        assessment = await self._assess_browser_state_with_agent(
            page,
            settings=settings,
            task_name="linkedin_easy_apply_step_state",
            goal=(
                "Assess whether the current LinkedIn Easy Apply step is ready to continue, "
                "still settling, or blocked by a visible issue after the latest action."
            ),
            extra_rules=(
                "Use blocked when the screen shows a visible validation or completeness issue.",
                (
                    "Use complete only when the current step is clearly ready for the next "
                    "stage or has already advanced."
                ),
                "Use pending while the step is still rendering or saving.",
            ),
            recent_actions=recent_actions,
            step_index=step.step_index,
            focus_locator=root,
        )
        self._record_event(
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=ExecutionEventType.STEP_REACHED,
            payload={
                "stage": "easy_apply_step_assessment",
                "step_index": step.step_index,
                "status": assessment.status,
                "confidence": assessment.confidence,
                "summary": assessment.summary,
                "evidence": list(assessment.evidence),
            },
        )
        if assessment.status == "blocked":
            return "blocked", assessment.summary
        return assessment.status, assessment.summary

    async def _retry_invalid_fields_after_primary_action(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
        previous_step: EasyApplyStep,
        step_answers: tuple[ApplicationAnswer, ...],
    ) -> None:
        if not await self._easy_apply_modal_visible(page):
            return
        current_step = await self._extract_step(
            page,
            last_known_step_index=previous_step.step_index,
            last_known_total_steps=previous_step.total_steps,
        )
        if current_step.step_index != previous_step.step_index:
            return

        answer_by_key = {
            answer.normalized_key: answer.answer_raw
            for answer in step_answers
            if answer.answer_raw.strip()
        }
        root = await self._easy_apply_root(page)

        for field in current_step.fields:
            if field.control_kind not in {"text", "textarea"}:
                continue
            locator = await self._find_control_locator(root, field)
            if locator is None:
                continue
            state = await self._inspect_text_field_interaction(locator)
            if not state.invalid:
                continue

            target_value = answer_by_key.get(field.normalized_key) or state.current_value
            self._record_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "easy_apply_retry_invalid_field",
                    "step_index": current_step.step_index,
                    "normalized_key": field.normalized_key,
                    "validation_message": state.validation_message,
                    "target_value": target_value,
                },
            )
            try:
                await self._complete_text_field_interaction(
                    page=page,
                    field=field,
                    target_value=target_value,
                    settings=settings,
                )
            except LinkedInEasyApplyError as exc:
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.EXECUTION_FAILED,
                    payload={
                        "stage": "easy_apply_retry_invalid_field",
                        "step_index": current_step.step_index,
                        "normalized_key": field.normalized_key,
                        "message": str(exc),
                    },
                )

    async def _assess_browser_state_with_agent(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
        task_name: str,
        goal: str,
        extra_rules: tuple[str, ...] = (),
        recent_actions: tuple[dict[str, object], ...] = (),
        step_index: int = 0,
        focus_locator: Locator | None = None,
    ) -> BrowserTaskAssessment:
        browser_agent = self._create_browser_agent(settings)
        try:
            return await browser_agent.assess_browser_task(
                page=page,
                goal=goal,
                task_name=task_name,
                extra_rules=extra_rules,
                recent_actions=recent_actions,
                step_index=step_index,
                focus_locator=focus_locator,
            )
        except BrowserAutomationError as exc:
            raise LinkedInEasyApplyError(str(exc)) from exc

    async def _easy_apply_root(self, page: Page) -> Locator:
        for selector in (
            ".jobs-easy-apply-modal",
            "[data-test-modal] [role='dialog']",
            "[role='dialog']",
        ):
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    return candidate
        active_surface = page.locator('[data-job-applier-active-surface="true"]')
        if await active_surface.count() and await active_surface.first.is_visible():
            return active_surface.first
        snapshot = await BrowserDomSnapshotter().capture(page)
        if snapshot.active_surface:
            active_surface = page.locator('[data-job-applier-active-surface="true"]')
            if await active_surface.count() and await active_surface.first.is_visible():
                return active_surface.first
        msg = "LinkedIn Easy Apply dialog is not visible."
        raise LinkedInEasyApplyError(msg)

    async def _easy_apply_modal_visible(self, page: Page) -> bool:
        try:
            await self._easy_apply_root(page)
        except LinkedInEasyApplyError:
            return False
        return True

    async def _ensure_authenticated_page(self, page: Page) -> None:
        if await self._get_session_manager().page_requires_login(page):
            raise LinkedInAuthError("LinkedIn session expired during Easy Apply execution.")

    def _credentials_from_settings(self, runtime_settings: RuntimeSettings) -> LinkedInCredentials:
        if runtime_settings.linkedin_email is None or runtime_settings.linkedin_password is None:
            msg = (
                "LinkedIn credentials are required. "
                "Set JOB_APPLIER_LINKEDIN_EMAIL and JOB_APPLIER_LINKEDIN_PASSWORD in your .env."
            )
            raise LinkedInAuthError(msg)
        return LinkedInCredentials(
            email=runtime_settings.linkedin_email,
            password=runtime_settings.linkedin_password,
        )

    def _create_session_manager(
        self,
        settings: UserAgentSettings | None = None,
    ) -> LinkedInSessionManager:
        self._session_manager = LinkedInSessionManager(
            credentials=self._credentials_from_settings(self._runtime_settings),
            storage_state_path=self._runtime_settings.resolved_linkedin_storage_state_path,
            login_timeout_seconds=self._runtime_settings.linkedin_login_timeout_seconds,
            ai_api_key=(
                settings.ai.api_key
                if settings is not None
                else self._runtime_settings.openai_api_key
            ),
            ai_model=settings.ai.model if settings is not None else "o3-mini",
            playwright_mcp_url=(
                self._runtime_settings.resolved_playwright_mcp_url
                if self._runtime_settings.playwright_mcp_url is not None
                else None
            ),
            playwright_mcp_prefer_stdio_for_local=(
                self._runtime_settings.playwright_mcp_prefer_stdio_for_local
            ),
            playwright_mcp_stdio_command=(
                self._runtime_settings.resolved_playwright_mcp_stdio_command
            ),
            openai_responses_max_retries=self._runtime_settings.openai_responses_max_retries,
            openai_responses_retry_max_delay_seconds=(
                self._runtime_settings.openai_responses_retry_max_delay_seconds
            ),
        )
        return self._session_manager

    def _get_session_manager(self) -> LinkedInSessionManager:
        if self._session_manager is None:
            return self._create_session_manager()
        return self._session_manager

    def _create_browser_agent(self, settings: UserAgentSettings) -> OpenAIResponsesBrowserAgent:
        api_key = settings.ai.api_key or self._runtime_settings.openai_api_key
        if api_key is None:
            msg = (
                "OpenAI API key is required for the agentic LinkedIn Easy Apply flow. "
                "Configure it in the panel or set JOB_APPLIER_OPENAI_API_KEY."
            )
            raise LinkedInEasyApplyError(msg)
        return OpenAIResponsesBrowserAgent(
            api_key=api_key,
            model=settings.ai.model,
            min_action_delay_ms=self._runtime_settings.linkedin_min_action_delay_ms,
            max_action_delay_ms=self._runtime_settings.linkedin_max_action_delay_ms,
            openai_max_retries=self._runtime_settings.openai_responses_max_retries,
            openai_retry_max_delay_seconds=(
                self._runtime_settings.openai_responses_retry_max_delay_seconds
            ),
        )

    async def _pause_before_navigation(self, page: Page, *, reason: str) -> None:
        delay_ms = random.randint(
            self._runtime_settings.linkedin_min_navigation_delay_ms,
            self._runtime_settings.linkedin_max_navigation_delay_ms,
        )
        logger.info("linkedin_navigation_delay", extra={"reason": reason, "delay_ms": delay_ms})
        await page.wait_for_timeout(delay_ms)

    async def _start_trace(self, context: BrowserContext) -> bool:
        try:
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
        except Exception:  # noqa: BLE001
            logger.exception("linkedin_playwright_trace_start_failed")
            return False
        return True

    async def _stop_trace(
        self,
        context: BrowserContext,
        *,
        trace_started: bool,
        run_dir: Path,
        submission_id: UUID,
        preserve: bool,
    ) -> ArtifactSnapshot | None:
        if not trace_started:
            return None

        try:
            if not preserve:
                await context.tracing.stop()
                return None

            trace_path = self._artifact_path(
                run_dir,
                submission_id=submission_id,
                label="playwright_trace",
                extension="zip",
            )
            await context.tracing.stop(path=str(trace_path))
        except Exception:  # noqa: BLE001
            logger.exception("linkedin_playwright_trace_stop_failed")
            return None

        return _build_file_artifact(
            submission_id=submission_id,
            path=trace_path,
            artifact_type=ArtifactType.PLAYWRIGHT_TRACE,
        )

    async def _capture_debug_bundle(
        self,
        page: Page,
        *,
        run_dir: Path,
        submission_id: UUID,
        label: str,
    ) -> list[ArtifactSnapshot]:
        capture_token = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        screenshot_path = self._artifact_path(
            run_dir,
            submission_id=submission_id,
            label=label,
            extension="png",
            capture_token=capture_token,
        )
        html_path = self._artifact_path(
            run_dir,
            submission_id=submission_id,
            label=label,
            extension="html",
            capture_token=capture_token,
        )
        return [
            await self._capture_screenshot(
                page,
                screenshot_path,
                submission_id=submission_id,
            ),
            await self._capture_html_dump(
                page,
                html_path,
                submission_id=submission_id,
            ),
        ]

    async def _capture_screenshot(
        self,
        page: Page,
        path: Path,
        *,
        submission_id: UUID,
    ) -> ArtifactSnapshot:
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True)
        return ArtifactSnapshot(
            submission_id=submission_id,
            artifact_type=ArtifactType.SCREENSHOT,
            path=str(path),
            sha256=_sha256_file(path),
        )

    async def _capture_html_dump(
        self,
        page: Page,
        path: Path,
        *,
        submission_id: UUID,
    ) -> ArtifactSnapshot:
        path.parent.mkdir(parents=True, exist_ok=True)
        html_content = await page.content()
        path.write_text(html_content, encoding="utf-8")
        return ArtifactSnapshot(
            submission_id=submission_id,
            artifact_type=ArtifactType.HTML_DUMP,
            path=str(path),
            sha256=_sha256_file(path),
        )

    def _artifact_path(
        self,
        run_dir: Path,
        *,
        submission_id: UUID,
        label: str,
        extension: str,
        capture_token: str | None = None,
    ) -> Path:
        token = capture_token or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        return run_dir / f"{submission_id.hex}_{slug}_{token}.{extension}"

    def _build_run_dir(self, posting: JobPosting, submission_id: UUID) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        external_job_id = posting.external_job_id or posting.id.hex
        run_dir = (
            self._runtime_settings.resolved_linkedin_artifacts_dir
            / "submissions"
            / f"{timestamp}-{external_job_id}-{submission_id.hex[:8]}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _record_event(
        self,
        execution_events: list[ExecutionEvent],
        *,
        execution_id: UUID,
        event_type: ExecutionEventType,
        payload: dict[str, object],
        submission_id: UUID | None = None,
    ) -> None:
        execution_events.append(
            ExecutionEvent(
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=event_type,
                payload_json=json.dumps(payload, sort_keys=True),
            ),
        )

    def _record_exception_event(
        self,
        execution_events: list[ExecutionEvent],
        *,
        execution_id: UUID,
        stage: str,
        error: Exception,
        submission_id: UUID | None = None,
    ) -> None:
        self._record_event(
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=ExecutionEventType.EXCEPTION_CAPTURED,
            payload={
                "stage": stage,
                "error_type": error.__class__.__name__,
                "message": str(error),
                "stack_trace": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__),
                ),
            },
        )


class LinkedInEasyApplySubmitter(JobSubmitter):
    """Persist successful LinkedIn Easy Apply runs into the SQLite audit model."""

    def __init__(
        self,
        *,
        executor: EasyApplyExecutor,
        submission_repository: SubmissionRepository,
        answer_repository: AnswerRepository,
        profile_snapshot_repository: ProfileSnapshotRepository,
        recruiter_repository: RecruiterInteractionRepository,
        artifact_repository: ArtifactSnapshotRepository,
        execution_event_repository: ExecutionEventRepository,
    ) -> None:
        self._executor = executor
        self._submission_repository = submission_repository
        self._answer_repository = answer_repository
        self._profile_snapshot_repository = profile_snapshot_repository
        self._recruiter_repository = recruiter_repository
        self._artifact_repository = artifact_repository
        self._execution_event_repository = execution_event_repository

    async def submit(
        self,
        settings: UserAgentSettings,
        posting: JobPosting,
        *,
        execution_id: UUID,
        origin: ExecutionOrigin,
    ) -> SubmissionAttempt:
        result = await self._executor.execute(
            settings,
            posting,
            execution_id=execution_id,
            origin=origin,
        )

        if result.status is SubmissionStatus.SUBMITTED:
            record = self._persist_successful_submission(result, posting, settings, origin)
            self._persist_execution_events(result.execution_events, keep_submission_link=True)
            return SubmissionAttempt(
                submission=record.submission,
                successful_record=record,
            )

        self._persist_execution_events(result.execution_events, keep_submission_link=False)

        submission = ApplicationSubmission(
            id=result.submission_id,
            job_posting_id=posting.id,
            status=result.status,
            started_at=result.started_at,
            cv_version=result.cv_version or settings.profile.cv_filename,
            cover_letter_version=result.cover_letter_version,
            execution_origin=origin,
            notes=result.notes,
        )
        return SubmissionAttempt(submission=submission)

    def _persist_successful_submission(
        self,
        result: EasyApplyExecutionResult,
        posting: JobPosting,
        settings: UserAgentSettings,
        origin: ExecutionOrigin,
    ) -> SuccessfulSubmissionRecord:
        base_submission = ApplicationSubmission(
            id=result.submission_id,
            job_posting_id=posting.id,
            status=SubmissionStatus.PENDING,
            started_at=result.started_at,
            cv_version=result.cv_version or settings.profile.cv_filename,
            cover_letter_version=result.cover_letter_version,
            execution_origin=origin,
            notes=result.notes,
        )
        record = create_successful_submission_record(
            base_submission,
            settings=settings,
            submitted_at=result.submitted_at,
        )
        self._profile_snapshot_repository.save(record.snapshot)
        self._submission_repository.save(record.submission)
        for answer in result.answers:
            self._answer_repository.save(answer)
        for recruiter_interaction in result.recruiter_interactions:
            self._recruiter_repository.save(recruiter_interaction)
        for artifact in result.artifacts:
            self._artifact_repository.save(artifact)
        return record

    def _persist_execution_events(
        self,
        execution_events: tuple[ExecutionEvent, ...],
        *,
        keep_submission_link: bool,
    ) -> None:
        for event in execution_events:
            persisted_event = event
            if not keep_submission_link and event.submission_id is not None:
                persisted_event = replace(event, submission_id=None)
            self._execution_event_repository.save(persisted_event)


def _attribute_selector(attribute: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{attribute}="{escaped}"]'


def _pick_option_index(options: tuple[str, ...], *, preferred: str | None) -> int | None:
    if not options:
        return None
    if preferred is None:
        return 0
    normalized_preferred = normalize_text(preferred)
    for index, option in enumerate(options):
        if normalize_text(option) == normalized_preferred:
            return index
    for index, option in enumerate(options):
        if normalized_preferred in normalize_text(option):
            return index
    return 0


def _build_file_artifact(
    *,
    submission_id: UUID,
    path: Path,
    artifact_type: ArtifactType,
) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        submission_id=submission_id,
        artifact_type=artifact_type,
        path=str(path),
        sha256=_sha256_file(path),
    )


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()
