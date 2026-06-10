"""LinkedIn Easy Apply automation and submission persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from job_applier.application.agent_execution import (
    JobSubmitter,
    ScoredJobPosting,
    SubmissionAttempt,
)
from job_applier.application.config import UserAgentSettings
from job_applier.application.repositories import (
    AnswerRepository,
    ApplyActionMemoryRepository,
    ArtifactSnapshotRepository,
    ExecutionEventRepository,
    ProfileSnapshotRepository,
    RecruiterInteractionRepository,
    ResumeSourceSnapshotRepository,
    SubmissionRepository,
)
from job_applier.application.snapshotting import (
    SuccessfulSubmissionRecord,
    create_successful_submission_record,
)
from job_applier.cost_observability import record_efficiency_counter
from job_applier.domain.entities import (
    ApplicationAnswer,
    ApplicationSubmission,
    ApplyActionMemory,
    ArtifactSnapshot,
    ExecutionEvent,
    JobPosting,
    RecruiterInteraction,
    utc_now,
)
from job_applier.domain.enums import (
    AnswerSource,
    ArtifactType,
    DebugExecutionStage,
    ExecutionEventType,
    ExecutionOrigin,
    FillStrategy,
    QuestionType,
    ResumeMode,
    SubmissionStatus,
    SupportedLanguage,
)
from job_applier.infrastructure.language_support import detect_job_posting_language
from job_applier.infrastructure.linkedin.apply_memory import (
    TASK_FINALIZE_CHECKBOX,
    TASK_FINALIZE_FIELD,
    TASK_FINALIZE_RADIO,
    TASK_PRIMARY_ACTION,
    TASK_RESOLVE_CHECKBOX,
    TASK_RESOLVE_RADIO,
    TASK_RESOLVE_SELECT,
    AdaptiveApplyMemory,
    build_field_resolution_task_signature,
    build_field_task_signature,
    build_step_task_signature,
    resolution_task_type_for_field,
)
from job_applier.infrastructure.linkedin.auth import (
    LinkedInAuthError,
    LinkedInCredentials,
    LinkedInSessionManager,
)
from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAgentAction,
    BrowserAgentSnapshot,
    BrowserAutomationError,
    BrowserDomSnapshotter,
    BrowserInteractiveFieldAssessment,
    BrowserTaskAssessment,
    OpenAIResponsesBrowserAgent,
)
from job_applier.infrastructure.linkedin.job_email import (
    SmtpJobApplicationEmailSender,
    detect_job_application_email_target,
)
from job_applier.infrastructure.linkedin.question_resolution import (
    EasyApplyField,
    LinkedInAnswerResolver,
    LinkedInQuestionExtractor,
    ResolvedFieldValue,
    _looks_like_accessibility_accommodation_question,
    _looks_like_sensitive_demographic_gate_question,
    _looks_like_sensitive_demographic_question,
    _profile_first_name,
    _profile_last_name,
    _validation_feedback_requires_semantic_retry,
    field_has_meaningful_current_value,
    field_needs_semantic_step_planning,
    field_reference,
    normalize_text,
)
from job_applier.infrastructure.linkedin.recruiter_connect import (
    LinkedInRecruiterCandidateFinder,
    PlaywrightRecruiterConnector,
)
from job_applier.infrastructure.resume_dynamic import (
    DynamicResumeBuildResult,
    OhMyCvDynamicResumeBuilder,
)
from job_applier.observability import (
    append_artifact_reference,
    append_timeline_event,
    bind_submission_context,
    update_progress_snapshot,
)
from job_applier.recruiter_connect_observability import record_recruiter_connect_observation
from job_applier.settings import RuntimeSettings

logger = logging.getLogger(__name__)

_FIELD_STATE_INSPECTION_TIMEOUT_MS = 5_000


def _field_disallows_adaptive_resolution_memory(field: EasyApplyField) -> bool:
    return (
        _looks_like_accessibility_accommodation_question(field)
        or _looks_like_sensitive_demographic_question(field)
        or _looks_like_sensitive_demographic_gate_question(field)
    )


class LinkedInEasyApplyError(RuntimeError):
    """Raised when the Easy Apply execution cannot continue."""


class MissingRequiredProfileDataError(LinkedInEasyApplyError):
    """Raised when a required apply field lacks safe factual data from the profile."""

    def __init__(
        self,
        *,
        normalized_key: str,
        question_type: QuestionType,
        control_kind: str,
    ) -> None:
        self.normalized_key = normalized_key
        self.question_type = question_type
        self.control_kind = control_kind
        super().__init__(
            "Required LinkedIn Easy Apply field could not be resolved safely because the "
            "profile does not provide the factual data needed: "
            f"normalized_key={normalized_key}, "
            f"question_type={question_type.value}, "
            f"control_kind={control_kind}."
        )


def _field_has_explicit_invalid_feedback(field: EasyApplyField) -> bool:
    feedback = normalize_text(f"{field.helper_text or ''} {field.field_context}")
    if not feedback:
        return False
    return any(
        token in feedback
        for token in (
            "invalid input",
            "invalid value",
            "error",
            "required",
            "obrigatorio",
            "obrigatória",
            "obrigatorio",
            "invalido",
            "inválido",
        )
    )


def _field_requires_agentic_semantic_recovery(field: EasyApplyField) -> bool:
    if field.control_kind not in {"text", "textarea"}:
        return False
    if not field_has_meaningful_current_value(field):
        return False
    return _field_has_explicit_invalid_feedback(field)


def _resume_field_matches_requested_cv(
    field: EasyApplyField,
    *,
    submission_cv_path: Path | None,
    fallback_filename: str | None = None,
) -> bool:
    target_filename = (
        submission_cv_path.name
        if submission_cv_path is not None
        else (fallback_filename.strip() if fallback_filename else None)
    )
    if target_filename is None:
        return False
    return _resume_text_matches_requested_cv(field.current_value, target_filename)


def _resume_field_verification_state(
    field: EasyApplyField,
    *,
    target_cv_name: str | None,
) -> ResumeVerificationState:
    if target_cv_name is None:
        return ResumeVerificationState(target_cv_name=None)
    selected_value = normalize_text(field.current_value)
    verified = _resume_text_matches_requested_cv(field.current_value, target_cv_name)
    option_visible = any(
        _resume_text_matches_requested_cv(option, target_cv_name) for option in field.options
    )
    if verified:
        reason = "verified"
    elif not option_visible:
        reason = "picker_missing_target_resume"
    elif selected_value:
        reason = "picker_selected_different_resume"
    else:
        reason = "picker_exposes_target_resume_unselected"
    return ResumeVerificationState(
        target_cv_name=target_cv_name,
        verified=verified,
        option_visible=option_visible,
        selected_value=selected_value,
        reason=reason,
    )


def _evaluate_resume_verification(
    step: EasyApplyStep,
    step_answers: Sequence[ApplicationAnswer],
    *,
    target_cv_name: str | None,
) -> ResumeVerificationState:
    if target_cv_name is None:
        return ResumeVerificationState(target_cv_name=None)
    resume_fields = tuple(
        field for field in step.fields if field.question_type is QuestionType.RESUME_UPLOAD
    )
    if not resume_fields:
        return ResumeVerificationState(target_cv_name=target_cv_name)
    field_states = tuple(
        _resume_field_verification_state(field, target_cv_name=target_cv_name)
        for field in resume_fields
    )
    for state in field_states:
        if state.verified:
            return state
    option_visible = any(state.option_visible for state in field_states)
    selected_value = next(
        (state.selected_value for state in field_states if state.selected_value),
        "",
    )
    if not option_visible:
        reason = "picker_missing_target_resume"
    elif selected_value:
        reason = "picker_selected_different_resume"
    else:
        reason = "picker_exposes_target_resume_unselected"
    return ResumeVerificationState(
        target_cv_name=target_cv_name,
        verified=False,
        option_visible=option_visible,
        selected_value=selected_value,
        reason=reason,
    )


def _interactive_field_recovery_directive(
    *,
    assessment: BrowserInteractiveFieldAssessment,
    field_label: str,
    target_value: str,
) -> InteractiveFieldRecoveryDirective:
    """Translate one chooser/autocomplete assessment into an agentic recovery plan."""

    common_rule = (
        f"The field {field_label!r} must end up accepted for the intended value {target_value!r}."
    )
    match assessment.status:
        case "needs_focus":
            return InteractiveFieldRecoveryDirective(
                task_name="linkedin_easy_apply_recover_chooser_focus",
                goal=(
                    "Recover focus or reopen the current chooser/autocomplete widget so it can "
                    "surface or accept the intended answer without advancing the step."
                ),
                extra_rules=(
                    common_rule,
                    (
                        "Prioritize refocusing the current widget, reopening hidden suggestions, "
                        "or restoring the chooser state before attempting to confirm anything."
                    ),
                ),
            )
        case "needs_option_selection":
            return InteractiveFieldRecoveryDirective(
                task_name="linkedin_easy_apply_select_chooser_option",
                goal=(
                    "Select the best visible chooser/autocomplete option for the current field "
                    "without advancing or closing the LinkedIn Easy Apply step."
                ),
                extra_rules=(
                    common_rule,
                    (
                        "Prioritize selecting one visible option from the current chooser state "
                        "before reformulating the query or leaving the widget."
                    ),
                ),
            )
        case "needs_query_reformulation":
            return InteractiveFieldRecoveryDirective(
                task_name="linkedin_easy_apply_reformulate_chooser_query",
                goal=(
                    "Adjust the chooser query only as much as needed to surface a "
                    "semantically matching option for the current field."
                ),
                extra_rules=(
                    common_rule,
                    (
                        "Preserve the meaning of the intended answer while using a shorter or "
                        "UI-friendlier query that can reveal a matching suggestion."
                    ),
                ),
            )
        case "needs_confirmation":
            return InteractiveFieldRecoveryDirective(
                task_name="linkedin_easy_apply_confirm_chooser_selection",
                goal=(
                    "Commit the current or highlighted chooser/autocomplete selection so the "
                    "field becomes accepted without advancing or closing the step."
                ),
                extra_rules=(
                    common_rule,
                    (
                        "Prioritize a commit move such as clicking the current suggestion or "
                        "using Enter or Tab only when it finalizes the existing widget state."
                    ),
                ),
            )
        case "blocked":
            return InteractiveFieldRecoveryDirective(
                task_name="linkedin_easy_apply_unblock_chooser_field",
                goal=(
                    "Recover the blocked chooser/autocomplete field so the current step can "
                    "accept the intended answer without advancing or closing the form."
                ),
                extra_rules=(
                    common_rule,
                    (
                        "The field currently looks blocked. Prefer materially different recovery "
                        "moves instead of repeating the same ineffective interaction."
                    ),
                ),
            )
        case _:
            return InteractiveFieldRecoveryDirective(
                task_name="linkedin_easy_apply_finalize_field_interaction",
                goal=(
                    "Finish the interaction for the already-filled LinkedIn Easy Apply field so "
                    "the current step accepts the value without advancing or closing the form."
                ),
                extra_rules=(common_rule,),
            )


@dataclass(frozen=True, slots=True)
class EasyApplyStep:
    """Current Easy Apply step metadata and discovered controls."""

    step_index: int
    total_steps: int
    fields: tuple[EasyApplyField, ...]
    surface_text: str = ""


@dataclass(frozen=True, slots=True)
class InteractiveFieldRecoveryDirective:
    """Agentic recovery directive for chooser/autocomplete text widgets."""

    task_name: str
    goal: str
    extra_rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EasyApplyExecutionResult:
    """Structured result produced by the Playwright Easy Apply executor."""

    submission_id: UUID
    started_at: datetime
    status: SubmissionStatus
    resume_mode: ResumeMode = ResumeMode.STATIC
    target_language: SupportedLanguage = SupportedLanguage.ENGLISH
    matched_role_target: str | None = None
    matched_specializations: tuple[str, ...] = ()
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
class JobApplyEntrypointAssessment:
    """Deterministic view of the application controls visible on the job page."""

    easy_apply_available: bool
    external_apply_only: bool
    terminally_unavailable: bool = False
    labels: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedSubmissionCv:
    """Resume artifact selected for one submission execution."""

    path: Path
    cv_version: str
    resume_mode: ResumeMode = ResumeMode.STATIC
    target_language: SupportedLanguage = SupportedLanguage.ENGLISH
    matched_role_target: str | None = None
    matched_specializations: tuple[str, ...] = ()
    artifacts: tuple[ArtifactSnapshot, ...] = ()
    used_dynamic_variant: bool = False
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeVerificationState:
    """Verification state for the requested resume within Easy Apply."""

    target_cv_name: str | None
    verified: bool = False
    option_visible: bool = False
    selected_value: str = ""
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeUploadSettleState:
    """Observed completion state for a LinkedIn resume upload interaction."""

    selection_matches: bool = False
    target_visible: bool = False
    success_feedback: bool = False
    uploading: bool = False
    status_text: str | None = None

    @property
    def settled(self) -> bool:
        return self.selection_matches or self.target_visible or self.success_feedback


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


@dataclass(frozen=True, slots=True)
class ControlValidationState:
    """Validation snapshot for non-text Easy Apply controls."""

    invalid: bool
    validation_message: str | None = None
    current_value: str = ""


def _field_debug_summary(field: EasyApplyField) -> dict[str, object]:
    return {
        "question_raw": field.question_raw,
        "normalized_key": field.normalized_key,
        "question_type": field.question_type.value,
        "control_kind": field.control_kind,
        "input_type": field.input_type,
        "required": field.required,
        "prefilled": field.prefilled,
        "current_value": field.current_value,
        "classification_confidence": field.classification_confidence,
        "classification_rule": field.classification_rule,
        "field_context_preview": field.field_context[:240],
        "helper_text": field.helper_text,
        "option_count": len(field.options),
        "options_preview": list(field.options[:6]),
    }


def _step_field_signature(step: EasyApplyStep) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (
            normalize_text(field.question_raw),
            normalize_text(field.normalized_key),
            field.question_type.value,
            field.control_kind,
            field.input_type or "",
            "required" if field.required else "optional",
            str(len(field.options)),
        )
        for field in step.fields
    )


def _step_surface_changed(previous_step: EasyApplyStep, current_step: EasyApplyStep) -> bool:
    if _step_field_signature(previous_step) != _step_field_signature(current_step):
        return True
    if previous_step.fields or current_step.fields:
        return False
    return normalize_text(previous_step.surface_text[:240]) != normalize_text(
        current_step.surface_text[:240]
    )


def _same_step_field_identity(left: EasyApplyField, right: EasyApplyField) -> bool:
    left_ref = field_reference(left)
    right_ref = field_reference(right)
    if left_ref and right_ref and left_ref == right_ref:
        return True
    return (
        left.question_type is right.question_type
        and normalize_text(left.normalized_key) == normalize_text(right.normalized_key)
        and normalize_text(left.question_raw) == normalize_text(right.question_raw)
    )


class EasyApplyExecutor(Protocol):
    """Boundary used by the submitter to run the browser automation."""

    async def execute(
        self,
        settings: UserAgentSettings,
        posting: JobPosting,
        scored_job: ScoredJobPosting,
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
        apply_action_memory_repository: ApplyActionMemoryRepository | None = None,
        resume_source_snapshot_repository: ResumeSourceSnapshotRepository | None = None,
    ) -> None:
        self._runtime_settings = runtime_settings
        self._answer_resolver = answer_resolver or LinkedInAnswerResolver()
        self._question_extractor = LinkedInQuestionExtractor()
        self._recruiter_candidate_finder = LinkedInRecruiterCandidateFinder()
        self._recruiter_connector = PlaywrightRecruiterConnector(runtime_settings)
        self._job_email_sender = SmtpJobApplicationEmailSender(runtime_settings)
        self._execution_event_repository = execution_event_repository
        self._dynamic_resume_builder = OhMyCvDynamicResumeBuilder(
            runtime_settings,
            resume_source_snapshot_repository=resume_source_snapshot_repository,
        )
        self._apply_memory = (
            AdaptiveApplyMemory(apply_action_memory_repository)
            if apply_action_memory_repository is not None
            else None
        )
        self._session_manager: LinkedInSessionManager | None = None

    def _is_production_apply_run(self) -> bool:
        return (
            self._runtime_settings.resolved_agent_debug_stage is DebugExecutionStage.FULL
            and not self._runtime_settings.agent_test_mode
        )

    def _agentic_retry_budget(self, *, default: int, production_cap: int = 3) -> int:
        if self._is_production_apply_run():
            return max(1, min(default, production_cap))
        return max(1, default)

    def _easy_apply_step_revisit_limit(self) -> int | None:
        if self._is_production_apply_run():
            return 3
        return None

    def _easy_apply_iteration_limit(self) -> int:
        if self._is_production_apply_run():
            return 64
        return 128

    def _recruiter_connect_feature_enabled(self, settings: UserAgentSettings) -> bool:
        return (
            self._runtime_settings.feature_recruiter_connect_enabled
            and settings.agent.auto_connect_with_recruiter
        )

    async def execute(
        self,
        settings: UserAgentSettings,
        posting: JobPosting,
        scored_job: ScoredJobPosting,
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
        result: EasyApplyExecutionResult | None = None
        primary_error: Exception | None = None

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
                        scored_job,
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
                except Exception as exc:  # noqa: BLE001
                    primary_error = exc
                    raise
                finally:
                    try:
                        await context.close()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "linkedin_easy_apply_context_close_failed",
                            extra={
                                "job_posting_id": str(posting.id),
                                "submission_id": str(submission_id),
                                "close_error": str(exc),
                            },
                        )
                        if primary_error is None and result is None:
                            raise
            finally:
                try:
                    await browser.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "linkedin_easy_apply_browser_close_failed",
                        extra={
                            "job_posting_id": str(posting.id),
                            "submission_id": str(submission_id),
                            "close_error": str(exc),
                        },
                    )
                    if primary_error is None and result is None:
                        raise

    async def _execute_once(
        self,
        context: BrowserContext,
        settings: UserAgentSettings,
        posting: JobPosting,
        scored_job: ScoredJobPosting,
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
        resume_mode = settings.profile.resume_mode
        matched_role_target = scored_job.matched_role_target
        matched_specializations = scored_job.matched_specializations
        target_language = detect_job_posting_language(
            posting,
            default_language=settings.profile.preferred_language,
        ).language

        try:
            with bind_submission_context(submission_id):
                await self._open_job_detail_page(page, posting=posting)
                await self._ensure_authenticated_page(page)
                update_progress_snapshot(
                    {
                        "current_stage": "easy_apply_job_page_loaded",
                        "current_job": self._build_progress_job(posting, submission_id),
                        "current_step": None,
                    },
                )
                append_timeline_event(
                    "easy_apply_job_page_loaded",
                    {
                        "job_posting_id": str(posting.id),
                        "external_job_id": posting.external_job_id,
                        "title": posting.title,
                        "company_name": posting.company_name,
                        "url": posting.url,
                    },
                )
                artifacts.extend(
                    await self._capture_debug_bundle(
                        page,
                        run_dir=run_dir,
                        submission_id=submission_id,
                        label="job_opened",
                    ),
                )
                entrypoint_assessment = await self._assess_job_apply_entrypoint(page)
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_entrypoint_assessed",
                        "job_posting_id": str(posting.id),
                        "easy_apply_available": entrypoint_assessment.easy_apply_available,
                        "external_apply_only": entrypoint_assessment.external_apply_only,
                        "terminally_unavailable": (entrypoint_assessment.terminally_unavailable),
                        "labels": list(entrypoint_assessment.labels[:6]),
                        "notes": entrypoint_assessment.notes,
                    },
                )
                append_timeline_event(
                    "easy_apply_entrypoint_assessed",
                    {
                        "job_posting_id": str(posting.id),
                        "submission_id": str(submission_id),
                        "easy_apply_available": entrypoint_assessment.easy_apply_available,
                        "external_apply_only": entrypoint_assessment.external_apply_only,
                        "terminally_unavailable": (entrypoint_assessment.terminally_unavailable),
                        "labels": list(entrypoint_assessment.labels[:6]),
                        "notes": entrypoint_assessment.notes,
                    },
                )
                if entrypoint_assessment.external_apply_only:
                    notes = (
                        entrypoint_assessment.notes
                        or "The job page only exposes an external apply control."
                    )
                    update_progress_snapshot(
                        {
                            "current_stage": "submit_skipped",
                            "current_job": self._build_progress_job(
                                posting,
                                submission_id,
                                status=SubmissionStatus.SKIPPED.value,
                            ),
                            "current_step": None,
                            "last_error": notes,
                        },
                    )
                    self._record_event(
                        execution_events,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        event_type=ExecutionEventType.JOB_PROCESSED,
                        payload={
                            "job_posting_id": str(posting.id),
                            "origin": origin.value,
                            "reason": "external_apply_only",
                            "status": SubmissionStatus.SKIPPED.value,
                            "notes": notes,
                        },
                    )
                    return EasyApplyExecutionResult(
                        submission_id=submission_id,
                        started_at=started_at,
                        status=SubmissionStatus.SKIPPED,
                        resume_mode=resume_mode,
                        matched_role_target=matched_role_target,
                        matched_specializations=matched_specializations,
                        notes=notes,
                        execution_events=tuple(execution_events),
                        artifacts=tuple(artifacts),
                        recruiter_interactions=tuple(recruiter_interactions),
                    )
                if entrypoint_assessment.terminally_unavailable:
                    notes = (
                        entrypoint_assessment.notes
                        or "The current LinkedIn job page is no longer accepting applications."
                    )
                    update_progress_snapshot(
                        {
                            "current_stage": "submit_skipped",
                            "current_job": self._build_progress_job(
                                posting,
                                submission_id,
                                status=SubmissionStatus.SKIPPED.value,
                            ),
                            "current_step": None,
                            "last_error": notes,
                        },
                    )
                    self._record_event(
                        execution_events,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        event_type=ExecutionEventType.JOB_PROCESSED,
                        payload={
                            "job_posting_id": str(posting.id),
                            "origin": origin.value,
                            "reason": "job_unavailable",
                            "status": SubmissionStatus.SKIPPED.value,
                            "notes": notes,
                        },
                    )
                    return EasyApplyExecutionResult(
                        submission_id=submission_id,
                        started_at=started_at,
                        status=SubmissionStatus.SKIPPED,
                        resume_mode=resume_mode,
                        matched_role_target=matched_role_target,
                        matched_specializations=matched_specializations,
                        notes=notes,
                        execution_events=tuple(execution_events),
                        artifacts=tuple(artifacts),
                        recruiter_interactions=tuple(recruiter_interactions),
                    )
                if not entrypoint_assessment.easy_apply_available:
                    blocker_assessment = await self._assess_job_page_apply_blocker_with_agent(
                        page,
                        settings=settings,
                    )
                    if blocker_assessment is not None:
                        blocker_notes = blocker_assessment.summary.strip()
                        if blocker_assessment.status == "complete":
                            notes = (
                                blocker_notes
                                or "LinkedIn indicates this job was already applied to."
                            )
                            artifacts.extend(
                                await self._capture_debug_bundle(
                                    page,
                                    run_dir=run_dir,
                                    submission_id=submission_id,
                                    label="skip_already_applied",
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
                                    "reason": "already_applied",
                                    "status": SubmissionStatus.SKIPPED.value,
                                    "notes": notes,
                                },
                            )
                            return EasyApplyExecutionResult(
                                submission_id=submission_id,
                                started_at=started_at,
                                status=SubmissionStatus.SKIPPED,
                                resume_mode=resume_mode,
                                matched_role_target=matched_role_target,
                                matched_specializations=matched_specializations,
                                notes=notes,
                                execution_events=tuple(execution_events),
                                artifacts=tuple(artifacts),
                                recruiter_interactions=tuple(recruiter_interactions),
                            )
                        if blocker_assessment.status == "blocked":
                            notes = (
                                blocker_notes
                                or entrypoint_assessment.notes
                                or (
                                    "The current LinkedIn job page does not allow a new "
                                    "application to be started."
                                )
                            )
                            artifacts.extend(
                                await self._capture_debug_bundle(
                                    page,
                                    run_dir=run_dir,
                                    submission_id=submission_id,
                                    label="skip_unavailable",
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
                                    "reason": "job_page_blocked",
                                    "status": SubmissionStatus.SKIPPED.value,
                                    "notes": notes,
                                },
                            )
                            return EasyApplyExecutionResult(
                                submission_id=submission_id,
                                started_at=started_at,
                                status=SubmissionStatus.SKIPPED,
                                resume_mode=resume_mode,
                                matched_role_target=matched_role_target,
                                matched_specializations=matched_specializations,
                                notes=notes,
                                execution_events=tuple(execution_events),
                                artifacts=tuple(artifacts),
                                recruiter_interactions=tuple(recruiter_interactions),
                            )
                recruiter_candidate = None
                if self._recruiter_connect_feature_enabled(settings):
                    recruiter_candidate = await self._recruiter_candidate_finder.find(
                        page,
                        settings,
                    )
                prepared_submission_cv: PreparedSubmissionCv | None = None
                submission_cv_path: Path | None = None
                submission_cv_version = settings.profile.cv_filename

                try:
                    update_progress_snapshot(
                        {
                            "current_stage": "easy_apply_open_modal",
                            "current_job": self._build_progress_job(posting, submission_id),
                            "current_step": None,
                        },
                    )
                    await self._open_easy_apply_modal_with_agent(
                        page,
                        settings=settings,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        execution_events=execution_events,
                    )
                except LinkedInEasyApplyError as exc:
                    notes = str(exc) or "Browser agent could not open the Easy Apply modal."
                    blocker_assessment = await self._assess_job_page_apply_blocker_with_agent(
                        page,
                        settings=settings,
                    )
                    if blocker_assessment is not None and blocker_assessment.status == "complete":
                        already_applied_notes = blocker_assessment.summary.strip() or (
                            "LinkedIn indicates this job was already applied to."
                        )
                        artifacts.extend(
                            await self._capture_debug_bundle(
                                page,
                                run_dir=run_dir,
                                submission_id=submission_id,
                                label="skip_already_applied",
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
                                "reason": "already_applied",
                                "status": SubmissionStatus.SKIPPED.value,
                                "notes": already_applied_notes,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.SKIPPED,
                            resume_mode=resume_mode,
                            matched_role_target=matched_role_target,
                            matched_specializations=matched_specializations,
                            notes=already_applied_notes,
                            execution_events=tuple(execution_events),
                            artifacts=tuple(artifacts),
                            recruiter_interactions=tuple(recruiter_interactions),
                        )
                    if blocker_assessment is not None and blocker_assessment.status == "blocked":
                        blocked_notes = blocker_assessment.summary.strip() or notes
                        artifacts.extend(
                            await self._capture_debug_bundle(
                                page,
                                run_dir=run_dir,
                                submission_id=submission_id,
                                label="skip_unavailable",
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
                                "reason": "job_page_blocked",
                                "status": SubmissionStatus.SKIPPED.value,
                                "notes": blocked_notes,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.SKIPPED,
                            resume_mode=resume_mode,
                            matched_role_target=matched_role_target,
                            matched_specializations=matched_specializations,
                            notes=blocked_notes,
                            execution_events=tuple(execution_events),
                            artifacts=tuple(artifacts),
                            recruiter_interactions=tuple(recruiter_interactions),
                        )
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
                        resume_mode=resume_mode,
                        matched_role_target=matched_role_target,
                        matched_specializations=matched_specializations,
                        notes=notes,
                        execution_events=tuple(execution_events),
                        artifacts=tuple(artifacts),
                        recruiter_interactions=tuple(recruiter_interactions),
                    )
                update_progress_snapshot(
                    {
                        "current_stage": "easy_apply_modal_opened",
                        "current_job": self._build_progress_job(posting, submission_id),
                        "current_step": 0,
                    },
                )
                append_timeline_event(
                    "easy_apply_modal_opened",
                    {
                        "job_posting_id": str(posting.id),
                        "submission_id": str(submission_id),
                    },
                )
                prepared_submission_cv = await self._prepare_submission_cv_path(
                    settings=settings,
                    posting=posting,
                    scored_job=scored_job,
                    execution_id=execution_id,
                    run_dir=run_dir,
                    submission_id=submission_id,
                    execution_events=execution_events,
                )
                submission_cv_path = prepared_submission_cv.path if prepared_submission_cv else None
                submission_cv_version = (
                    prepared_submission_cv.cv_version
                    if prepared_submission_cv is not None
                    else settings.profile.cv_filename
                )
                if prepared_submission_cv is not None:
                    resume_mode = prepared_submission_cv.resume_mode
                    target_language = prepared_submission_cv.target_language
                    matched_role_target = prepared_submission_cv.matched_role_target
                    matched_specializations = prepared_submission_cv.matched_specializations
                    artifacts.extend(prepared_submission_cv.artifacts)

                max_iterations = self._easy_apply_iteration_limit()
                step_revisit_limit = self._easy_apply_step_revisit_limit()
                last_known_step_index = 0
                last_known_total_steps = 1
                step_visit_counts: dict[int, int] = {}
                force_resume_reselection = False
                force_semantic_reassert_keys: set[str] = set()
                resume_review_repair_attempted = False
                resume_review_verified_selection = False
                resume_verification_required = False
                for _ in range(max_iterations):
                    step = await self._extract_step(
                        page,
                        last_known_step_index=last_known_step_index,
                        last_known_total_steps=last_known_total_steps,
                    )
                    step_visit_counts[step.step_index] = (
                        step_visit_counts.get(step.step_index, 0) + 1
                    )
                    if (
                        step_revisit_limit is not None
                        and step_visit_counts[step.step_index] > step_revisit_limit
                    ):
                        notes = (
                            "LinkedIn Easy Apply exceeded the production retry limit for "
                            f"step {step.step_index + 1}."
                        )
                        artifacts.extend(
                            await self._capture_debug_bundle(
                                page,
                                run_dir=run_dir,
                                submission_id=submission_id,
                                label=f"failure_step_{step.step_index + 1:02d}_retry_limit",
                            ),
                        )
                        self._record_event(
                            execution_events,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            event_type=ExecutionEventType.JOB_PROCESSED,
                            payload={
                                "job_posting_id": str(posting.id),
                                "reason": "step_retry_limit_exceeded",
                                "status": SubmissionStatus.FAILED.value,
                                "notes": notes,
                                "step_index": step.step_index,
                                "step_visit_count": step_visit_counts[step.step_index],
                                "retry_limit": step_revisit_limit,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.FAILED,
                            resume_mode=resume_mode,
                            target_language=target_language,
                            matched_role_target=matched_role_target,
                            matched_specializations=matched_specializations,
                            notes=notes,
                        )
                    update_progress_snapshot(
                        {
                            "current_stage": "easy_apply_step_extracted",
                            "current_job": self._build_progress_job(posting, submission_id),
                            "current_step": step.step_index + 1,
                            "easy_apply_total_steps": step.total_steps,
                            "easy_apply_field_count": len(step.fields),
                        },
                    )
                    append_timeline_event(
                        "easy_apply_step_extracted",
                        {
                            "job_posting_id": str(posting.id),
                            "submission_id": str(submission_id),
                            "step_index": step.step_index,
                            "total_steps": step.total_steps,
                            "field_count": len(step.fields),
                            "field_summaries": [
                                _field_debug_summary(field) for field in step.fields
                            ],
                        },
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
                            "field_summaries": [
                                _field_debug_summary(field) for field in step.fields
                            ],
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
                    try:
                        step_answers, step_artifacts = await self._fill_step_fields(
                            page,
                            step,
                            settings,
                            posting=posting,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            execution_events=execution_events,
                            uploaded_cv_paths=uploaded_cv_paths,
                            submission_cv_path=submission_cv_path,
                            force_resume_reselection=force_resume_reselection,
                            force_semantic_reassert_keys=frozenset(force_semantic_reassert_keys),
                        )
                    except MissingRequiredProfileDataError as exc:
                        notes = str(exc)
                        update_progress_snapshot(
                            {
                                "current_stage": "submit_skipped",
                                "current_job": self._build_progress_job(
                                    posting,
                                    submission_id,
                                    status=SubmissionStatus.SKIPPED.value,
                                    skip_reason="missing_required_profile_data",
                                ),
                                "current_step": step.step_index + 1,
                                "last_error": notes,
                            },
                        )
                        artifacts.extend(
                            await self._capture_debug_bundle(
                                page,
                                run_dir=run_dir,
                                submission_id=submission_id,
                                label="skip_missing_profile_data",
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
                                "reason": "missing_required_profile_data",
                                "status": SubmissionStatus.SKIPPED.value,
                                "notes": notes,
                                "normalized_key": exc.normalized_key,
                                "question_type": exc.question_type.value,
                                "control_kind": exc.control_kind,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.SKIPPED,
                            resume_mode=resume_mode,
                            target_language=target_language,
                            matched_role_target=matched_role_target,
                            matched_specializations=matched_specializations,
                            notes=notes,
                            answers=tuple(answers),
                            execution_events=tuple(execution_events),
                            artifacts=tuple(artifacts),
                            recruiter_interactions=tuple(recruiter_interactions),
                            cv_version=submission_cv_version,
                        )
                    target_resume_name = (
                        submission_cv_path.name
                        if submission_cv_path is not None
                        else settings.profile.cv_filename
                    )
                    resume_verification = _evaluate_resume_verification(
                        step,
                        step_answers,
                        target_cv_name=target_resume_name,
                    )
                    if any(
                        field.question_type is QuestionType.RESUME_UPLOAD for field in step.fields
                    ):
                        if resume_verification.verified:
                            resume_review_verified_selection = True
                            resume_verification_required = False
                        else:
                            resume_verification_required = True
                            self._record_event(
                                execution_events,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                event_type=ExecutionEventType.STEP_REACHED,
                                payload={
                                    "stage": "easy_apply_resume_verification_deferred",
                                    "step_index": step.step_index,
                                    "target_cv_name": target_resume_name,
                                    "selected_value": resume_verification.selected_value,
                                    "option_visible": resume_verification.option_visible,
                                    "reason": resume_verification.reason,
                                },
                            )
                    if force_resume_reselection and any(
                        field.question_type is QuestionType.RESUME_UPLOAD for field in step.fields
                    ):
                        force_resume_reselection = False
                    answers.extend(step_answers)
                    artifacts.extend(step_artifacts)
                    await self._retry_invalid_fields_after_primary_action(
                        page,
                        settings=settings,
                        posting=posting,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        execution_events=execution_events,
                        previous_step=step,
                        step_answers=tuple(step_answers),
                        repair_origin="pre_primary_action",
                    )

                    review_repair_reason = await self._maybe_repair_easy_apply_review(
                        page,
                        step=step,
                        settings=settings,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        execution_events=execution_events,
                        submission_cv_path=submission_cv_path,
                        resume_review_repair_attempted=resume_review_repair_attempted,
                        resume_review_verified_selection=resume_review_verified_selection,
                    )
                    if review_repair_reason is not None:
                        if review_repair_reason == "resume_mismatch":
                            resume_review_repair_attempted = True
                            force_resume_reselection = True
                        elif (
                            review_repair_reason == "resume_preview_stale_after_verified_selection"
                        ):
                            notes = (
                                "LinkedIn Easy Apply kept showing a stale resume in the review "
                                "step even after the requested tailored resume appeared selected "
                                "earlier, so the submission was blocked to avoid sending the "
                                "wrong document."
                            )
                            artifacts.extend(
                                await self._capture_debug_bundle(
                                    page,
                                    run_dir=run_dir,
                                    submission_id=submission_id,
                                    label="failure_review_resume_stale_after_verification",
                                ),
                            )
                            self._record_event(
                                execution_events,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                event_type=ExecutionEventType.JOB_PROCESSED,
                                payload={
                                    "job_posting_id": str(posting.id),
                                    "reason": "resume_review_stale_after_verification",
                                    "status": SubmissionStatus.FAILED.value,
                                    "notes": notes,
                                },
                            )
                            return EasyApplyExecutionResult(
                                submission_id=submission_id,
                                started_at=started_at,
                                status=SubmissionStatus.FAILED,
                                resume_mode=resume_mode,
                                target_language=target_language,
                                matched_role_target=matched_role_target,
                                matched_specializations=matched_specializations,
                                notes=notes,
                                answers=tuple(answers),
                                execution_events=tuple(execution_events),
                                artifacts=tuple(artifacts),
                                recruiter_interactions=tuple(recruiter_interactions),
                                cv_version=submission_cv_version,
                            )
                        elif review_repair_reason == "invalid_city_review_value":
                            force_semantic_reassert_keys = {"city"}
                        continue
                    if (
                        target_resume_name
                        and "review your application" in normalize_text(step.surface_text)
                        and await self._review_resume_matches_requested_cv(
                            page,
                            target_cv_name=target_resume_name,
                        )
                    ):
                        resume_review_verified_selection = True
                    if (
                        resume_verification_required
                        and target_resume_name
                        and "review your application" in normalize_text(step.surface_text)
                    ):
                        if await self._review_resume_matches_requested_cv(
                            page,
                            target_cv_name=target_resume_name,
                        ):
                            resume_review_verified_selection = True
                            resume_verification_required = False
                        else:
                            notes = (
                                "LinkedIn Easy Apply could not verify that the requested tailored "
                                "resume is selected in the review step, so the submission was "
                                "blocked to avoid sending a stale resume."
                            )
                            artifacts.extend(
                                await self._capture_debug_bundle(
                                    page,
                                    run_dir=run_dir,
                                    submission_id=submission_id,
                                    label="failure_resume_verification",
                                ),
                            )
                            self._record_event(
                                execution_events,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                event_type=ExecutionEventType.JOB_PROCESSED,
                                payload={
                                    "job_posting_id": str(posting.id),
                                    "reason": "resume_verification_failed",
                                    "status": SubmissionStatus.FAILED.value,
                                    "notes": notes,
                                    "target_cv_name": target_resume_name,
                                },
                            )
                            return EasyApplyExecutionResult(
                                submission_id=submission_id,
                                started_at=started_at,
                                status=SubmissionStatus.FAILED,
                                resume_mode=resume_mode,
                                target_language=target_language,
                                matched_role_target=matched_role_target,
                                matched_specializations=matched_specializations,
                                notes=notes,
                                answers=tuple(answers),
                                execution_events=tuple(execution_events),
                                artifacts=tuple(artifacts),
                                recruiter_interactions=tuple(recruiter_interactions),
                                cv_version=submission_cv_version,
                            )
                    if resume_verification_required and not resume_review_verified_selection:
                        footer_label = await self._resume_submit_footer_label(
                            page,
                            step=step,
                        )
                        if footer_label is not None:
                            notes = (
                                "LinkedIn Easy Apply exposed the final submit action before "
                                "the requested tailored resume could be verified in the live "
                                "picker, so the submission was blocked to avoid sending a "
                                "stale resume."
                            )
                            artifacts.extend(
                                await self._capture_debug_bundle(
                                    page,
                                    run_dir=run_dir,
                                    submission_id=submission_id,
                                    label="failure_resume_unverified_pre_submit",
                                ),
                            )
                            self._record_event(
                                execution_events,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                event_type=ExecutionEventType.STEP_REACHED,
                                payload={
                                    "stage": "easy_apply_resume_submission_blocked",
                                    "step_index": step.step_index,
                                    "target_cv_name": target_resume_name,
                                    "footer_label": footer_label,
                                    "reason": "resume_unverified_before_submit",
                                },
                            )
                            self._record_event(
                                execution_events,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                event_type=ExecutionEventType.JOB_PROCESSED,
                                payload={
                                    "job_posting_id": str(posting.id),
                                    "reason": "resume_unverified_before_submit",
                                    "status": SubmissionStatus.FAILED.value,
                                    "notes": notes,
                                },
                            )
                            return EasyApplyExecutionResult(
                                submission_id=submission_id,
                                started_at=started_at,
                                status=SubmissionStatus.FAILED,
                                resume_mode=resume_mode,
                                target_language=target_language,
                                matched_role_target=matched_role_target,
                                matched_specializations=matched_specializations,
                                notes=notes,
                                answers=tuple(answers),
                                execution_events=tuple(execution_events),
                                artifacts=tuple(artifacts),
                                recruiter_interactions=tuple(recruiter_interactions),
                                cv_version=submission_cv_version,
                            )

                    try:
                        update_progress_snapshot(
                            {
                                "current_stage": "easy_apply_step_progression",
                                "current_job": self._build_progress_job(posting, submission_id),
                                "current_step": step.step_index + 1,
                            },
                        )
                        action = await self._progress_easy_apply_step_with_agent(
                            page,
                            settings=settings,
                            step=step,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            execution_events=execution_events,
                        )
                    except LinkedInEasyApplyError as exc:
                        if (
                            step.step_index >= step.total_steps - 1
                            and not step.fields
                            and await self._submit_transition_requires_job_page_recheck(page)
                        ):
                            success, recheck_notes = await self._confirm_submission_via_job_page(
                                page,
                                posting=posting,
                                settings=settings,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                execution_events=execution_events,
                            )
                            if success:
                                submitted_at = utc_now()
                                notes = recheck_notes or (
                                    "LinkedIn closed the dialog during final submission and "
                                    "the original vacancy now indicates the application was sent."
                                )
                                self._record_event(
                                    execution_events,
                                    execution_id=execution_id,
                                    submission_id=submission_id,
                                    event_type=ExecutionEventType.JOB_PROCESSED,
                                    payload={
                                        "job_posting_id": str(posting.id),
                                        "status": SubmissionStatus.SUBMITTED.value,
                                        "notes": notes,
                                        "submitted_at": submitted_at.isoformat(),
                                    },
                                )
                                return EasyApplyExecutionResult(
                                    submission_id=submission_id,
                                    started_at=started_at,
                                    status=SubmissionStatus.SUBMITTED,
                                    resume_mode=resume_mode,
                                    target_language=target_language,
                                    matched_role_target=matched_role_target,
                                    matched_specializations=matched_specializations,
                                    notes=notes,
                                    answers=tuple(answers),
                                    execution_events=tuple(execution_events),
                                    artifacts=tuple(artifacts),
                                    recruiter_interactions=tuple(recruiter_interactions),
                                    submitted_at=submitted_at,
                                    cv_version=submission_cv_version,
                                    cover_letter_version=None,
                                )
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
                            resume_mode=resume_mode,
                            target_language=target_language,
                            matched_role_target=matched_role_target,
                            matched_specializations=matched_specializations,
                            notes=notes,
                        )

                    if self._action_indicates_submission(action):
                        if resume_verification_required and not resume_review_verified_selection:
                            notes = (
                                "LinkedIn Easy Apply reached the submit action without a verified "
                                "selection of the requested tailored resume, so the submission "
                                "was blocked to avoid sending a stale resume."
                            )
                            artifacts.extend(
                                await self._capture_debug_bundle(
                                    page,
                                    run_dir=run_dir,
                                    submission_id=submission_id,
                                    label="failure_resume_unverified_submit",
                                ),
                            )
                            self._record_event(
                                execution_events,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                event_type=ExecutionEventType.JOB_PROCESSED,
                                payload={
                                    "job_posting_id": str(posting.id),
                                    "reason": "resume_unverified_before_submit",
                                    "status": SubmissionStatus.FAILED.value,
                                    "notes": notes,
                                },
                            )
                            return EasyApplyExecutionResult(
                                submission_id=submission_id,
                                started_at=started_at,
                                status=SubmissionStatus.FAILED,
                                resume_mode=resume_mode,
                                target_language=target_language,
                                matched_role_target=matched_role_target,
                                matched_specializations=matched_specializations,
                                notes=notes,
                                answers=tuple(answers),
                                execution_events=tuple(execution_events),
                                artifacts=tuple(artifacts),
                                recruiter_interactions=tuple(recruiter_interactions),
                                cv_version=submission_cv_version,
                            )
                        update_progress_snapshot(
                            {
                                "current_stage": "easy_apply_submit_triggered",
                                "current_job": self._build_progress_job(posting, submission_id),
                                "current_step": step.step_index + 1,
                            },
                        )
                        self._record_event(
                            execution_events,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            event_type=ExecutionEventType.SUBMIT_TRIGGERED,
                            payload={
                                "job_posting_id": str(posting.id),
                                "step_index": step.step_index,
                                "reasoning": action.reasoning,
                                "action_type": action.action_type,
                                "action_intent": action.action_intent,
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
                        if not success and await self._submit_transition_requires_job_page_recheck(
                            page
                        ):
                            success, recheck_notes = await self._confirm_submission_via_job_page(
                                page,
                                posting=posting,
                                settings=settings,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                execution_events=execution_events,
                            )
                            if success and recheck_notes:
                                outcome_notes = recheck_notes
                        if success:
                            update_progress_snapshot(
                                {
                                    "current_stage": "easy_apply_submitted",
                                    "current_job": self._build_progress_job(
                                        posting,
                                        submission_id,
                                        status=SubmissionStatus.SUBMITTED.value,
                                    ),
                                    "current_step": None,
                                },
                            )
                            await _maybe_send_job_application_email_from_executor(
                                self,
                                posting=posting,
                                settings=settings,
                                submission_cv_path=submission_cv_path,
                                execution_id=execution_id,
                                submission_id=submission_id,
                                execution_events=execution_events,
                            )
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
                                            "connect_path": recruiter_attempt.connect_path,
                                            "send_action": recruiter_attempt.send_action,
                                            "success_signal": recruiter_attempt.success_signal,
                                            "result_reason": recruiter_attempt.result_reason,
                                            "message_source": recruiter_attempt.message_source,
                                            "note_mode": recruiter_attempt.note_mode,
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
                                            "connect_path": recruiter_attempt.connect_path,
                                            "send_action": recruiter_attempt.send_action,
                                            "success_signal": recruiter_attempt.success_signal,
                                            "result_reason": recruiter_attempt.result_reason,
                                            "message_source": recruiter_attempt.message_source,
                                            "note_mode": recruiter_attempt.note_mode,
                                        },
                                    )
                            elif settings.agent.auto_connect_with_recruiter:
                                reason = (
                                    "feature_flag_disabled"
                                    if not self._runtime_settings.feature_recruiter_connect_enabled
                                    else "candidate_not_found"
                                )
                                self._record_event(
                                    execution_events,
                                    execution_id=execution_id,
                                    submission_id=submission_id,
                                    event_type=ExecutionEventType.RECRUITER_CONNECT_ATTEMPTED,
                                    payload={
                                        "job_posting_id": str(posting.id),
                                        "status": "skipped",
                                        "reason": reason,
                                    },
                                )
                                record_recruiter_connect_observation(
                                    counters=("candidate_not_found",),
                                    status="skipped",
                                    reason=reason,
                                    timeline_event="recruiter_connect_skipped",
                                    extra={
                                        "job_posting_id": str(posting.id),
                                        "external_job_id": posting.external_job_id,
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
                                resume_mode=resume_mode,
                                target_language=target_language,
                                matched_role_target=matched_role_target,
                                matched_specializations=matched_specializations,
                                notes=outcome_notes
                                or "LinkedIn Easy Apply submitted successfully.",
                                answers=tuple(answers),
                                recruiter_interactions=tuple(recruiter_interactions),
                                submitted_at=utc_now(),
                                cv_version=submission_cv_version,
                            )
                        update_progress_snapshot(
                            {
                                "current_stage": "easy_apply_submit_unconfirmed",
                                "current_job": self._build_progress_job(
                                    posting,
                                    submission_id,
                                    status=SubmissionStatus.FAILED.value,
                                ),
                                "current_step": step.step_index + 1,
                            },
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
                            resume_mode=resume_mode,
                            target_language=target_language,
                            matched_role_target=matched_role_target,
                            matched_specializations=matched_specializations,
                            notes=outcome_notes or "LinkedIn did not confirm the application.",
                        )

                    assessment_status, assessment_notes = await self._assess_easy_apply_step_state(
                        page,
                        settings=settings,
                        posting=posting,
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
                        update_progress_snapshot(
                            {
                                "current_stage": "easy_apply_blocked",
                                "current_job": self._build_progress_job(posting, submission_id),
                                "current_step": step.step_index + 1,
                            },
                        )
                        (
                            remediation_status,
                            remediation_notes,
                        ) = await self._resolve_easy_apply_bottleneck_with_agent(
                            page,
                            settings=settings,
                            posting=posting,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            execution_events=execution_events,
                            step=step,
                            blocked_summary=assessment_notes,
                            step_answers=tuple(step_answers),
                        )
                        if remediation_status != "blocked":
                            continue
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
                                "notes": remediation_notes or assessment_notes,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.FAILED,
                            resume_mode=resume_mode,
                            target_language=target_language,
                            matched_role_target=matched_role_target,
                            matched_specializations=matched_specializations,
                            notes=remediation_notes or assessment_notes,
                        )

                notes = "LinkedIn Easy Apply exceeded the maximum number of execution iterations."
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
                        "reason": "max_iterations_exceeded",
                        "status": SubmissionStatus.FAILED.value,
                        "notes": notes,
                    },
                )
                return EasyApplyExecutionResult(
                    submission_id=submission_id,
                    started_at=started_at,
                    status=SubmissionStatus.FAILED,
                    resume_mode=resume_mode,
                    target_language=target_language,
                    matched_role_target=matched_role_target,
                    matched_specializations=matched_specializations,
                    notes=notes,
                )
        except Exception as exc:  # noqa: BLE001
            update_progress_snapshot(
                {
                    "current_stage": "easy_apply_exception",
                    "current_job": self._build_progress_job(posting, submission_id),
                    "current_step": None,
                    "last_error": str(exc),
                },
            )
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
                resume_mode=resume_mode,
                target_language=target_language,
                matched_role_target=matched_role_target,
                matched_specializations=matched_specializations,
                notes=str(exc),
            )
        finally:
            if not page.is_closed():
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "linkedin_easy_apply_page_close_failed",
                        extra={
                            "job_posting_id": str(posting.id),
                            "submission_id": str(submission_id),
                        },
                    )

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
        submission_cv_path: Path | None,
        force_resume_reselection: bool = False,
        force_semantic_reassert_keys: frozenset[str] = frozenset(),
    ) -> tuple[list[ApplicationAnswer], list[ArtifactSnapshot]]:
        root = await self._easy_apply_root(page)
        answers: list[ApplicationAnswer] = []
        artifacts: list[ArtifactSnapshot] = []
        step_has_file_resume_control = any(
            field.question_type is QuestionType.RESUME_UPLOAD and field.control_kind == "file"
            for field in step.fields
        )
        semantic_candidate_fields = tuple(
            field
            for field in step.fields
            if field_needs_semantic_step_planning(field)
            and not (
                step_has_file_resume_control
                and field.question_type is QuestionType.RESUME_UPLOAD
                and field.control_kind != "file"
            )
        )
        semantic_step_plan = await self._answer_resolver.plan_step(
            step_index=step.step_index,
            total_steps=step.total_steps,
            surface_text=step.surface_text,
            fields=step.fields,
            candidate_fields=semantic_candidate_fields,
            settings=settings,
            posting=posting,
        )
        semantic_plan_by_ref = (
            {plan.field_ref: plan for plan in semantic_step_plan.field_plans}
            if semantic_step_plan is not None
            else {}
        )
        if semantic_candidate_fields:
            self._record_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "easy_apply_semantic_step_plan",
                    "step_index": step.step_index,
                    "candidate_field_refs": [
                        field_reference(field) for field in semantic_candidate_fields
                    ],
                    "planned_field_refs": list(semantic_plan_by_ref),
                },
            )

        for field in step.fields:
            if (
                step_has_file_resume_control
                and field.question_type is QuestionType.RESUME_UPLOAD
                and field.control_kind != "file"
            ):
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_resume_choice_skipped",
                        "step_index": step.step_index,
                        "normalized_key": field.normalized_key,
                        "control_kind": field.control_kind,
                        "reason": "resume_upload_control_present_in_same_step",
                    },
                )
                continue
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
            resume_field_matches_requested = (
                field.question_type is QuestionType.RESUME_UPLOAD
                and _resume_field_matches_requested_cv(
                    field,
                    submission_cv_path=submission_cv_path,
                    fallback_filename=settings.profile.cv_filename,
                )
            )
            force_resume_refresh = (
                field.question_type is QuestionType.RESUME_UPLOAD
                and submission_cv_path is not None
                and not resume_field_matches_requested
            )
            force_semantic_reassert = field.normalized_key in force_semantic_reassert_keys
            resolution: ResolvedFieldValue | None
            resolution_memory_entry: ApplyActionMemory | None = None
            resolution_memory_task_type: str | None = None
            resolution_memory_signature: dict[str, object] | None = None
            stale_resolution_memory: ApplyActionMemory | None = None
            if force_resume_refresh:
                assert submission_cv_path is not None
                resolution = ResolvedFieldValue(
                    value=submission_cv_path.name,
                    answer_source=AnswerSource.PROFILE_SNAPSHOT,
                    fill_strategy=FillStrategy.DETERMINISTIC,
                    confidence=1.0,
                )
            else:
                (
                    resolution_memory_entry,
                    resolution_memory_task_type,
                    resolution_memory_signature,
                    resolution,
                ) = self._replay_field_resolution_memory(field)
                if resolution is None:
                    resolution = await self._answer_resolver.resolve(
                        field,
                        settings,
                        posting=posting,
                        semantic_plan=semantic_plan_by_ref.get(field_reference(field)),
                    )
            if resolution is not None and resolution.fill_strategy is FillStrategy.ADAPTIVE_MEMORY:
                logger.info(
                    "linkedin_field_resolution_memory_replayed",
                    extra={
                        "step_index": step.step_index,
                        "normalized_key": field.normalized_key,
                        "question_type": field.question_type.value,
                        "control_kind": field.control_kind,
                    },
                )
            if (
                resolution is None
                and field.prefilled
                and field_has_meaningful_current_value(field)
                and _field_has_explicit_invalid_feedback(field)
            ):
                resolution = await self._answer_resolver.resolve_with_validation_feedback(
                    field,
                    settings,
                    posting=posting,
                    validation_message=field.helper_text or field.field_context,
                    current_value=field.current_value,
                    previous_answer=field.current_value,
                )
            if resolution is None and _field_requires_agentic_semantic_recovery(field):
                resolution = ResolvedFieldValue(
                    value=field.current_value,
                    answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
                    fill_strategy=FillStrategy.BEST_EFFORT,
                    ambiguity_flag=True,
                    confidence=0.05,
                    reasoning="semantic_retry_existing_field_value",
                )
            if (
                resolution is None
                and force_semantic_reassert
                and field_has_meaningful_current_value(field)
            ):
                resolution = ResolvedFieldValue(
                    value=field.current_value,
                    answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
                    fill_strategy=FillStrategy.BEST_EFFORT,
                    ambiguity_flag=True,
                    confidence=0.05,
                    reasoning="review_repair_reassert_existing_field_value",
                )

            if resolution is None:
                stage = (
                    "easy_apply_field_preserved"
                    if field.prefilled and field_has_meaningful_current_value(field)
                    else "easy_apply_field_unresolved"
                )
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": stage,
                        "step_index": step.step_index,
                        "question_raw": field.question_raw,
                        "normalized_key": field.normalized_key,
                        "question_type": field.question_type.value,
                        "control_kind": field.control_kind,
                        "required": field.required,
                        "prefilled": field.prefilled,
                        "current_value": field.current_value,
                        "classification_confidence": field.classification_confidence,
                        "field_context": field.field_context[:240],
                        "options_preview": list(field.options[:6]),
                        "force_semantic_reassert": force_semantic_reassert,
                    },
                )
                if field.required and not (
                    field.prefilled and field_has_meaningful_current_value(field)
                ):
                    raise MissingRequiredProfileDataError(
                        normalized_key=field.normalized_key,
                        question_type=field.question_type,
                        control_kind=field.control_kind,
                    )
                continue

            applied_value = await self._apply_field_value(
                page,
                root,
                field,
                resolution,
                settings,
                submission_cv_path=submission_cv_path,
                force_resume_reassert=force_resume_refresh,
                semantic_retry_required=(
                    _field_has_explicit_invalid_feedback(field) or force_semantic_reassert
                ),
                step_index=step.step_index,
                total_steps=step.total_steps,
            )
            if (
                applied_value is None
                and resolution_memory_entry is not None
                and resolution_memory_task_type is not None
            ):
                self._record_apply_memory_failure(
                    resolution_memory_entry,
                    task_type=resolution_memory_task_type,
                )
                stale_resolution_memory = resolution_memory_entry
                resolution_memory_entry = None
                resolution = await self._answer_resolver.resolve(
                    field,
                    settings,
                    posting=posting,
                    semantic_plan=semantic_plan_by_ref.get(field_reference(field)),
                )
                if resolution is not None:
                    applied_value = await self._apply_field_value(
                        page,
                        root,
                        field,
                        resolution,
                        settings,
                        submission_cv_path=submission_cv_path,
                        force_resume_reassert=force_resume_refresh,
                        semantic_retry_required=(
                            _field_has_explicit_invalid_feedback(field) or force_semantic_reassert
                        ),
                        step_index=step.step_index,
                        total_steps=step.total_steps,
                    )
            if applied_value is None:
                if field.question_type is QuestionType.RESUME_UPLOAD:
                    target_resume_name = (
                        submission_cv_path.name
                        if submission_cv_path is not None
                        else settings.profile.cv_filename
                    )
                    resume_state = await self._reload_resume_verification_state(
                        page=page,
                        field=field,
                        target_cv_name=target_resume_name,
                        step_index=step.step_index,
                        total_steps=step.total_steps,
                    )
                    if resume_state.verified and target_resume_name:
                        applied_value = target_resume_name
                    else:
                        self._record_event(
                            execution_events,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            event_type=ExecutionEventType.STEP_REACHED,
                            payload={
                                "stage": "easy_apply_resume_selection_unverified",
                                "step_index": step.step_index,
                                "normalized_key": field.normalized_key,
                                "question_type": field.question_type.value,
                                "control_kind": field.control_kind,
                                "answer_source": (
                                    resolution.answer_source.value
                                    if resolution is not None
                                    else None
                                ),
                                "fill_strategy": (
                                    resolution.fill_strategy.value
                                    if resolution is not None
                                    else None
                                ),
                                "target_cv_name": target_resume_name,
                                "selected_value": resume_state.selected_value,
                                "option_visible": resume_state.option_visible,
                                "reason": resume_state.reason,
                            },
                        )
                        continue
            if applied_value is None:
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_field_apply_failed",
                        "step_index": step.step_index,
                        "normalized_key": field.normalized_key,
                        "question_type": field.question_type.value,
                        "control_kind": field.control_kind,
                        "answer_source": (
                            resolution.answer_source.value if resolution is not None else None
                        ),
                        "fill_strategy": (
                            resolution.fill_strategy.value if resolution is not None else None
                        ),
                    },
                )
                continue
            assert resolution is not None
            if resolution_memory_entry is not None and resolution_memory_task_type is not None:
                self._record_apply_memory_success(
                    resolution_memory_entry,
                    task_type=resolution_memory_task_type,
                )
            elif (
                resolution_memory_signature is not None
                and resolution_memory_task_type is not None
                and self._should_promote_field_resolution_memory(field, resolution)
            ):
                self._promote_field_resolution_memory(
                    task_type=resolution_memory_task_type,
                    signature_payload=resolution_memory_signature,
                    field=field,
                    resolved_value=applied_value,
                    existing_memory=stale_resolution_memory,
                    replace_existing=stale_resolution_memory is not None,
                )

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
            self._record_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "easy_apply_field_applied",
                    "step_index": step.step_index,
                    "normalized_key": field.normalized_key,
                    "question_type": field.question_type.value,
                    "control_kind": field.control_kind,
                    "answer_source": resolution.answer_source.value,
                    "fill_strategy": resolution.fill_strategy.value,
                    "ambiguity_flag": resolution.ambiguity_flag,
                    "confidence": resolution.confidence,
                    "reasoning": resolution.reasoning,
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
                cv_path = str(submission_cv_path or Path(settings.profile.cv_path))
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
        *,
        submission_cv_path: Path | None,
        force_resume_reassert: bool = False,
        semantic_retry_required: bool = False,
        step_index: int | None = None,
        total_steps: int | None = None,
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
                state = await self._inspect_text_field_interaction(locator)
                if self._text_field_requires_interactive_selection(state):
                    try:
                        return await asyncio.wait_for(
                            self._complete_text_field_interaction(
                                page=page,
                                field=field,
                                target_value=resolution.value,
                                settings=settings,
                                semantic_retry_required=semantic_retry_required,
                                last_known_step_index=step_index,
                                last_known_total_steps=total_steps,
                            ),
                            timeout=(
                                self._runtime_settings.linkedin_field_interaction_timeout_seconds
                            ),
                        )
                    except TimeoutError as exc:
                        msg = (
                            "Timed out while the browser agent was trying to finalize an "
                            "interactive Easy Apply field."
                        )
                        raise LinkedInEasyApplyError(msg) from exc
                committed_state = await self._commit_typed_text_field_entry(
                    page=page,
                    locator=locator,
                    initial_state=state,
                )
                if semantic_retry_required or committed_state.invalid:
                    try:
                        return await asyncio.wait_for(
                            self._complete_text_field_interaction(
                                page=page,
                                field=field,
                                target_value=resolution.value,
                                settings=settings,
                                semantic_retry_required=semantic_retry_required,
                                last_known_step_index=step_index,
                                last_known_total_steps=total_steps,
                            ),
                            timeout=(
                                self._runtime_settings.linkedin_field_interaction_timeout_seconds
                            ),
                        )
                    except TimeoutError as exc:
                        msg = (
                            "Timed out while the browser agent was trying to finalize an "
                            "interactive LinkedIn Easy Apply chooser field."
                        )
                        raise LinkedInEasyApplyError(msg) from exc
                return committed_state.current_value or resolution.value
            case "select":
                if field.question_type is QuestionType.RESUME_UPLOAD:
                    return await self._apply_resume_choice_field(
                        page=page,
                        root=root,
                        field=field,
                        settings=settings,
                        submission_cv_path=submission_cv_path,
                        force_reassert=force_resume_reassert,
                    )
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
                if field.question_type is QuestionType.RESUME_UPLOAD:
                    return await self._apply_resume_choice_field(
                        page=page,
                        root=root,
                        field=field,
                        settings=settings,
                        submission_cv_path=submission_cv_path,
                        force_reassert=force_resume_reassert,
                    )
                locator = await self._find_control_locator(root, field)
                if locator is None:
                    return None
                should_check = normalize_text(resolution.value) in {"yes", "true", "1"}
                if await self._set_checkbox_state(
                    page=page,
                    root=root,
                    field=field,
                    locator=locator,
                    desired_checked=should_check,
                ):
                    return "Yes" if should_check else "No"
                try:
                    if await asyncio.wait_for(
                        self._complete_checkbox_interaction(
                            page=page,
                            field=field,
                            settings=settings,
                            desired_checked=should_check,
                        ),
                        timeout=(self._runtime_settings.linkedin_field_interaction_timeout_seconds),
                    ):
                        return "Yes" if should_check else "No"
                except TimeoutError as exc:
                    msg = (
                        "Timed out while the browser agent was trying to finalize a "
                        "LinkedIn Easy Apply checkbox."
                    )
                    raise LinkedInEasyApplyError(msg) from exc
                msg = "Could not set the LinkedIn Easy Apply checkbox to the requested state."
                raise LinkedInEasyApplyError(msg)
            case "radio":
                if field.question_type is QuestionType.RESUME_UPLOAD:
                    return await self._apply_resume_choice_field(
                        page=page,
                        root=root,
                        field=field,
                        settings=settings,
                        submission_cv_path=submission_cv_path,
                        force_reassert=force_resume_reassert,
                        step_index=step_index,
                        total_steps=total_steps,
                    )
                option_index = _pick_option_index(field.options, preferred=resolution.value)
                if option_index is None:
                    return None
                option = field.options[option_index]
                if await self._check_radio_option(root, field, option):
                    return option
                try:
                    if await asyncio.wait_for(
                        self._complete_radio_interaction(
                            page=page,
                            field=field,
                            option_index=option_index,
                            option_label=option,
                            settings=settings,
                        ),
                        timeout=(self._runtime_settings.linkedin_field_interaction_timeout_seconds),
                    ):
                        return option
                except TimeoutError as exc:
                    msg = (
                        "Timed out while the browser agent was trying to finalize a "
                        "LinkedIn Easy Apply radio field."
                    )
                    raise LinkedInEasyApplyError(msg) from exc
                return None
            case "file":
                locator = await self._find_control_locator(root, field)
                resolved_cv_path = submission_cv_path or _existing_path(settings.profile.cv_path)
                if locator is None or resolved_cv_path is None:
                    return None
                await locator.set_input_files(resolved_cv_path)
                target_cv_name = (
                    submission_cv_path.name
                    if submission_cv_path is not None
                    else settings.profile.cv_filename or resolved_cv_path.name
                )
                settle_state = await self._await_resume_upload_settlement(
                    page,
                    target_cv_name=target_cv_name,
                )
                if settle_state.settled:
                    return settings.profile.cv_filename or resolved_cv_path.name
                return None

    async def _complete_text_field_interaction(
        self,
        *,
        page: Page,
        field: EasyApplyField,
        target_value: str,
        settings: UserAgentSettings,
        semantic_retry_required: bool = False,
        last_known_step_index: int | None = None,
        last_known_total_steps: int | None = None,
    ) -> str:
        browser_agent = self._create_browser_agent(settings)
        initial_root = await self._easy_apply_root(page)
        initial_locator = await self._find_control_locator(initial_root, field)
        if initial_locator is None:
            msg = (
                "LinkedIn removed the current form field while the agent was trying to finalize it."
            )
            raise LinkedInEasyApplyError(msg)
        initial_state = await self._inspect_text_field_interaction(initial_locator)
        signature_payload = build_field_task_signature(
            task_type=TASK_FINALIZE_FIELD,
            field=field,
            visible_option_texts=initial_state.visible_option_texts,
            validation_message=initial_state.validation_message,
        )

        memory_entry, _, _ = await self._attempt_replay_apply_memory(
            browser_agent=browser_agent,
            page=page,
            task_type=TASK_FINALIZE_FIELD,
            signature_payload=signature_payload,
            available_values={"intended_field_value": target_value},
            focus_locator=initial_locator,
            priority_locator=initial_locator,
        )
        if memory_entry is not None:
            root_after_memory = await self._easy_apply_root(page)
            locator_after_memory = await self._find_control_locator(root_after_memory, field)
            if locator_after_memory is not None:
                memory_state = await self._inspect_text_field_interaction(locator_after_memory)
                if await self._text_field_interaction_resolved(
                    page,
                    state=memory_state,
                    field=field,
                    semantic_retry_required=semantic_retry_required,
                    last_known_step_index=last_known_step_index,
                    last_known_total_steps=last_known_total_steps,
                ):
                    self._record_apply_memory_success(memory_entry, task_type=TASK_FINALIZE_FIELD)
                    return memory_state.current_value or target_value
            self._record_apply_memory_failure(memory_entry, task_type=TASK_FINALIZE_FIELD)

        root = await self._easy_apply_root(page)
        locator = await self._find_control_locator(root, field)
        if locator is None:
            msg = (
                "LinkedIn removed the current form field while the agent was trying to finalize it."
            )
            raise LinkedInEasyApplyError(msg)
        state = await self._inspect_text_field_interaction(locator)
        if await self._text_field_interaction_resolved(
            page,
            state=state,
            field=field,
            semantic_retry_required=semantic_retry_required,
            last_known_step_index=last_known_step_index,
            last_known_total_steps=last_known_total_steps,
        ):
            return state.current_value or target_value

        focus_locator = await self._field_interaction_focus_locator(root, field)

        async def field_interaction_complete(candidate_page: Page) -> bool:
            candidate_root = await self._easy_apply_root(candidate_page)
            candidate_locator = await self._find_control_locator(candidate_root, field)
            if candidate_locator is None:
                return True
            candidate_state = await self._inspect_text_field_interaction(candidate_locator)
            return await self._text_field_interaction_resolved(
                candidate_page,
                state=candidate_state,
                field=field,
                semantic_retry_required=semantic_retry_required,
                last_known_step_index=last_known_step_index,
                last_known_total_steps=last_known_total_steps,
            )

        try:
            await browser_agent.complete_browser_task(
                page=page,
                available_values={"intended_field_value": target_value},
                goal=(
                    "Get the current LinkedIn Easy Apply field accepted by its own "
                    "chooser, autocomplete, or suggestion widget without changing the "
                    "underlying intended answer."
                ),
                timeout_seconds=self._runtime_settings.linkedin_field_interaction_timeout_seconds,
                task_name="linkedin_easy_apply_finalize_interactive_field",
                is_complete=field_interaction_complete,
                extra_rules=(
                    f"The active field label is {field.question_raw!r}.",
                    f"The intended accepted answer is {target_value!r}.",
                    (
                        f"The field currently contains {state.current_value!r}."
                        if state.current_value
                        else "The field is currently blank."
                    ),
                    (
                        f"Visible field validation feedback: {state.validation_message!r}."
                        if state.validation_message
                        else "No explicit validation text is currently visible for this field."
                    ),
                    (
                        "Visible chooser options right now: "
                        f"{', '.join(state.visible_option_texts)!r}."
                        if state.visible_option_texts
                        else "No chooser options are visible in the current snapshot yet."
                    ),
                    (
                        f"The widget reports aria-autocomplete={state.aria_autocomplete!r}, "
                        f"aria-expanded={state.aria_expanded}, "
                        f"has_popup_binding={state.has_popup_binding}, "
                        f"active_descendant={state.active_descendant!r}."
                    ),
                    (
                        "The current step has already rejected this field once. Existing text "
                        "in the box is not sufficient by itself; the widget must accept or "
                        "commit the answer."
                        if semantic_retry_required
                        else "Treat this as an interactive field finalization task."
                    ),
                    (
                        "This control may look like plain text, but if the UI exposes chooser, "
                        "typeahead, listbox, or suggestion behavior, you must satisfy that "
                        "widget rather than only typing text."
                    ),
                    (
                        "If suggestions appear, select the best semantically matching option "
                        "for the intended answer."
                    ),
                    (
                        "If no options are visible yet, recover focus, reopen the chooser, "
                        "adjust the query only when needed to surface equivalent options, wait "
                        "for the UI, and then commit the best match."
                    ),
                    (
                        "Once the field has a non-empty value, no visible validation error, "
                        "and the current step no longer presents this field as rejected, you may "
                        "treat the chooser interaction as complete even if the widget still "
                        "retains autocomplete wiring such as aria-owns or aria-activedescendant."
                    ),
                    (
                        "The intended answer may need a UI-friendly query variant, but do not "
                        "drift to a different meaning."
                    ),
                    (
                        "Do not click Continue, Next, Review, Submit, Back, Dismiss, Close, or "
                        "any other step-level control during this task."
                    ),
                    (
                        "Stay inside the scoped field region and any chooser options connected "
                        "to this field."
                    ),
                    (
                        "Use done only when the widget itself appears to have accepted the "
                        "answer or when the field disappears because the page no longer needs "
                        "it."
                    ),
                ),
                allowed_action_types=("click", "fill", "press", "scroll", "wait", "done", "fail"),
                focus_locator=focus_locator or locator,
                priority_locator=locator,
            )
        except BrowserAutomationError as exc:
            raise LinkedInEasyApplyError(str(exc)) from exc

        root = await self._easy_apply_root(page)
        locator = await self._find_control_locator(root, field)
        if locator is None:
            return target_value
        final_state = await self._inspect_text_field_interaction(locator)
        if await self._text_field_interaction_resolved(
            page,
            state=final_state,
            field=field,
            semantic_retry_required=semantic_retry_required,
            last_known_step_index=last_known_step_index,
            last_known_total_steps=last_known_total_steps,
        ):
            return final_state.current_value or target_value
        if (
            self._text_field_requires_interactive_selection(final_state)
            or not final_state.has_value
            or not await self._field_text_value_semantically_accepted(
                page,
                field=field,
                semantic_retry_required=semantic_retry_required,
                last_known_step_index=last_known_step_index,
                last_known_total_steps=last_known_total_steps,
            )
        ):
            msg = (
                "Browser agent could not finish the interactive field flow for the current "
                "LinkedIn Easy Apply step."
            )
            raise LinkedInEasyApplyError(msg)
        return final_state.current_value or target_value

    async def _commit_typed_text_field_entry(
        self,
        *,
        page: Page,
        locator: Locator,
        initial_state: TextFieldInteractionState,
    ) -> TextFieldInteractionState:
        state = initial_state
        if not state.focused and not state.invalid:
            return state
        for settle_action in ("blur", "tab"):
            try:
                if settle_action == "blur":
                    await locator.evaluate("(node) => node.blur()")
                else:
                    await locator.press("Tab")
                await page.wait_for_timeout(150)
            except Exception:  # noqa: BLE001
                continue
            try:
                state = await self._inspect_text_field_interaction(locator)
            except Exception:  # noqa: BLE001
                continue
            if not state.focused or not state.invalid:
                return state
        return state

    async def _inspect_text_field_interaction(self, locator: Locator) -> TextFieldInteractionState:
        try:
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
              const looksLikeCharacterCounter = (text) =>
                /^\\d+\\s*\\/\\s*\\d+(?:\\s+\\d+\\s+\\S+\\s+\\d+(?:\\s+\\S+)*)?$/i.test(text);
              const hasErrorSignal = (candidate) => {
                if (!candidate || candidate.nodeType !== 1) {
                  return false;
                }
                const role = collapse(candidate.getAttribute("role"));
                const ariaLive = collapse(candidate.getAttribute("aria-live"));
                const metadata = collapse(
                  [
                    candidate.getAttribute("class"),
                    candidate.getAttribute("id"),
                    candidate.getAttribute("data-test-form-element-error-messages"),
                  ]
                    .filter(Boolean)
                    .join(" ")
                );
                return (
                  role === "alert"
                  || ariaLive === "assertive"
                  || candidate.getAttribute("aria-invalid") === "true"
                  || /(?:^|\\s)(?:error|invalid|warning)(?:\\s|$)/i.test(metadata)
                  || metadata.includes("artdeco-inline-feedback__message")
                  || metadata.includes("fb-dash-form-element__error-message")
                );
              };
              const isPotentialOptionNode = (candidate) => {
                if (!candidate || candidate.nodeType !== 1) {
                  return false;
                }
                const role = collapse(candidate.getAttribute("role"));
                if (role === "option") {
                  return true;
                }
                if (
                  candidate.hasAttribute("aria-selected")
                  || candidate.hasAttribute("data-value")
                ) {
                  return true;
                }
                const tagName = (candidate.tagName || "").toLowerCase();
                if (tagName !== "li") {
                  return false;
                }
                const parent = candidate.parentElement;
                const parentRole = collapse(parent?.getAttribute("role"));
                const metadata = collapse(
                  [
                    candidate.getAttribute("class"),
                    parent?.getAttribute("class"),
                    parent?.getAttribute("id"),
                    parent?.getAttribute("aria-label"),
                  ]
                    .filter(Boolean)
                    .join(" ")
                );
                return (
                  parentRole === "listbox"
                  || parentRole === "menu"
                  || /option|select|dropdown|autocomplete|typeahead|suggest/i.test(metadata)
                );
              };
              const isNearbyOptionForField = (candidate) => {
                if (
                  !candidate
                  || candidate.nodeType !== 1
                  || candidate === node
                  || candidate.contains(node)
                  || node.contains(candidate)
                  || !isVisible(candidate)
                  || !isPotentialOptionNode(candidate)
                ) {
                  return false;
                }
                const rect = candidate.getBoundingClientRect();
                const verticalGap = rect.top - fieldRect.bottom;
                const aboveFieldGap = fieldRect.top - rect.bottom;
                if (verticalGap > 420 || aboveFieldGap > 48) {
                  return false;
                }
                if (overlapsHorizontally(rect)) {
                  return true;
                }
                const fieldCenter = fieldRect.left + (fieldRect.width / 2);
                const candidateCenter = rect.left + (rect.width / 2);
                return Math.abs(fieldCenter - candidateCenter) <= Math.max(96, fieldRect.width);
              };
              const explicitValidationTexts = [];
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
                if (looksLikeCharacterCounter(text) && !hasErrorSignal(candidate)) {
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
                explicitValidationTexts.push({ text, verticalGap });
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
              for (const attributeName of ["aria-errormessage"]) {
                for (const id of splitIds(node.getAttribute(attributeName))) {
                  pushValidationText(document.getElementById(id));
                }
              }
              for (const id of splitIds(node.getAttribute("aria-activedescendant"))) {
                pushRoot(document.getElementById(id));
              }
              const validationScopes = [];
              const pushValidationScope = (candidate) => {
                if (
                  !candidate
                  || candidate.nodeType !== 1
                  || validationScopes.includes(candidate)
                ) {
                  return;
                }
                validationScopes.push(candidate);
              };
              pushValidationScope(
                node.closest(
                  [
                    ".fb-form-element",
                    ".jobs-easy-apply-form-section__grouping",
                    ".jobs-easy-apply-form-element",
                    "[role='group']",
                    "fieldset",
                    "section",
                    "form",
                  ].join(", ")
                )
              );
              pushValidationScope(node.parentElement);
              let ancestor = node.parentElement;
              let depth = 0;
              while (ancestor && depth < 3) {
                for (const candidate of ancestor.querySelectorAll(
                  [
                    "[role='alert']",
                    "[aria-live='assertive']",
                    "[aria-live='polite']",
                    ".artdeco-inline-feedback__message",
                    ".fb-dash-form-element__error-message",
                    "[data-test-form-element-error-messages]",
                  ].join(", ")
                )) {
                  pushValidationText(candidate);
                }
                ancestor = ancestor.parentElement;
                depth += 1;
              }
              for (const scope of validationScopes) {
                for (const candidate of scope.querySelectorAll(
                  [
                    "[role='alert']",
                    "[aria-live='assertive']",
                    "[aria-live='polite']",
                    ".artdeco-inline-feedback__message",
                    ".fb-dash-form-element__error-message",
                    "[data-test-form-element-error-messages]",
                  ].join(", ")
                )) {
                  pushValidationText(candidate);
                }
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
              if (optionTexts.length === 0) {
                for (const optionNode of document.querySelectorAll(
                  "[role='option'], [aria-selected], [data-value], li"
                )) {
                  if (!isNearbyOptionForField(optionNode)) {
                    continue;
                  }
                  pushOption(optionNode);
                }
              }
              explicitValidationTexts.sort((left, right) => {
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
                ...explicitValidationTexts.map((item) => item.text),
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
                """,
                timeout=_FIELD_STATE_INSPECTION_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError as exc:
            msg = (
                "Timed out while inspecting the current LinkedIn Easy Apply field after a "
                "browser action."
            )
            raise LinkedInEasyApplyError(msg) from exc
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

    async def _field_text_value_semantically_accepted(
        self,
        page: Page,
        *,
        field: EasyApplyField,
        semantic_retry_required: bool,
        last_known_step_index: int | None,
        last_known_total_steps: int | None,
    ) -> bool:
        if not semantic_retry_required:
            return True
        if last_known_step_index is None or last_known_total_steps is None:
            return True
        try:
            current_step = await self._extract_step(
                page,
                last_known_step_index=last_known_step_index,
                last_known_total_steps=last_known_total_steps,
            )
        except LinkedInEasyApplyError:
            return True
        if current_step.step_index != last_known_step_index:
            return True
        matching_field = next(
            (
                candidate
                for candidate in current_step.fields
                if _same_step_field_identity(candidate, field)
            ),
            None,
        )
        if matching_field is None:
            return True
        if not field_has_meaningful_current_value(matching_field):
            return False
        return not _validation_feedback_requires_semantic_retry(
            field=matching_field,
            normalized_validation=normalize_text(
                " ".join(
                    fragment
                    for fragment in (
                        matching_field.helper_text or "",
                        matching_field.field_context,
                    )
                    if fragment
                )
            ),
        )

    def _text_field_interaction_complete(self, state: TextFieldInteractionState) -> bool:
        return (
            state.has_value
            and not state.invalid
            and not self._text_field_requires_interactive_selection(state)
        )

    async def _text_field_interaction_resolved(
        self,
        page: Page,
        *,
        state: TextFieldInteractionState,
        field: EasyApplyField,
        semantic_retry_required: bool,
        last_known_step_index: int | None,
        last_known_total_steps: int | None,
    ) -> bool:
        if not state.has_value or state.invalid:
            return False
        if not await self._field_text_value_semantically_accepted(
            page,
            field=field,
            semantic_retry_required=semantic_retry_required,
            last_known_step_index=last_known_step_index,
            last_known_total_steps=last_known_total_steps,
        ):
            return False
        if semantic_retry_required:
            return True
        return not self._text_field_requires_interactive_selection(state)

    def _text_field_requires_interactive_selection(
        self,
        state: TextFieldInteractionState,
    ) -> bool:
        autocomplete_binding = bool(
            state.aria_autocomplete is not None
            and (
                state.has_popup_binding
                or state.aria_expanded
                or state.active_descendant
                or state.role == "combobox"
            )
        )
        return bool(
            state.visible_option_count
            or state.aria_expanded
            or state.active_descendant
            or state.role == "combobox"
            or autocomplete_binding
        )

    async def _apply_resume_choice_field(
        self,
        *,
        page: Page,
        root: Locator,
        field: EasyApplyField,
        settings: UserAgentSettings,
        submission_cv_path: Path | None,
        force_reassert: bool = False,
        step_index: int | None = None,
        total_steps: int | None = None,
        target_cv_name_override: str | None = None,
        allow_upload: bool = True,
    ) -> str | None:
        target_cv_name = target_cv_name_override or (
            submission_cv_path.name
            if submission_cv_path is not None
            else settings.profile.cv_filename
        )
        if target_cv_name is None:
            return None
        if not force_reassert and _resume_text_matches_requested_cv(
            field.current_value,
            target_cv_name,
        ):
            return target_cv_name
        if force_reassert and submission_cv_path is not None:
            if await self._upload_resume_from_choice_step(
                page=page,
                root=root,
                submission_cv_path=submission_cv_path,
                target_cv_name=target_cv_name,
            ):
                refreshed_root = await self._easy_apply_root(page)
                if await self._resume_picker_selection_matches_requested_cv(
                    refreshed_root,
                    target_cv_name=target_cv_name,
                ):
                    return target_cv_name
        option_index = _pick_resume_option_index(field.options, target_cv_name)
        if option_index is None:
            if (
                allow_upload
                and submission_cv_path is not None
                and await self._upload_resume_from_choice_step(
                    page=page,
                    root=root,
                    submission_cv_path=submission_cv_path,
                    target_cv_name=target_cv_name,
                )
            ):
                refreshed_root = await self._easy_apply_root(page)
                if await self._resume_picker_selection_matches_requested_cv(
                    refreshed_root,
                    target_cv_name=target_cv_name,
                ):
                    return target_cv_name
                refreshed_field = await self._reload_resume_choice_field(
                    page=page,
                    field=field,
                    step_index=step_index,
                    total_steps=total_steps,
                )
                if refreshed_field is not None:
                    return await self._apply_resume_choice_field(
                        page=page,
                        root=refreshed_root,
                        field=refreshed_field,
                        settings=settings,
                        submission_cv_path=None,
                        force_reassert=True,
                        step_index=step_index,
                        total_steps=total_steps,
                        target_cv_name_override=target_cv_name,
                        allow_upload=False,
                    )
                return None
            if submission_cv_path is not None:
                msg = (
                    "LinkedIn Easy Apply did not expose the requested dynamic resume in the "
                    "resume picker and the upload path could not be completed."
                )
                raise LinkedInEasyApplyError(msg)
            return None

        match field.control_kind:
            case "radio":
                if force_reassert:
                    alternate_option_index = _pick_alternate_resume_option_index(
                        field.options,
                        target_cv_name,
                    )
                    if alternate_option_index is not None:
                        await self._check_radio_option_by_index(
                            root,
                            field,
                            option_index=alternate_option_index,
                            force_activate=True,
                        )
                        await page.wait_for_timeout(250)
                if await self._check_radio_option_by_index(
                    root,
                    field,
                    option_index=option_index,
                    force_activate=force_reassert,
                ):
                    if force_reassert:
                        await page.wait_for_timeout(450)
                    refreshed_root = await self._easy_apply_root(page)
                    if await self._resume_picker_selection_matches_requested_cv(
                        refreshed_root,
                        target_cv_name=target_cv_name,
                    ):
                        return target_cv_name
                if await self._complete_radio_interaction(
                    page=page,
                    field=field,
                    option_index=option_index,
                    option_label=target_cv_name,
                    settings=settings,
                ):
                    refreshed_root = await self._easy_apply_root(page)
                    if await self._resume_picker_selection_matches_requested_cv(
                        refreshed_root,
                        target_cv_name=target_cv_name,
                    ):
                        return target_cv_name
            case "checkbox":
                locator = await self._find_control_locator(root, field)
                if locator is not None and await self._set_checkbox_state(
                    page=page,
                    root=root,
                    field=field,
                    locator=locator,
                    desired_checked=True,
                ):
                    refreshed_root = await self._easy_apply_root(page)
                    if await self._resume_picker_selection_matches_requested_cv(
                        refreshed_root,
                        target_cv_name=target_cv_name,
                    ):
                        return target_cv_name
                if await self._complete_checkbox_interaction(
                    page=page,
                    field=field,
                    settings=settings,
                    desired_checked=True,
                ):
                    refreshed_root = await self._easy_apply_root(page)
                    if await self._resume_picker_selection_matches_requested_cv(
                        refreshed_root,
                        target_cv_name=target_cv_name,
                    ):
                        return target_cv_name
            case "select":
                locator = await self._find_control_locator(root, field)
                if locator is not None:
                    await self._select_field_option(locator, field, option_index=option_index)
                    refreshed_root = await self._easy_apply_root(page)
                    if await self._resume_picker_selection_matches_requested_cv(
                        refreshed_root,
                        target_cv_name=target_cv_name,
                    ):
                        return target_cv_name
        return None

    async def _reload_resume_choice_field(
        self,
        *,
        page: Page,
        field: EasyApplyField,
        step_index: int | None,
        total_steps: int | None,
    ) -> EasyApplyField | None:
        if step_index is None or total_steps is None:
            return None
        try:
            current_step = await self._extract_step(
                page,
                last_known_step_index=step_index,
                last_known_total_steps=total_steps,
            )
        except LinkedInEasyApplyError:
            return None
        if current_step.step_index != step_index:
            return None
        return next(
            (
                candidate
                for candidate in current_step.fields
                if _same_step_field_identity(candidate, field)
            ),
            next(
                (
                    candidate
                    for candidate in current_step.fields
                    if candidate.question_type is QuestionType.RESUME_UPLOAD
                ),
                None,
            ),
        )

    async def _reload_resume_verification_state(
        self,
        *,
        page: Page,
        field: EasyApplyField,
        target_cv_name: str | None,
        step_index: int | None,
        total_steps: int | None,
    ) -> ResumeVerificationState:
        refreshed_field = await self._reload_resume_choice_field(
            page=page,
            field=field,
            step_index=step_index,
            total_steps=total_steps,
        )
        if refreshed_field is None:
            return _resume_field_verification_state(
                field,
                target_cv_name=target_cv_name,
            )
        return _resume_field_verification_state(
            refreshed_field,
            target_cv_name=target_cv_name,
        )

    async def _resume_picker_selection_matches_requested_cv(
        self,
        root: Locator,
        *,
        target_cv_name: str,
    ) -> bool:
        try:
            payload = await root.evaluate(
                """
                (node, { targetCvName }) => {
                  const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
                  const normalize = (value) => collapse(value).toLowerCase();
                  const target = normalize(targetCvName);
                  if (!target) {
                    return false;
                  }
                  const isVisible = (element) => {
                    if (!(element instanceof Element)) {
                      return false;
                    }
                    const style = window.getComputedStyle(element);
                    if (
                      style.display === "none"
                      || style.visibility === "hidden"
                      || style.opacity === "0"
                    ) {
                      return false;
                    }
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const matchesTarget = (text) => {
                    const normalizedText = normalize(text);
                    return Boolean(normalizedText && normalizedText.includes(target));
                  };

                  const radios = Array.from(node.querySelectorAll('input[type="radio"]'));
                  for (const radio of radios) {
                    if (!(radio instanceof HTMLInputElement) || !radio.checked) {
                      continue;
                    }
                    const card = radio.closest('[role="button"], label, fieldset, div');
                    const cardText = collapse(card?.innerText || card?.textContent || "");
                    if (matchesTarget(cardText)) {
                      return true;
                    }
                  }

                  for (const roleRadio of node.querySelectorAll(
                    '[role="radio"][aria-checked="true"]'
                  )) {
                    if (!isVisible(roleRadio)) {
                      continue;
                    }
                    const text = collapse(roleRadio.innerText || roleRadio.textContent || "");
                    if (matchesTarget(text)) {
                      return true;
                    }
                  }
                  const selectedCards = Array.from(
                    node.querySelectorAll(
                      '[aria-selected="true"], [data-selected="true"], [aria-current="true"]'
                    )
                  );
                  for (const card of selectedCards) {
                    if (!isVisible(card)) {
                      continue;
                    }
                    const text = collapse(card.innerText || card.textContent || "");
                    if (matchesTarget(text)) {
                      return true;
                    }
                  }
                  return false;
                }
                """,
                {"targetCvName": target_cv_name},
            )
        except Exception:  # noqa: BLE001
            return False
        return bool(payload)

    async def _inspect_resume_upload_settlement(
        self,
        page: Page,
        *,
        target_cv_name: str,
    ) -> ResumeUploadSettleState:
        if not target_cv_name:
            return ResumeUploadSettleState()
        root = await self._easy_apply_root(page)
        try:
            payload = await root.evaluate(
                """
                (node, { targetCvName }) => {
                  const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
                  const normalize = (value) => collapse(value).toLowerCase();
                  const target = normalize(targetCvName);
                  const isVisible = (element) => {
                    if (!(element instanceof Element)) {
                      return false;
                    }
                    const style = window.getComputedStyle(element);
                    if (
                      style.display === "none"
                      || style.visibility === "hidden"
                      || style.opacity === "0"
                    ) {
                      return false;
                    }
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const matchesTarget = (text) => {
                    const normalizedText = normalize(text);
                    return Boolean(normalizedText && normalizedText.includes(target));
                  };

                  let selectionMatches = false;
                  const radios = Array.from(node.querySelectorAll('input[type="radio"]'));
                  for (const radio of radios) {
                    if (!(radio instanceof HTMLInputElement) || !radio.checked) {
                      continue;
                    }
                    const card = radio.closest('[role="button"], label, fieldset, div');
                    const cardText = collapse(card?.innerText || card?.textContent || "");
                    if (matchesTarget(cardText)) {
                      selectionMatches = true;
                      break;
                    }
                  }
                  if (!selectionMatches) {
                    for (const roleRadio of node.querySelectorAll(
                      '[role="radio"][aria-checked="true"]'
                    )) {
                      if (!isVisible(roleRadio)) {
                        continue;
                      }
                      const text = collapse(roleRadio.innerText || roleRadio.textContent || "");
                      if (matchesTarget(text)) {
                        selectionMatches = true;
                        break;
                      }
                    }
                  }
                  if (!selectionMatches) {
                    for (const card of node.querySelectorAll(
                      '[aria-selected="true"], [data-selected="true"], [aria-current="true"]'
                    )) {
                      if (!isVisible(card)) {
                        continue;
                      }
                      const text = collapse(card.innerText || card.textContent || "");
                      if (matchesTarget(text)) {
                        selectionMatches = true;
                        break;
                      }
                    }
                  }

                  const rootText = collapse(node.innerText || node.textContent || "");
                  const targetVisible = matchesTarget(rootText);
                  const statusTexts = [];
                  for (const candidate of document.querySelectorAll(
                    [
                      '[role="status"]',
                      '[role="alert"]',
                      '[aria-live="polite"]',
                      '[aria-live="assertive"]',
                    ].join(', ')
                  )) {
                    if (!isVisible(candidate)) {
                      continue;
                    }
                    const text = collapse(candidate.innerText || candidate.textContent || '');
                    if (text) {
                      statusTexts.push(text);
                    }
                  }
                  const normalizedStatus = normalize(statusTexts.join(' '));
                  const successFeedback =
                    normalizedStatus.includes('resume uploaded successfully')
                    || normalizedStatus.includes('uploaded successfully')
                    || normalizedStatus.includes('successfully uploaded');
                  const uploading =
                    normalizedStatus.includes('uploading')
                    || normalizedStatus.includes('processing upload');
                  return {
                    selection_matches: selectionMatches,
                    target_visible: targetVisible,
                    success_feedback: successFeedback,
                    uploading,
                    status_text: collapse(statusTexts.join(' ')),
                  };
                }
                """,
                {"targetCvName": target_cv_name},
            )
        except Exception:  # noqa: BLE001
            return ResumeUploadSettleState()
        return ResumeUploadSettleState(
            selection_matches=bool(payload.get("selection_matches")),
            target_visible=bool(payload.get("target_visible")),
            success_feedback=bool(payload.get("success_feedback")),
            uploading=bool(payload.get("uploading")),
            status_text=str(payload.get("status_text") or "").strip() or None,
        )

    async def _await_resume_upload_settlement(
        self,
        page: Page,
        *,
        target_cv_name: str,
        timeout_ms: int = 6_500,
        poll_interval_ms: int = 250,
    ) -> ResumeUploadSettleState:
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        last_state = ResumeUploadSettleState()
        while True:
            last_state = await self._inspect_resume_upload_settlement(
                page,
                target_cv_name=target_cv_name,
            )
            if last_state.settled:
                return last_state
            if asyncio.get_running_loop().time() >= deadline:
                return last_state
            await page.wait_for_timeout(poll_interval_ms)

    async def _extract_easy_apply_review_sections(
        self,
        page: Page,
    ) -> dict[str, dict[str, str]]:
        root = await self._easy_apply_root(page)
        try:
            payload = await root.evaluate(
                """
                (node) => {
                  const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
                  const normalize = (value) => collapse(value).toLowerCase();
                  const isVisible = (element) => {
                    if (!(element instanceof Element)) {
                      return false;
                    }
                    const style = window.getComputedStyle(element);
                    if (
                      style.display === "none"
                      || style.visibility === "hidden"
                      || style.opacity === "0"
                    ) {
                      return false;
                    }
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  let refCounter = 1;
                  const ensureRef = (element, attributeName) => {
                    const existing = collapse(element.getAttribute(attributeName));
                    if (existing) {
                      return existing;
                    }
                    const ref = `job-applier-review-${refCounter}`;
                    refCounter += 1;
                    element.setAttribute(attributeName, ref);
                    return ref;
                  };

                  const sections = [];
                  const buttons = Array.from(node.querySelectorAll('button, [role="button"]'));
                  for (const button of buttons) {
                    if (!isVisible(button)) {
                      continue;
                    }
                    const buttonLabel = collapse(
                      button.innerText
                      || button.textContent
                      || button.getAttribute('aria-label')
                      || ''
                    );
                    if (normalize(buttonLabel) !== 'edit') {
                      continue;
                    }
                    let header = button.parentElement;
                    while (header && header !== node) {
                      const texts = Array.from(header.querySelectorAll('p, h1, h2, h3, span'))
                        .map((candidate) =>
                          collapse(candidate.innerText || candidate.textContent || '')
                        )
                        .filter(Boolean)
                        .filter((text) => normalize(text) !== 'edit');
                      if (texts.length) {
                        const title = texts[0];
                        const content = header.nextElementSibling;
                        sections.push({
                          title,
                          body_text: collapse(content?.innerText || content?.textContent || ''),
                          edit_ref: ensureRef(button, 'data-job-applier-review-edit-ref'),
                        });
                        break;
                      }
                      header = header.parentElement;
                    }
                  }
                  return sections;
                }
                """,
            )
        except Exception:  # noqa: BLE001
            return {}

        sections: dict[str, dict[str, str]] = {}
        for item in payload if isinstance(payload, list) else ():
            if not isinstance(item, dict):
                continue
            title = normalize_text(str(item.get("title") or ""))
            if not title:
                continue
            sections[title] = {
                "edit_ref": normalize_text(str(item.get("edit_ref") or "")),
                "body_text": normalize_text(str(item.get("body_text") or "")),
            }
        return sections

    async def _review_resume_matches_requested_cv(
        self,
        page: Page,
        *,
        target_cv_name: str,
    ) -> bool:
        if not target_cv_name:
            return False
        sections = await self._extract_easy_apply_review_sections(page)
        resume_section = sections.get("resume")
        if resume_section is None:
            return False
        return _resume_text_matches_requested_cv(
            resume_section.get("body_text", ""),
            target_cv_name,
        )

    async def _click_easy_apply_review_edit(
        self,
        page: Page,
        *,
        edit_ref: str,
    ) -> bool:
        if not edit_ref:
            return False
        root = await self._easy_apply_root(page)
        button = root.locator(_attribute_selector("data-job-applier-review-edit-ref", edit_ref))
        if not await button.count():
            return False
        try:
            await button.first.scroll_into_view_if_needed(timeout=2_000)
            await button.first.click(timeout=3_000)
            await self._wait_for_easy_apply_surface(page)
        except Exception:  # noqa: BLE001
            return False
        return True

    async def _resume_submit_footer_label(
        self,
        page: Page,
        *,
        step: EasyApplyStep,
    ) -> str | None:
        root = await self._easy_apply_root(page)
        footer_primary = await self._locate_easy_apply_footer_primary_button(root)
        if footer_primary is None:
            return None
        _, footer_label = footer_primary
        footer_intent = self._infer_easy_apply_footer_action_intent(
            footer_label,
            step=step,
        )
        if footer_intent != "submit_application":
            return None
        return footer_label

    async def _maybe_repair_easy_apply_review(
        self,
        page: Page,
        *,
        step: EasyApplyStep,
        settings: UserAgentSettings,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
        submission_cv_path: Path | None,
        resume_review_repair_attempted: bool = False,
        resume_review_verified_selection: bool = False,
    ) -> str | None:
        if "review your application" not in normalize_text(step.surface_text):
            return None

        sections = await self._extract_easy_apply_review_sections(page)
        if not sections:
            return None

        target_cv_name = (
            submission_cv_path.name
            if submission_cv_path is not None
            else settings.profile.cv_filename
        )
        resume_section = sections.get("resume")
        if (
            target_cv_name
            and resume_section is not None
            and not _resume_text_matches_requested_cv(
                resume_section.get("body_text", ""),
                target_cv_name,
            )
        ):
            if resume_review_repair_attempted and resume_review_verified_selection:
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_review_repair",
                        "step_index": step.step_index,
                        "reason": "resume_preview_stale_after_verified_selection",
                        "target_cv_name": target_cv_name,
                        "review_text": resume_section.get("body_text", ""),
                    },
                )
                return "resume_preview_stale_after_verified_selection"
            if await self._click_easy_apply_review_edit(
                page,
                edit_ref=resume_section.get("edit_ref", ""),
            ):
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_review_repair",
                        "step_index": step.step_index,
                        "reason": "resume_mismatch",
                        "target_cv_name": target_cv_name,
                        "review_text": resume_section.get("body_text", ""),
                    },
                )
                return "resume_mismatch"

        contact_section = sections.get("contact info")
        if contact_section is not None and "urn:li:geo:" in normalize_text(
            contact_section.get("body_text", "")
        ):
            if await self._click_easy_apply_review_edit(
                page,
                edit_ref=contact_section.get("edit_ref", ""),
            ):
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_review_repair",
                        "step_index": step.step_index,
                        "reason": "invalid_city_review_value",
                        "review_text": contact_section.get("body_text", ""),
                    },
                )
                return "invalid_city_review_value"
        return None

    async def _upload_resume_from_choice_step(
        self,
        *,
        page: Page,
        root: Locator,
        submission_cv_path: Path,
        target_cv_name: str,
    ) -> bool:
        if not submission_cv_path.exists():
            return False

        upload_trigger = await self._locate_resume_upload_trigger(root)
        if upload_trigger is not None:
            try:
                async with page.expect_file_chooser(timeout=2_500) as chooser_info:
                    await upload_trigger.click()
                chooser = await chooser_info.value
                await chooser.set_files(submission_cv_path)
                settle_state = await self._await_resume_upload_settlement(
                    page,
                    target_cv_name=target_cv_name,
                )
                return settle_state.settled
            except PlaywrightTimeoutError:
                await upload_trigger.click()
                await page.wait_for_timeout(250)

            revealed_file_input = await self._locate_resume_file_input(
                root,
                page,
                include_page_fallback=True,
            )
            if revealed_file_input is not None:
                await revealed_file_input.set_input_files(submission_cv_path)
                settle_state = await self._await_resume_upload_settlement(
                    page,
                    target_cv_name=target_cv_name,
                )
                return settle_state.settled

        direct_file_input = await self._locate_resume_file_input(
            root,
            page,
            include_page_fallback=False,
        )
        if direct_file_input is None:
            direct_file_input = await self._locate_resume_file_input(
                root,
                page,
                include_page_fallback=True,
            )
        if direct_file_input is None:
            return False
        await direct_file_input.set_input_files(submission_cv_path)
        settle_state = await self._await_resume_upload_settlement(
            page,
            target_cv_name=target_cv_name,
        )
        return settle_state.settled

    async def _locate_resume_upload_trigger(self, root: Locator) -> Locator | None:
        candidates = (
            root.get_by_role("button", name=re.compile(r"upload\s+resume", re.I)),
            root.get_by_role("link", name=re.compile(r"upload\s+resume", re.I)),
            root.get_by_text(re.compile(r"upload\s+resume", re.I)),
        )
        for candidate in candidates:
            try:
                if await candidate.count() > 0:
                    return candidate.first
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _locate_resume_file_input(
        self,
        root: Locator,
        page: Page,
        *,
        include_page_fallback: bool = True,
    ) -> Locator | None:
        candidates = [root.locator('input[type="file"]')]
        if include_page_fallback:
            candidates.append(page.locator('input[type="file"]'))
        for candidate in candidates:
            try:
                if await candidate.count() > 0:
                    return candidate.first
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _check_radio_option(
        self,
        root: Locator,
        field: EasyApplyField,
        option: str,
    ) -> bool:
        option_index = _pick_option_index(field.options, preferred=option)
        if option_index is None:
            return False
        return await self._check_radio_option_by_index(root, field, option_index=option_index)

    async def _check_radio_option_by_index(
        self,
        root: Locator,
        field: EasyApplyField,
        *,
        option_index: int,
        force_activate: bool = False,
    ) -> bool:
        option_label = field.options[option_index] if len(field.options) > option_index else None
        option_locator = await self._resolve_radio_option_locator(
            root,
            field,
            option_index=option_index,
        )
        if option_locator is not None:
            try:
                await option_locator.scroll_into_view_if_needed(timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            if not force_activate and await self._radio_option_is_selected(
                root,
                field,
                option_index=option_index,
                option_locator=option_locator,
            ):
                return True
            if await self._activate_radio_option(root, option_locator):
                return True
            if option_label and await self._click_radio_text_target(option_locator, option_label):
                if await self._radio_option_is_selected(root, field, option_index=option_index):
                    return True
            if await self._force_radio_option_via_dom(
                root,
                field,
                option_index=option_index,
                option_label=option_label,
            ):
                if await self._radio_option_is_selected(root, field, option_index=option_index):
                    return True
            if await self._radio_option_is_selected(root, field, option_index=option_index):
                return True
        return False

    async def _resolve_radio_input_locator(
        self,
        root: Locator,
        field: EasyApplyField,
        *,
        option_index: int,
    ) -> Locator | None:
        option_ref = (
            field.option_refs[option_index] if len(field.option_refs) > option_index else None
        )
        if option_ref:
            locator = root.locator(_attribute_selector("data-job-applier-option-ref", option_ref))
            if await locator.count():
                return locator.first

        if field.name:
            group = root.locator(
                f'input[type="radio"]{_attribute_selector("name", field.name)}',
            )
            if await group.count() > option_index:
                return group.nth(option_index)

        group_locator = await self._resolve_radio_group_locator(root, field)
        if group_locator is not None:
            radios = group_locator.locator('input[type="radio"]')
            if await radios.count() > option_index:
                return radios.nth(option_index)

            option_label = field.options[option_index] if len(field.options) > option_index else ""
            normalized_label = option_label.strip()
            if normalized_label:
                try:
                    label_target = group_locator.get_by_text(normalized_label, exact=True)
                    if await label_target.count():
                        input_locator = await self._resolve_radio_input_from_locator(
                            label_target.first,
                        )
                        if input_locator is not None:
                            return input_locator
                except Exception:  # noqa: BLE001
                    pass
        return None

    async def _resolve_radio_group_locator(
        self,
        root: Locator,
        field: EasyApplyField,
    ) -> Locator | None:
        if field.dom_ref:
            direct_locator = root.locator(
                _attribute_selector("data-job-applier-field-ref", field.dom_ref),
            )
            if await direct_locator.count():
                group_locator = direct_locator.first.locator(
                    "xpath=ancestor-or-self::*["
                    "@data-job-applier-radio-group-ref or @role='radiogroup' or self::fieldset"
                    "][1]"
                )
                if await group_locator.count():
                    return group_locator.first

        if field.option_refs:
            for option_ref in field.option_refs:
                locator = root.locator(
                    _attribute_selector("data-job-applier-option-ref", option_ref),
                )
                if not await locator.count():
                    continue
                group_locator = locator.first.locator(
                    "xpath=ancestor-or-self::*["
                    "@data-job-applier-radio-group-ref or @role='radiogroup' or self::fieldset"
                    "][1]"
                )
                if await group_locator.count():
                    return group_locator.first

        if field.name:
            named_group = root.locator(
                f'input[type="radio"]{_attribute_selector("name", field.name)}',
            )
            if await named_group.count():
                group_locator = named_group.first.locator(
                    "xpath=ancestor-or-self::*["
                    "@data-job-applier-radio-group-ref or @role='radiogroup' or self::fieldset"
                    "][1]"
                )
                if await group_locator.count():
                    return group_locator.first

        question_text = field.question_raw.strip()
        if question_text:
            literal = _xpath_literal(question_text)
            question_locators = (
                root.locator(
                    "xpath="
                    f".//*[self::legend or self::label or self::p][normalize-space()={literal}]"
                    "/following-sibling::fieldset[1]"
                ),
                root.locator(
                    "xpath="
                    f".//*[self::legend or self::label or self::p][normalize-space()={literal}]"
                    "/ancestor::*[self::div or self::section][1]"
                    "//*[self::fieldset or @role='radiogroup'][1]"
                ),
            )
            for question_locator in question_locators:
                if await question_locator.count():
                    return question_locator.first
        return None

    async def _resolve_radio_option_locator(
        self,
        root: Locator,
        field: EasyApplyField,
        *,
        option_index: int,
    ) -> Locator | None:
        input_locator = await self._resolve_radio_input_locator(
            root,
            field,
            option_index=option_index,
        )
        if input_locator is None:
            return None

        role_radio = input_locator.locator("xpath=ancestor-or-self::*[@role='radio'][1]")
        if await role_radio.count():
            return role_radio.first

        explicit_label = await self._resolve_radio_explicit_label(root, input_locator)
        if explicit_label is not None:
            return explicit_label

        wrapping_label = input_locator.locator("xpath=ancestor::label[1]")
        if await wrapping_label.count():
            return wrapping_label.first

        return input_locator

    async def _resolve_radio_explicit_label(
        self,
        root: Locator,
        locator: Locator,
    ) -> Locator | None:
        input_id = await _radio_option_input_id(locator)
        if not input_id:
            return None
        label = root.locator(f'label[for="{input_id}"]')
        if not await label.count():
            return None
        return label.first

    async def _resolve_radio_input_from_locator(self, locator: Locator) -> Locator | None:
        try:
            input_locator = locator.locator("xpath=ancestor-or-self::input[@type='radio'][1]")
            if await input_locator.count():
                return input_locator.first
        except Exception:  # noqa: BLE001
            pass

        try:
            nested_input = locator.locator('input[type="radio"]')
            if await nested_input.count():
                return nested_input.first
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _radio_option_is_selected(
        self,
        root: Locator,
        field: EasyApplyField,
        *,
        option_index: int,
        option_locator: Locator | None = None,
    ) -> bool:
        locator = option_locator or await self._resolve_radio_option_locator(
            root,
            field,
            option_index=option_index,
        )
        if locator is not None and await _radio_option_is_checked(locator):
            return True

        refreshed_locator = await self._resolve_radio_option_locator(
            root,
            field,
            option_index=option_index,
        )
        if refreshed_locator is None:
            return False
        return await _radio_option_is_checked(refreshed_locator)

    async def _radio_click_target(self, locator: Locator) -> Locator:
        role_radio = locator.locator("xpath=ancestor-or-self::*[@role='radio'][1]")
        if await role_radio.count():
            return role_radio.first

        wrapping_label = locator.locator("xpath=ancestor::label[1]")
        if await wrapping_label.count():
            return wrapping_label.first

        return locator

    async def _checkbox_click_target(self, locator: Locator) -> Locator:
        role_checkbox = locator.locator("xpath=ancestor-or-self::*[@role='checkbox'][1]")
        if await role_checkbox.count():
            return role_checkbox.first

        wrapping_label = locator.locator("xpath=ancestor::label[1]")
        if await wrapping_label.count():
            return wrapping_label.first

        return locator

    async def _click_radio_text_target(self, locator: Locator, label: str) -> bool:
        normalized_label = label.strip()
        if not normalized_label:
            return False

        try:
            candidate = locator.get_by_text(normalized_label, exact=True)
            if not await candidate.count():
                return False
            text_target = candidate.first
            await text_target.scroll_into_view_if_needed(timeout=2_000)
            await text_target.click(timeout=2_000, force=True)
        except Exception:  # noqa: BLE001
            return False
        return await _radio_option_is_checked(locator)

    async def _force_radio_option_via_dom(
        self,
        root: Locator,
        field: EasyApplyField,
        *,
        option_index: int,
        option_label: str | None,
    ) -> bool:
        option_ref = (
            field.option_refs[option_index] if len(field.option_refs) > option_index else None
        )
        try:
            activated = await root.evaluate(
                """
                (scope, { optionRef, fieldName, optionIndex, optionLabel }) => {
                  if (!(scope instanceof Element)) {
                    return false;
                  }
                  const collapse = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                  const normalizedLabel = collapse(optionLabel);
                  const cssEscape = globalThis.CSS?.escape
                    ? globalThis.CSS.escape.bind(globalThis.CSS)
                    : (value) => String(value).replace(/["\\\\]/g, '\\\\$&');
                  const queryByOptionRef = () => {
                    if (!optionRef) {
                      return null;
                    }
                    return scope.querySelector(
                      `[data-job-applier-option-ref="${cssEscape(optionRef)}"]`
                    );
                  };
                  const queryByFieldName = () => {
                    if (!fieldName) {
                      return null;
                    }
                    const matches = scope.querySelectorAll(
                      `input[type="radio"][name="${cssEscape(fieldName)}"]`
                    );
                    return matches[optionIndex] instanceof HTMLInputElement
                      ? matches[optionIndex]
                      : null;
                  };
                  const queryByLabelText = () => {
                    if (!normalizedLabel) {
                      return null;
                    }
                    const candidates = scope.querySelectorAll(
                      '[role="radio"], label, div, p, span'
                    );
                    for (const candidate of candidates) {
                      if (!(candidate instanceof Element)) {
                        continue;
                      }
                      if (
                        collapse(candidate.innerText || candidate.textContent || '')
                          !== normalizedLabel
                      ) {
                        continue;
                      }
                      const roleRadio = candidate.closest('[role="radio"]');
                      if (roleRadio) {
                        const nestedInput = roleRadio.querySelector('input[type="radio"]');
                        if (nestedInput instanceof HTMLInputElement) {
                          return nestedInput;
                        }
                      }
                      const wrappingLabel = candidate.closest('label');
                      const labeledInputId = (
                        wrappingLabel?.getAttribute('for')
                        || candidate.getAttribute('for')
                      );
                      if (labeledInputId) {
                        const labeledInput = scope.querySelector(`#${cssEscape(labeledInputId)}`);
                        if (labeledInput instanceof HTMLInputElement) {
                          return labeledInput;
                        }
                      }
                    }
                    return null;
                  };
                  const input = queryByOptionRef() || queryByFieldName() || queryByLabelText();
                  if (!(input instanceof HTMLInputElement)) {
                    return false;
                  }
                  const roleRadio = input.closest('[role="radio"]');
                  const explicitLabel = input.id
                    ? scope.querySelector(`label[for="${cssEscape(input.id)}"]`)
                    : null;
                  const textTarget = roleRadio
                    ? Array.from(roleRadio.querySelectorAll('p, span, div')).find((candidate) => {
                        return (
                          collapse(candidate.innerText || candidate.textContent || '')
                          === normalizedLabel
                        );
                      })
                    : null;
                  const fieldset = input.closest('fieldset');
                  const syncPeerState = () => {
                    if (!(fieldset instanceof Element)) {
                      return;
                    }
                    for (const peer of fieldset.querySelectorAll('input[type="radio"]')) {
                      if (!(peer instanceof HTMLInputElement)) {
                        continue;
                      }
                      peer.checked = peer === input;
                      const peerRoleRadio = peer.closest('[role="radio"]');
                      if (peerRoleRadio instanceof Element) {
                        peerRoleRadio.setAttribute(
                          'aria-checked',
                          peer === input ? 'true' : 'false'
                        );
                      }
                    }
                  };
                  const dispatchPointerSequence = (target) => {
                    if (!(target instanceof HTMLElement)) {
                      return;
                    }
                    const init = { bubbles: true, cancelable: true, composed: true, view: window };
                    target.dispatchEvent(new PointerEvent('pointerdown', init));
                    target.dispatchEvent(new MouseEvent('mousedown', init));
                    target.dispatchEvent(new MouseEvent('click', init));
                    target.dispatchEvent(new MouseEvent('mouseup', init));
                    target.dispatchEvent(new PointerEvent('pointerup', init));
                  };
                  for (const target of [textTarget, roleRadio, explicitLabel, input]) {
                    dispatchPointerSequence(target);
                    if (input.checked || roleRadio?.getAttribute('aria-checked') === 'true') {
                      syncPeerState();
                      input.dispatchEvent(new Event('input', { bubbles: true }));
                      input.dispatchEvent(new Event('change', { bubbles: true }));
                      return true;
                    }
                  }
                  const descriptor = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype,
                    'checked'
                  );
                  descriptor?.set?.call(input, true);
                  input.checked = true;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  if (roleRadio instanceof Element) {
                    roleRadio.setAttribute('aria-checked', 'true');
                  }
                  syncPeerState();
                  return input.checked || roleRadio?.getAttribute('aria-checked') === 'true';
                }
                """,
                {
                    "optionRef": option_ref,
                    "fieldName": field.name,
                    "optionIndex": option_index,
                    "optionLabel": option_label,
                },
            )
        except Exception:  # noqa: BLE001
            return False
        return bool(activated)

    async def _activate_radio_option(self, root: Locator, locator: Locator) -> bool:
        input_locator = await self._resolve_radio_input_from_locator(locator)

        if input_locator is not None:
            try:
                await input_locator.scroll_into_view_if_needed(timeout=2_000)
            except Exception:  # noqa: BLE001
                pass

            try:
                await input_locator.check(timeout=2_000, force=True)
            except Exception:  # noqa: BLE001
                pass
            else:
                if await _radio_option_is_checked(input_locator):
                    return True

            try:
                await input_locator.click(timeout=2_000, force=True)
            except Exception:  # noqa: BLE001
                pass
            else:
                if await _radio_option_is_checked(input_locator):
                    return True

            try:
                await input_locator.focus()
                await input_locator.press("Space", timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            else:
                if await _radio_option_is_checked(input_locator):
                    return True

        try:
            await locator.check(timeout=2_000)
        except Exception:  # noqa: BLE001
            pass
        else:
            if await _radio_option_is_checked(locator):
                return True

        try:
            await locator.click(timeout=2_000)
        except Exception:  # noqa: BLE001
            pass
        else:
            if await _radio_option_is_checked(locator):
                return True

        input_id = await _radio_option_input_id(locator)
        if input_id:
            label = await self._resolve_radio_explicit_label(root, locator)
            if label is not None:
                try:
                    await label.click(timeout=2_000)
                except Exception:  # noqa: BLE001
                    pass
                if await _radio_option_is_checked(locator):
                    return True

        wrapping_label = locator.locator("xpath=ancestor::label[1]")
        if await wrapping_label.count():
            try:
                await wrapping_label.first.click(timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            if await _radio_option_is_checked(locator):
                return True

        role_radio = locator.locator("xpath=ancestor-or-self::*[@role='radio'][1]")
        if await role_radio.count():
            try:
                await role_radio.first.click(timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            if await _radio_option_is_checked(locator):
                return True
            try:
                await role_radio.first.focus()
                await role_radio.first.press("Space", timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            if await _radio_option_is_checked(locator):
                return True

        try:
            activated = await locator.evaluate(
                """
                (node) => {
                  if (!(node instanceof Element)) {
                    return false;
                  }
                  const roleRadio = node.matches('[role="radio"]')
                    ? node
                    : node.closest('[role="radio"]') || node.querySelector('[role="radio"]');
                  const input = node instanceof HTMLInputElement
                    ? node
                    : node.querySelector('input[type="radio"]')
                      || roleRadio?.querySelector('input[type="radio"]');
                  const fieldset = node.closest('fieldset')
                    || roleRadio?.closest('fieldset')
                    || input?.closest('fieldset');
                  const explicitLabel = input instanceof HTMLInputElement && input.id
                    ? document.querySelector(`label[for="${input.id}"]`)
                    : null;
                  const wrappingLabel = node.closest?.('label') || input?.closest?.('label');
                  const pointerEvent = (type) =>
                    new MouseEvent(type, {
                      bubbles: true,
                      cancelable: true,
                      composed: true,
                      view: window,
                    });
                  const syncPeerState = () => {
                    if (!(fieldset instanceof Element)) {
                      return;
                    }
                    const peerRadios = fieldset.querySelectorAll('[role="radio"]');
                    for (const peer of peerRadios) {
                      if (peer instanceof Element) {
                        peer.setAttribute('aria-checked', peer === roleRadio ? 'true' : 'false');
                      }
                    }
                    const peerInputs = fieldset.querySelectorAll('input[type="radio"]');
                    for (const peerInput of peerInputs) {
                      if (!(peerInput instanceof HTMLInputElement)) {
                        continue;
                      }
                      peerInput.checked = peerInput === input;
                    }
                  };
                  const clickTargets = [
                    roleRadio,
                    explicitLabel,
                    wrappingLabel,
                    node,
                    input,
                  ].filter((candidate) => candidate instanceof HTMLElement);
                  for (const target of clickTargets) {
                    target.dispatchEvent(pointerEvent('pointerdown'));
                    target.dispatchEvent(pointerEvent('mousedown'));
                    target.click();
                    target.dispatchEvent(pointerEvent('mouseup'));
                    target.dispatchEvent(pointerEvent('pointerup'));
                    target.dispatchEvent(pointerEvent('pointerout'));
                    if (
                      (input instanceof HTMLInputElement && input.checked) ||
                      (
                        roleRadio instanceof Element &&
                        roleRadio.getAttribute('aria-checked') === 'true'
                      )
                    ) {
                      if (roleRadio instanceof Element) {
                        roleRadio.setAttribute('aria-checked', 'true');
                      }
                      syncPeerState();
                      return true;
                    }
                  }
                  if (input instanceof HTMLInputElement) {
                    const descriptor = Object.getOwnPropertyDescriptor(
                      HTMLInputElement.prototype,
                      'checked'
                    );
                    descriptor?.set?.call(input, true);
                    input.checked = true;
                    input.dispatchEvent(pointerEvent('pointerdown'));
                    input.dispatchEvent(pointerEvent('mousedown'));
                    input.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.dispatchEvent(pointerEvent('mouseup'));
                    input.dispatchEvent(pointerEvent('pointerup'));
                    if (roleRadio instanceof Element) {
                      roleRadio.setAttribute('aria-checked', 'true');
                    }
                    syncPeerState();
                  }
                  return (
                    (input instanceof HTMLInputElement && input.checked) ||
                    (
                      roleRadio instanceof Element &&
                      roleRadio.getAttribute('aria-checked') === 'true'
                    )
                  );
                }
                """
            )
        except Exception:  # noqa: BLE001
            return False
        if bool(activated):
            await asyncio.sleep(0.15)
        return await _radio_option_is_checked(locator)

    async def _set_checkbox_state(
        self,
        page: Page,
        root: Locator,
        field: EasyApplyField,
        locator: Locator,
        *,
        desired_checked: bool,
    ) -> bool:
        if await self._await_checkbox_state(
            page=page,
            field=field,
            desired_checked=desired_checked,
            locator=locator,
            root=root,
            attempts=1,
        ):
            return True

        try:
            if desired_checked:
                await locator.check(timeout=2_000)
            else:
                await locator.uncheck(timeout=2_000)
        except Exception:  # noqa: BLE001
            pass
        else:
            if await self._await_checkbox_state(
                page=page,
                field=field,
                desired_checked=desired_checked,
                locator=locator,
            ):
                return True

        try:
            await locator.set_checked(desired_checked, timeout=2_000, force=True)
        except Exception:  # noqa: BLE001
            pass
        else:
            if await self._await_checkbox_state(
                page=page,
                field=field,
                desired_checked=desired_checked,
                locator=locator,
            ):
                return True

        input_id = await locator.get_attribute("id")
        if input_id:
            label = root.locator(f'label[for="{input_id}"]')
            if await label.count():
                try:
                    await label.first.click(timeout=2_000)
                except Exception:  # noqa: BLE001
                    pass
                if await self._await_checkbox_state(
                    page=page,
                    field=field,
                    desired_checked=desired_checked,
                    locator=locator,
                ):
                    return True

        wrapping_label = locator.locator("xpath=ancestor::label[1]")
        if await wrapping_label.count():
            try:
                await wrapping_label.first.click(timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            if await self._await_checkbox_state(
                page=page,
                field=field,
                desired_checked=desired_checked,
                locator=locator,
            ):
                return True

        role_checkbox = locator.locator("xpath=ancestor-or-self::*[@role='checkbox'][1]")
        if await role_checkbox.count():
            try:
                await role_checkbox.first.click(timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            else:
                if await self._await_checkbox_state(
                    page=page,
                    field=field,
                    desired_checked=desired_checked,
                    locator=role_checkbox.first,
                ):
                    return True
            try:
                await role_checkbox.first.click(timeout=2_000, force=True)
            except Exception:  # noqa: BLE001
                pass
            else:
                if await self._await_checkbox_state(
                    page=page,
                    field=field,
                    desired_checked=desired_checked,
                    locator=role_checkbox.first,
                ):
                    return True
            try:
                await role_checkbox.first.focus()
                await role_checkbox.first.press("Space", timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            else:
                if await self._await_checkbox_state(
                    page=page,
                    field=field,
                    desired_checked=desired_checked,
                    locator=role_checkbox.first,
                ):
                    return True
            try:
                box = await role_checkbox.first.bounding_box()
            except Exception:  # noqa: BLE001
                box = None
            if box:
                click_positions = (
                    (
                        box["x"] + min(max(box["width"] * 0.08, 8.0), max(box["width"] - 2.0, 1.0)),
                        box["y"] + (box["height"] / 2.0),
                    ),
                    (
                        box["x"] + (box["width"] / 2.0),
                        box["y"] + (box["height"] / 2.0),
                    ),
                )
                for click_x, click_y in click_positions:
                    try:
                        await page.mouse.click(click_x, click_y)
                    except Exception:  # noqa: BLE001
                        continue
                    if await self._await_checkbox_state(
                        page=page,
                        field=field,
                        desired_checked=desired_checked,
                        locator=role_checkbox.first,
                    ):
                        return True

        try:
            toggled = await locator.evaluate(
                """
                ({ node, desiredChecked }) => {
                  const pointerEvent = (type) =>
                    new MouseEvent(type, {
                      bubbles: true,
                      cancelable: true,
                      composed: true,
                      view: window,
                    });
                  const checkbox = node instanceof HTMLInputElement
                    ? node
                    : node instanceof Element
                      ? node.querySelector('input[type="checkbox"]')
                      : null;
                  const roleCheckbox = node instanceof Element
                    ? (
                      node.getAttribute("role") === "checkbox"
                        ? node
                        : (
                          node.closest('[role="checkbox"]')
                          || node.querySelector('[role="checkbox"]')
                        )
                    )
                    : null;
                  if (!(checkbox instanceof HTMLInputElement)) {
                    if (!(roleCheckbox instanceof Element)) {
                      return false;
                    }
                    const current = (
                      (roleCheckbox.getAttribute("aria-checked") || "").toLowerCase() === "true"
                    );
                    if (current === desiredChecked) {
                      return true;
                    }
                    if (roleCheckbox instanceof HTMLElement) {
                      roleCheckbox.click();
                    }
                    return (
                      roleCheckbox.getAttribute("aria-checked") || ""
                    ).toLowerCase() === "true";
                  }
                  const ariaChecked = (
                    roleCheckbox instanceof Element
                      ? roleCheckbox.getAttribute("aria-checked")
                      : ""
                  ).toLowerCase();
                  if (ariaChecked === "true" || ariaChecked === "false") {
                    const current = ariaChecked === "true";
                    if (current === desiredChecked) {
                      return true;
                    }
                  }
                  if (checkbox.checked === desiredChecked) {
                    return true;
                  }
                  const explicitLabel = checkbox instanceof HTMLInputElement && checkbox.id
                    ? document.querySelector(`label[for="${checkbox.id}"]`)
                    : null;
                  const wrappingLabel = node.closest?.("label") || checkbox?.closest?.("label");
                  const clickTargets = [
                    roleCheckbox,
                    explicitLabel,
                    wrappingLabel,
                    node,
                    checkbox,
                  ].filter((candidate) => candidate instanceof HTMLElement);
                  for (const target of clickTargets) {
                    target.dispatchEvent(pointerEvent("pointerdown"));
                    target.dispatchEvent(pointerEvent("mousedown"));
                    target.click();
                    target.dispatchEvent(pointerEvent("mouseup"));
                    target.dispatchEvent(pointerEvent("pointerup"));
                    target.dispatchEvent(pointerEvent("pointerout"));
                    const ariaAfterClick = (
                      roleCheckbox instanceof Element
                        ? (roleCheckbox.getAttribute("aria-checked") || "").toLowerCase()
                        : ""
                    );
                    if (
                      checkbox.checked === desiredChecked
                      || (
                        (ariaAfterClick === "true" || ariaAfterClick === "false")
                        && (ariaAfterClick === "true") === desiredChecked
                      )
                    ) {
                      if (roleCheckbox instanceof Element) {
                        roleCheckbox.setAttribute(
                          "aria-checked",
                          desiredChecked ? "true" : "false"
                        );
                      }
                      checkbox.checked = desiredChecked;
                      return true;
                    }
                  }
                  const descriptor = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype,
                    "checked"
                  );
                  descriptor?.set?.call(checkbox, desiredChecked);
                  checkbox.checked = desiredChecked;
                  checkbox.dispatchEvent(pointerEvent("pointerdown"));
                  checkbox.dispatchEvent(pointerEvent("mousedown"));
                  checkbox.dispatchEvent(new MouseEvent("click", { bubbles: true }));
                  checkbox.dispatchEvent(new Event("input", { bubbles: true }));
                  checkbox.dispatchEvent(new Event("change", { bubbles: true }));
                  checkbox.dispatchEvent(pointerEvent("mouseup"));
                  checkbox.dispatchEvent(pointerEvent("pointerup"));
                  if (roleCheckbox instanceof Element) {
                    roleCheckbox.setAttribute(
                      "aria-checked",
                      desiredChecked ? "true" : "false"
                    );
                  }
                  return (
                    checkbox.checked === desiredChecked
                    || (
                      roleCheckbox instanceof Element
                      && (roleCheckbox.getAttribute("aria-checked") || "").toLowerCase()
                        === (desiredChecked ? "true" : "false")
                    )
                  );
                }
                """,
                {"desiredChecked": desired_checked},
            )
        except Exception:  # noqa: BLE001
            return False
        if not bool(toggled):
            return False
        return await self._await_checkbox_state(
            page=page,
            field=field,
            desired_checked=desired_checked,
            locator=locator,
        )

    async def _await_checkbox_state(
        self,
        *,
        page: Page,
        field: EasyApplyField,
        desired_checked: bool,
        locator: Locator | None = None,
        root: Locator | None = None,
        attempts: int = 10,
        interval_ms: int = 150,
    ) -> bool:
        remaining_attempts = max(1, attempts)
        current_root = root
        while remaining_attempts > 0:
            if (
                locator is not None
                and await _checkbox_option_is_checked(locator) == desired_checked
            ):
                return True
            refreshed_root = current_root or await self._easy_apply_root(page)
            refreshed_locator = await self._find_control_locator(refreshed_root, field)
            if (
                refreshed_locator is not None
                and await _checkbox_option_is_checked(refreshed_locator) == desired_checked
            ):
                return True
            remaining_attempts -= 1
            if remaining_attempts <= 0:
                return False
            await page.wait_for_timeout(interval_ms)
            current_root = None
        return False

    async def _complete_checkbox_interaction(
        self,
        *,
        page: Page,
        field: EasyApplyField,
        settings: UserAgentSettings,
        desired_checked: bool,
    ) -> bool:
        browser_agent = self._create_browser_agent(settings)
        recent_actions: list[dict[str, object]] = []
        desired_state = "checked" if desired_checked else "unchecked"
        signature_payload = build_field_task_signature(
            task_type=TASK_FINALIZE_CHECKBOX,
            field=field,
            required_state=desired_state,
        )
        stale_memory: ApplyActionMemory | None = None
        initial_root = await self._easy_apply_root(page)
        initial_locator = await self._find_control_locator(initial_root, field)
        initial_click_target = (
            await self._checkbox_click_target(initial_locator)
            if initial_locator is not None
            else None
        )

        memory_entry, _, _ = await self._attempt_replay_apply_memory(
            browser_agent=browser_agent,
            page=page,
            task_type=TASK_FINALIZE_CHECKBOX,
            signature_payload=signature_payload,
            available_values={},
            focus_locator=initial_click_target or initial_root,
            priority_locator=initial_click_target,
        )
        if memory_entry is not None:
            root = await self._easy_apply_root(page)
            locator = await self._find_control_locator(root, field)
            if locator is not None and await self._await_checkbox_state(
                page=page,
                field=field,
                desired_checked=desired_checked,
                locator=locator,
                root=root,
                attempts=2,
            ):
                self._record_apply_memory_success(memory_entry, task_type=TASK_FINALIZE_CHECKBOX)
                return True
            self._record_apply_memory_failure(memory_entry, task_type=TASK_FINALIZE_CHECKBOX)
            stale_memory = memory_entry

        for attempt_index in range(self._agentic_retry_budget(default=4)):
            root = await self._easy_apply_root(page)
            locator = await self._find_control_locator(root, field)
            if locator is None:
                return False
            click_target = await self._checkbox_click_target(locator)
            if await self._await_checkbox_state(
                page=page,
                field=field,
                desired_checked=desired_checked,
                locator=locator,
                root=root,
                attempts=1,
            ):
                return True
            focus_locator = await self._field_interaction_focus_locator(root, field) or click_target
            snapshot = await browser_agent.capture_task_snapshot(
                page=page,
                focus_locator=focus_locator,
                priority_locator=click_target,
            )
            try:
                action = await browser_agent.perform_single_task_action(
                    page=page,
                    available_values={},
                    goal=(
                        "Set the current LinkedIn Easy Apply checkbox to the requested state "
                        "without advancing, dismissing, or submitting the form."
                    ),
                    task_name="linkedin_easy_apply_finalize_checkbox",
                    extra_rules=(
                        f"The checkbox question is {field.question_raw!r}.",
                        f"The requested final state is {desired_state!r}.",
                        "The correct surface may be a visible label, consent row, or clickable "
                        "card instead of the raw hidden input.",
                        "Do not interact with unrelated controls and do not advance the form.",
                    ),
                    allowed_action_types=("click", "press", "scroll", "wait", "done", "fail"),
                    recent_actions=recent_actions,
                    step_index=attempt_index,
                    focus_locator=focus_locator,
                    priority_locator=focus_locator,
                )
            except BrowserAutomationError:
                return False
            recent_actions.append(
                {
                    "step_index": attempt_index,
                    "task_name": "linkedin_easy_apply_finalize_checkbox",
                    "action_type": action.action_type,
                    "action_intent": action.action_intent,
                    "reasoning": action.reasoning,
                }
            )
            await page.wait_for_timeout(150)
            root = await self._easy_apply_root(page)
            locator = await self._find_control_locator(root, field)
            if locator is not None and await self._await_checkbox_state(
                page=page,
                field=field,
                desired_checked=desired_checked,
                locator=locator,
                root=root,
            ):
                self._promote_apply_memory(
                    task_type=TASK_FINALIZE_CHECKBOX,
                    signature_payload=signature_payload,
                    action=action,
                    snapshot=snapshot,
                    existing_memory=stale_memory,
                    replace_existing=stale_memory is not None,
                )
                return True
        return False

    async def _complete_radio_interaction(
        self,
        *,
        page: Page,
        field: EasyApplyField,
        option_index: int,
        option_label: str,
        settings: UserAgentSettings,
    ) -> bool:
        browser_agent = self._create_browser_agent(settings)
        recent_actions: list[dict[str, object]] = []
        signature_payload = build_field_task_signature(
            task_type=TASK_FINALIZE_RADIO,
            field=field,
            required_state=normalize_text(option_label),
        )
        stale_memory: ApplyActionMemory | None = None
        initial_root = await self._easy_apply_root(page)
        initial_option_locator = await self._resolve_radio_option_locator(
            initial_root,
            field,
            option_index=option_index,
        )
        initial_click_target = (
            await self._radio_click_target(initial_option_locator)
            if initial_option_locator is not None
            else None
        )

        memory_entry, _, _ = await self._attempt_replay_apply_memory(
            browser_agent=browser_agent,
            page=page,
            task_type=TASK_FINALIZE_RADIO,
            signature_payload=signature_payload,
            available_values={},
            focus_locator=initial_click_target or initial_root,
            priority_locator=initial_click_target,
        )
        if memory_entry is not None:
            root = await self._easy_apply_root(page)
            option_locator = await self._resolve_radio_option_locator(
                root,
                field,
                option_index=option_index,
            )
            if option_locator is not None and await self._radio_option_is_selected(
                root,
                field,
                option_index=option_index,
                option_locator=option_locator,
            ):
                self._record_apply_memory_success(memory_entry, task_type=TASK_FINALIZE_RADIO)
                return True
            self._record_apply_memory_failure(memory_entry, task_type=TASK_FINALIZE_RADIO)
            stale_memory = memory_entry

        for attempt_index in range(self._agentic_retry_budget(default=4)):
            root = await self._easy_apply_root(page)
            option_locator = await self._resolve_radio_option_locator(
                root,
                field,
                option_index=option_index,
            )
            if option_locator is None:
                return False
            click_target = await self._radio_click_target(option_locator)
            if await self._radio_option_is_selected(
                root,
                field,
                option_index=option_index,
                option_locator=option_locator,
            ):
                return True
            focus_locator = await self._field_interaction_focus_locator(root, field) or click_target
            snapshot = await browser_agent.capture_task_snapshot(
                page=page,
                focus_locator=focus_locator,
                priority_locator=focus_locator,
            )
            try:
                action = await browser_agent.perform_single_task_action(
                    page=page,
                    available_values={},
                    goal=(
                        "Select the requested LinkedIn Easy Apply radio option without advancing, "
                        "dismissing, or submitting the form."
                    ),
                    task_name="linkedin_easy_apply_finalize_radio",
                    extra_rules=(
                        f"The radio question is {field.question_raw!r}.",
                        f"The option that must end up selected is {option_label!r}.",
                        "The visible clickable surface may be the option row or label instead of "
                        "the raw radio input element.",
                        "Do not interact with unrelated controls and do not advance the form.",
                    ),
                    allowed_action_types=("click", "press", "scroll", "wait", "done", "fail"),
                    recent_actions=recent_actions,
                    step_index=attempt_index,
                    focus_locator=focus_locator,
                    priority_locator=focus_locator,
                )
            except BrowserAutomationError:
                return False
            recent_actions.append(
                {
                    "step_index": attempt_index,
                    "task_name": "linkedin_easy_apply_finalize_radio",
                    "action_type": action.action_type,
                    "action_intent": action.action_intent,
                    "reasoning": action.reasoning,
                }
            )
            await page.wait_for_timeout(150)
            root = await self._easy_apply_root(page)
            option_locator = await self._resolve_radio_option_locator(
                root,
                field,
                option_index=option_index,
            )
            if option_locator is None:
                return False
            if await self._radio_option_is_selected(
                root,
                field,
                option_index=option_index,
                option_locator=option_locator,
            ):
                self._promote_apply_memory(
                    task_type=TASK_FINALIZE_RADIO,
                    signature_payload=signature_payload,
                    action=action,
                    snapshot=snapshot,
                    existing_memory=stale_memory,
                    replace_existing=stale_memory is not None,
                )
                return True
            if option_label and await self._click_radio_text_target(option_locator, option_label):
                if await self._radio_option_is_selected(root, field, option_index=option_index):
                    synthetic_action = self._build_priority_target_click_action(
                        snapshot=snapshot,
                        action_intent="select_option",
                    )
                    if synthetic_action is not None:
                        self._promote_apply_memory(
                            task_type=TASK_FINALIZE_RADIO,
                            signature_payload=signature_payload,
                            action=synthetic_action,
                            snapshot=snapshot,
                            existing_memory=stale_memory,
                            replace_existing=stale_memory is not None,
                        )
                    return True
            if await self._activate_radio_option(root, option_locator):
                synthetic_action = self._build_priority_target_click_action(
                    snapshot=snapshot,
                    action_intent="select_option",
                )
                if synthetic_action is not None:
                    self._promote_apply_memory(
                        task_type=TASK_FINALIZE_RADIO,
                        signature_payload=signature_payload,
                        action=synthetic_action,
                        snapshot=snapshot,
                        existing_memory=stale_memory,
                        replace_existing=stale_memory is not None,
                    )
                return True
            if await self._radio_option_is_selected(root, field, option_index=option_index):
                synthetic_action = self._build_priority_target_click_action(
                    snapshot=snapshot,
                    action_intent="select_option",
                )
                if synthetic_action is not None:
                    self._promote_apply_memory(
                        task_type=TASK_FINALIZE_RADIO,
                        signature_payload=signature_payload,
                        action=synthetic_action,
                        snapshot=snapshot,
                        existing_memory=stale_memory,
                        replace_existing=stale_memory is not None,
                    )
                return True
        root = await self._easy_apply_root(page)
        option_locator = await self._resolve_radio_option_locator(
            root,
            field,
            option_index=option_index,
        )
        if option_locator is None:
            return False
        return await self._radio_option_is_selected(
            root,
            field,
            option_index=option_index,
            option_locator=option_locator,
        )

    async def _find_control_locator(self, root: Locator, field: EasyApplyField) -> Locator | None:
        if field.dom_ref:
            locator = root.locator(_attribute_selector("data-job-applier-field-ref", field.dom_ref))
            matched = await self._match_control_locator_candidates(locator, field)
            if matched is not None:
                return matched
        if field.dom_id:
            locator = root.locator(_attribute_selector("id", field.dom_id))
            matched = await self._match_control_locator_candidates(locator, field)
            if matched is not None:
                return matched
        if field.name:
            locator = root.locator(_attribute_selector("name", field.name))
            matched = await self._match_control_locator_candidates(locator, field)
            if matched is not None:
                return matched
        return None

    async def _match_control_locator_candidates(
        self,
        candidates: Locator,
        field: EasyApplyField,
    ) -> Locator | None:
        try:
            candidate_count = await candidates.count()
        except Exception:  # noqa: BLE001
            return None
        if candidate_count <= 0:
            return None
        for index in range(candidate_count):
            candidate = candidates.nth(index)
            matched = await self._match_control_locator(candidate, field)
            if matched is not None:
                return matched
        return None

    async def _match_control_locator(
        self,
        candidate: Locator,
        field: EasyApplyField,
    ) -> Locator | None:
        if await self._locator_matches_control_kind(candidate, field):
            return candidate
        selector = self._control_selector_for_field(field)
        if selector is None:
            return candidate
        try:
            descendants = candidate.locator(selector)
            if await descendants.count():
                return descendants.first
        except Exception:  # noqa: BLE001
            return None
        return None

    def _control_selector_for_field(self, field: EasyApplyField) -> str | None:
        if field.control_kind == "text":
            return (
                'input:not([type="radio"]):not([type="checkbox"]):not([type="hidden"]):'
                'not([type="file"]), textarea'
            )
        if field.control_kind == "textarea":
            return "textarea"
        if field.control_kind == "select":
            return "select"
        if field.control_kind == "radio":
            return '[role="radio"], input[type="radio"]'
        if field.control_kind == "checkbox":
            return '[role="checkbox"], input[type="checkbox"]'
        return 'input[type="file"]'

    async def _locator_matches_control_kind(
        self,
        locator: Locator,
        field: EasyApplyField,
    ) -> bool:
        try:
            payload = await locator.evaluate(
                """
                (node) => ({
                  tag: node instanceof Element ? node.tagName.toLowerCase() : "",
                  type: node instanceof HTMLInputElement ? node.type.toLowerCase() : "",
                  role: node instanceof Element
                    ? (node.getAttribute("role") || "").toLowerCase()
                    : "",
                })
                """
            )
        except Exception:  # noqa: BLE001
            return False
        tag = str((payload or {}).get("tag") or "").lower()
        input_type = str((payload or {}).get("type") or "").lower()
        role = str((payload or {}).get("role") or "").lower()
        if field.control_kind == "text":
            if tag == "textarea":
                return True
            if tag != "input":
                return False
            return input_type not in {"radio", "checkbox", "hidden", "file"}
        if field.control_kind == "textarea":
            return tag == "textarea"
        if field.control_kind == "select":
            return tag == "select"
        if field.control_kind == "radio":
            return role == "radio" or (tag == "input" and input_type == "radio")
        if field.control_kind == "checkbox":
            return role == "checkbox" or (tag == "input" and input_type == "checkbox")
        return tag == "input" and input_type == "file"

    async def _field_interaction_focus_locator(
        self,
        root: Locator,
        field: EasyApplyField,
    ) -> Locator | None:
        locator = await self._find_control_locator(root, field)
        if locator is None:
            return None
        try:
            scope_token = await locator.evaluate(
                """
                (node) => {
                  if (!node || node.nodeType !== 1) {
                    return null;
                  }
                  const scopeAttr = "data-job-applier-field-scope";
                  document
                    .querySelectorAll(`[${scopeAttr}]`)
                    .forEach((candidate) => candidate.removeAttribute(scopeAttr));

                  const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
                  const currentFieldRef = collapse(
                    node.getAttribute("data-job-applier-field-ref"),
                  );
                  const relatedFieldRefs = (candidate) => {
                    if (!candidate || candidate.nodeType !== 1) {
                      return [];
                    }
                    const refs = [];
                    const pushRef = (value) => {
                      const normalized = collapse(value);
                      if (normalized) {
                        refs.push(normalized);
                      }
                    };
                    pushRef(candidate.getAttribute("data-job-applier-field-ref"));
                    for (const descendant of candidate.querySelectorAll(
                      "[data-job-applier-field-ref]"
                    )) {
                      pushRef(descendant.getAttribute("data-job-applier-field-ref"));
                    }
                    return Array.from(new Set(refs));
                  };
                  const containsUnsafeGlobalControls = (candidate) => {
                    if (!candidate || candidate.nodeType !== 1) {
                      return false;
                    }
                    const interactiveDescendants = candidate.querySelectorAll(
                      "button, a[href], [role='button'], [role='link']"
                    );
                    for (const descendant of interactiveDescendants) {
                      if (
                        descendant === node
                        || node.contains(descendant)
                        || descendant.contains(node)
                      ) {
                        continue;
                      }
                      return true;
                    }
                    return false;
                  };

                  let best = node;
                  let current = node.parentElement;
                  while (current && current !== document.body) {
                    const refs = relatedFieldRefs(current);
                    if (
                      currentFieldRef
                      && refs.length > 0
                      && (refs.length > 1 || refs[0] !== currentFieldRef)
                    ) {
                      break;
                    }
                    if (containsUnsafeGlobalControls(current)) {
                      break;
                    }
                    best = current;
                    current = current.parentElement;
                  }

                  const token = currentFieldRef || "active-field-scope";
                  best.setAttribute(scopeAttr, token);
                  return token;
                }
                """
            )
        except Exception:  # noqa: BLE001
            scope_token = None
        if isinstance(scope_token, str) and scope_token.strip():
            container = root.locator(
                _attribute_selector("data-job-applier-field-scope", scope_token.strip())
            )
            if await container.count():
                return container.first
        container = locator.locator(
            "xpath=ancestor-or-self::*[@role='group' or self::fieldset or self::section][1]"
        )
        if await container.count():
            return container.first
        wrapper = locator.locator("xpath=ancestor-or-self::div[1]")
        if await wrapper.count():
            return wrapper.first
        container = locator.locator(
            "xpath=ancestor-or-self::*[@role='listbox' or @role='dialog' or self::form][1]"
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
              const truncate = (value, limit) => {
                const collapsed = collapse(value);
                if (!collapsed || collapsed.length <= limit) {
                  return collapsed;
                }
                return `${collapsed.slice(0, Math.max(0, limit - 3)).trim()}...`;
              };
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

              const isBinaryOnlyText = (value) => {
                const normalized = collapse(value).toLowerCase();
                if (!normalized) {
                  return false;
                }
                const tokens = normalized.split(/\\s+/).filter(Boolean);
                if (!tokens.length || tokens.length > 6) {
                  return false;
                }
                const binaryTokens = new Set([
                  "yes",
                  "no",
                  "sim",
                  "nao",
                  "não",
                  "si",
                  "oui",
                  "non",
                  "ja",
                  "nein",
                ]);
                return tokens.every((token) => binaryTokens.has(token));
              };

              const promptTextFromSibling = (element) => {
                const candidateScopes = [];
                const fieldset = element.closest("fieldset");
                if (fieldset) {
                  candidateScopes.push(fieldset);
                  if (fieldset.parentElement) {
                    candidateScopes.push(fieldset.parentElement);
                  }
                }
                const semanticContainer = element.closest(
                  [
                    ".jobs-easy-apply-form-section__grouping",
                    ".fb-form-element",
                    ".jobs-easy-apply-form-element",
                    "[role='group']",
                    "section",
                  ].join(", ")
                );
                if (semanticContainer) {
                  candidateScopes.push(semanticContainer);
                }

                const seen = new Set();
                for (const scope of candidateScopes) {
                  if (!(scope instanceof HTMLElement) || seen.has(scope)) {
                    continue;
                  }
                  seen.add(scope);

                  let sibling = scope.previousElementSibling;
                  while (sibling) {
                    const text = collapse(sibling.innerText || sibling.textContent || "");
                    const controlCount =
                      sibling instanceof HTMLElement
                        ? sibling.querySelectorAll("input, select, textarea").length
                        : 0;
                    if (
                      text
                      && !isBinaryOnlyText(text)
                      && text.length <= 360
                      && controlCount === 0
                    ) {
                      return truncate(text, 240);
                    }
                    sibling = sibling.previousElementSibling;
                  }

                  const parent = scope.parentElement;
                  if (!parent) {
                    continue;
                  }
                  const siblings = Array.from(parent.children);
                  const scopeIndex = siblings.indexOf(scope);
                  for (let index = scopeIndex - 1; index >= 0; index -= 1) {
                    const candidate = siblings[index];
                    const text = collapse(candidate.innerText || candidate.textContent || "");
                    const controlCount =
                      candidate instanceof HTMLElement
                        ? candidate.querySelectorAll("input, select, textarea").length
                        : 0;
                    if (
                      text
                      && !isBinaryOnlyText(text)
                      && text.length <= 360
                      && controlCount === 0
                    ) {
                      return truncate(text, 240);
                    }
                  }
                }
                return "";
              };

              const descriptiveScopeText = (scope, element) => {
                const promptText = promptTextFromSibling(element);
                const scopeText = collapse(scope?.innerText || scope?.textContent || "");
                if (promptText && scopeText) {
                  return truncate(`${promptText} ${scopeText}`, 900);
                }
                if (scopeText && !isBinaryOnlyText(scopeText)) {
                  return truncate(scopeText, 900);
                }
                return truncate(promptText, 900);
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
                  const siblingPrompt = promptTextFromSibling(fieldset);
                  if (siblingPrompt) {
                    return siblingPrompt;
                  }
                }

                const container = element.closest([
                  ".fb-form-element",
                  ".jobs-easy-apply-form-section__grouping",
                  ".jobs-easy-apply-form-element",
                  "[role='group']",
                  "fieldset",
                ].join(", "));
                if (container) {
                  const textLabel = container.querySelector(
                    "label, legend, .fb-form-element-label, [data-test-form-element-label]",
                  );
                  if (textLabel && collapse(textLabel.innerText)) {
                    return collapse(textLabel.innerText);
                  }
                  const siblingPrompt = promptTextFromSibling(container);
                  if (siblingPrompt) {
                    return siblingPrompt;
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
                let candidate = element.parentElement;
                while (candidate && candidate !== node) {
                  const text = collapse(candidate.innerText);
                  const radioCount = candidate.querySelectorAll("input[type=radio]").length;
                  const controlCount = candidate.querySelectorAll(
                    "input, select, textarea",
                  ).length;
                  if (text && text.length <= 360 && radioCount <= 1 && controlCount <= 2) {
                    return truncate(text, 240);
                  }
                  candidate = candidate.parentElement;
                }
                return collapse(element.getAttribute("value") || element.textContent || "");
              };

              const radioGroupKeyFor = (element) => {
                const name = collapse(element.getAttribute("name"));
                if (name) {
                  return `name:${name}`;
                }
                const fieldset = element.closest("fieldset");
                if (fieldset) {
                  return ensureRef(fieldset, "data-job-applier-radio-group-ref");
                }
                const groupScope = scopeFor(element);
                if (groupScope) {
                  return ensureRef(groupScope, "data-job-applier-radio-group-ref");
                }
                const id = collapse(element.getAttribute("id"));
                if (id) {
                  return `id:${id}`;
                }
                return ensureRef(element, "data-job-applier-radio-group-ref");
              };

              const radioQuestionFor = (element) => {
                const promptText = promptTextFromSibling(element);
                if (promptText) {
                  return promptText;
                }
                const explicit = questionFor(element);
                if (explicit && explicit.toLowerCase() !== "radio" && !isBinaryOnlyText(explicit)) {
                  return explicit;
                }
                const groupScope = scopeFor(element);
                if (!groupScope) {
                  return explicit;
                }
                const lines = (descriptiveScopeText(groupScope, element) || "")
                  .split(/\\n+/)
                  .map((line) => collapse(line))
                  .filter(Boolean);
                if (lines.length >= 2) {
                  return truncate(lines.slice(0, 2).join(" "), 220);
                }
                if (lines.length === 1) {
                  return truncate(lines[0], 220);
                }
                return explicit;
              };

              const scopeFor = (element) => {
                const semanticScope = element.closest(
                  [
                    ".jobs-easy-apply-form-section__grouping",
                    ".fb-form-element",
                    ".jobs-easy-apply-form-element",
                    "[role='group']",
                    "fieldset",
                    "section",
                  ].join(", ")
                );
                if (semanticScope) {
                  return semanticScope;
                }
                let candidate = element.parentElement;
                while (candidate && candidate !== node) {
                  const text = collapse(candidate.innerText);
                  const controlCount = candidate.querySelectorAll(
                    "input, select, textarea",
                  ).length;
                  if (text && text.length <= 1200 && controlCount >= 1 && controlCount <= 6) {
                    return candidate;
                  }
                  candidate = candidate.parentElement;
                }
                return element.parentElement;
              };

              const referencedTextFor = (element) => {
                const ids = ["aria-describedby", "aria-errormessage"]
                  .flatMap((attributeName) =>
                    collapse(element.getAttribute(attributeName))
                      .split(/\\s+/)
                      .map((item) => item.trim())
                      .filter(Boolean)
                  );
                const parts = [];
                const seen = new Set();
                for (const id of ids) {
                  const referenced = document.getElementById(id);
                  const text = collapse(referenced?.innerText || referenced?.textContent || "");
                  if (!text || seen.has(text)) {
                    continue;
                  }
                  seen.add(text);
                  parts.push(text);
                }
                return truncate(parts.join(" "), 280);
              };

              const fieldContextFor = (element) => {
                const scope = scopeFor(element);
                const descriptiveScope = descriptiveScopeText(scope, element);
                if (descriptiveScope) {
                  return descriptiveScope;
                }
                if (scope && collapse(scope.innerText)) {
                  return truncate(scope.innerText, 900);
                }
                return truncate(questionFor(element), 900);
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
                  field_context: fieldContextFor(element),
                  helper_text: referencedTextFor(element),
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
              const seenRadioGroups = new Set();
              for (const input of radioInputs) {
                const groupKey = radioGroupKeyFor(input);
                if (!groupKey || seenRadioGroups.has(groupKey)) {
                  continue;
                }
                seenRadioGroups.add(groupKey);
                const group = radioInputs.filter(
                  (candidate) => radioGroupKeyFor(candidate) === groupKey,
                );
                const selected = group.find((candidate) => candidate.checked);
                const optionRefs = group.map((candidate) =>
                  ensureRef(candidate, "data-job-applier-option-ref"),
                );
                const groupScope = scopeFor(input);
                const optionLabels = group
                  .map((candidate) => optionLabel(candidate))
                  .filter(Boolean);
                fields.push({
                  dom_ref: ensureRef(input, "data-job-applier-field-ref"),
                  dom_id: input.getAttribute("id"),
                  name: input.getAttribute("name"),
                  input_type: "radio",
                  control_kind: "radio",
                  question_raw: radioQuestionFor(input),
                  required:
                    group.some(
                      (candidate) =>
                        candidate.required || candidate.getAttribute("aria-required") === "true",
                    )
                    || /\\*/.test(radioQuestionFor(input)),
                  prefilled: Boolean(selected),
                  current_value: selected ? optionLabel(selected) : "",
                  field_context: fieldContextFor(input),
                  helper_text: referencedTextFor(input),
                  options: optionLabels,
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
                  field_context: fieldContextFor(input),
                  helper_text: referencedTextFor(input),
                  options: ["Yes", "No"],
                });
              }

              const text = collapse(node.innerText);
              const match =
                text.match(/step\\s*(\\d+)\\s*of\\s*(\\d+)/i)
                || text.match(/(?:^|\\b)(\\d+)\\s*\\/\\s*(\\d+)(?:\\b|\\s+pages?\\b)/i);
              return {
                current_step: match ? Number(match[1]) : null,
                total_steps: match ? Number(match[2]) : null,
                fields,
                surface_text: truncate(text, 2200),
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
        return EasyApplyStep(
            step_index=step_index,
            total_steps=total,
            fields=fields,
            surface_text=str(payload.get("surface_text") or ""),
        )

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
            for step_index in range(self._agentic_retry_budget(default=6)):
                if await self._easy_apply_modal_visible(page):
                    await self._wait_for_easy_apply_surface(page)
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
                    await self._wait_for_easy_apply_surface(page)
                    return
                if action.action_type == "done":
                    break
        except BrowserAutomationError as exc:
            raise LinkedInEasyApplyError(str(exc)) from exc

        msg = (
            "Browser agent could not open the LinkedIn Easy Apply modal from the current job page."
        )
        raise LinkedInEasyApplyError(msg)

    async def _detect_existing_application_on_job_page(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
    ) -> str | None:
        local_detection = await self._detect_existing_application_on_job_page_locally(page)
        if local_detection is not None:
            return local_detection
        assessment = await self._assess_job_page_apply_blocker_with_agent(
            page,
            settings=settings,
        )
        if assessment is None or assessment.status != "complete":
            return None
        return assessment.summary or "LinkedIn indicates this job was already applied to."

    async def _detect_existing_application_on_job_page_locally(
        self,
        page: Page,
    ) -> str | None:
        try:
            payload = await page.evaluate(
                """
                () => {
                  const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
                  const normalize = (value) => collapse(value).toLowerCase();
                  const isVisible = (element) => {
                    if (!(element instanceof Element)) {
                      return false;
                    }
                    const style = window.getComputedStyle(element);
                    if (
                      style.display === "none"
                      || style.visibility === "hidden"
                      || style.opacity === "0"
                    ) {
                      return false;
                    }
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };

                  const visibleTexts = Array.from(
                    document.querySelectorAll("h1, h2, h3, p, span, div, button, a")
                  )
                    .filter(isVisible)
                    .map((element) =>
                      collapse(
                        element.innerText
                        || element.textContent
                        || element.getAttribute("aria-label")
                        || ""
                      )
                    )
                    .filter(Boolean);

                  return {
                    page_text: normalize(visibleTexts.join(" ")),
                    sample_texts: visibleTexts.slice(0, 40),
                  };
                }
                """,
            )
        except Exception:  # noqa: BLE001
            return None

        if not isinstance(payload, dict):
            return None
        page_text = normalize_text(str(payload.get("page_text") or ""))
        if not page_text:
            return None

        success_markers = (
            "application submitted now",
            "application submitted",
            "application sent",
            "your application was sent",
            "application status",
            "already applied",
            "you applied",
            "candidatura enviada",
            "inscricao enviada",
            "inscrição enviada",
            "aplicacao enviada",
            "aplicação enviada",
            "status da candidatura",
        )
        if any(marker in page_text for marker in success_markers):
            return "LinkedIn indicates this job was already applied to."
        return None

    async def _assess_job_page_apply_blocker_with_agent(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
    ) -> BrowserTaskAssessment | None:
        try:
            assessment = await self._assess_browser_state_with_agent(
                page,
                settings=settings,
                task_name="linkedin_job_page_apply_availability",
                goal=(
                    "Determine whether the current LinkedIn job page can start a new LinkedIn "
                    "Easy Apply application right now. Do not click application controls during "
                    "this assessment."
                ),
                extra_rules=(
                    (
                        "Use complete only when the visible page explicitly indicates that this "
                        "account already applied to the job, such as application submitted, "
                        "already applied, or a dedicated application status tied to this job."
                    ),
                    (
                        "Use blocked when the page clearly prevents starting a new application "
                        "from this page, such as external apply only, no longer accepting "
                        "applications, job unavailable, or another visible blocker."
                    ),
                    (
                        "Use pending only when a visible LinkedIn Easy Apply control is ready "
                        "or the Easy Apply modal is already open."
                    ),
                    "Use unknown when the evidence is mixed or insufficient.",
                    (
                        "When the page shows an application status section with submitted or "
                        "already-applied language, prefer complete."
                    ),
                ),
            )
        except LinkedInEasyApplyError:
            return None
        if assessment.status in {"complete", "blocked"}:
            return assessment
        return None

    async def _assess_job_apply_entrypoint(self, page: Page) -> JobApplyEntrypointAssessment:
        if await self._easy_apply_modal_visible(page):
            return JobApplyEntrypointAssessment(
                easy_apply_available=True,
                external_apply_only=False,
                notes="The LinkedIn Easy Apply modal is already visible on the page.",
            )

        scan_payload = await page.evaluate(
            """
            () => {
              const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const isVisible = (element) => {
                if (!element) {
                  return false;
                }
                const style = window.getComputedStyle(element);
                if (
                  style.display === "none"
                  || style.visibility === "hidden"
                  || style.opacity === "0"
                ) {
                  return false;
                }
                const rect = element.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) {
                  return false;
                  }
                  return rect.bottom >= 0 && rect.top <= window.innerHeight;
              };
              const collectTexts = (selectors) =>
                selectors
                  .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
                  .filter(isVisible)
                  .map((element) => collapse(
                    element.innerText
                    || element.textContent
                    || element.getAttribute("aria-label")
                    || "",
                  ))
                  .filter(Boolean);

              const candidates = Array.from(
                document.querySelectorAll("button, a[href], [role='button']"),
              );
              const controls = candidates
                .filter(isVisible)
                .map((element) => {
                  const rect = element.getBoundingClientRect();
                  const text = collapse(element.innerText || element.textContent || "");
                  const ariaLabel = collapse(element.getAttribute("aria-label") || "");
                  const title = collapse(element.getAttribute("title") || "");
                  const href =
                    element instanceof HTMLAnchorElement
                      ? element.href || element.getAttribute("href") || ""
                      : element.getAttribute("href") || "";
                  return {
                    tag: element.tagName.toLowerCase(),
                    text,
                    aria_label: ariaLabel,
                    title,
                    href,
                    target: element.getAttribute("target") || "",
                    testid:
                      element.getAttribute("data-testid")
                      || element.getAttribute("data-test-id")
                      || "",
                    top: Math.round(rect.top),
                  };
                })
                .sort((left, right) => left.top - right.top)
                .slice(0, 80);
              const statusTexts = collectTexts([
                ".jobs-details-top-card__apply-error",
                ".jobs-unified-top-card__job-insight",
                ".jobs-unified-top-card__job-insight-view-model-secondary",
                ".artdeco-inline-feedback__message",
                "[data-test-job-details-apply-message]",
                "[data-test-job-unavailable-message]",
              ]).slice(0, 24);
              return { controls, status_texts: statusTexts };
            }
            """,
        )
        controls_payload = scan_payload.get("controls") if isinstance(scan_payload, dict) else None
        controls = controls_payload if isinstance(controls_payload, list) else []
        status_texts_payload = (
            scan_payload.get("status_texts") if isinstance(scan_payload, dict) else None
        )
        status_texts = tuple(
            dict.fromkeys(
                normalize_text(str(raw_text or ""))
                for raw_text in (
                    status_texts_payload if isinstance(status_texts_payload, list) else ()
                )
                if str(raw_text or "").strip()
            )
        )
        relevant_labels: list[str] = []
        easy_apply_labels: list[str] = []
        external_apply_labels: list[str] = []
        unavailable_status_labels: list[str] = []

        for status_text in status_texts:
            if any(
                marker in status_text
                for marker in (
                    "no longer accepting applications",
                    "job is no longer available",
                    "position has been filled",
                    "this job is closed",
                    "this position is no longer available",
                )
            ):
                unavailable_status_labels.append(status_text)

        for raw_control in controls:
            if not isinstance(raw_control, dict):
                continue
            text = str(raw_control.get("text") or "")
            aria_label = str(raw_control.get("aria_label") or "")
            title = str(raw_control.get("title") or "")
            href = str(raw_control.get("href") or "")
            target = str(raw_control.get("target") or "")
            combined_label = normalize_text(
                " ".join(part for part in (text, aria_label, title) if part)
            )
            if not combined_label and not href:
                continue
            if combined_label:
                relevant_labels.append(combined_label)

            has_easy_apply_signal = "easy apply" in combined_label
            if has_easy_apply_signal:
                easy_apply_labels.append(combined_label)
                continue

            has_external_apply_signal = "apply on company website" in combined_label or (
                "apply" in combined_label
                and "linkedin.com/safety/go" in href.lower()
                and target.lower() == "_blank"
            )
            if has_external_apply_signal:
                external_apply_labels.append(combined_label or href)

        deduped_labels = tuple(dict.fromkeys(label for label in relevant_labels if label))

        if easy_apply_labels:
            return JobApplyEntrypointAssessment(
                easy_apply_available=True,
                external_apply_only=False,
                labels=tuple(dict.fromkeys(easy_apply_labels))[:8],
                notes="A visible LinkedIn Easy Apply control is available on the job page.",
            )

        if external_apply_labels:
            return JobApplyEntrypointAssessment(
                easy_apply_available=False,
                external_apply_only=True,
                labels=tuple(dict.fromkeys(external_apply_labels))[:8],
                notes=(
                    "The only visible apply button leads to the company website and no control "
                    "is available on this page to open the LinkedIn Easy Apply modal."
                ),
            )

        if unavailable_status_labels:
            return JobApplyEntrypointAssessment(
                easy_apply_available=False,
                external_apply_only=False,
                terminally_unavailable=True,
                labels=tuple(dict.fromkeys(unavailable_status_labels))[:8],
                notes=(
                    "The current LinkedIn job page is no longer accepting applications and "
                    "does not allow a new Easy Apply flow to be started."
                ),
            )

        return JobApplyEntrypointAssessment(
            easy_apply_available=False,
            external_apply_only=False,
            labels=deduped_labels[:8],
            notes=(
                "No visible Easy Apply entrypoint was confirmed during the deterministic page scan."
            ),
        )

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
        signature_payload = build_step_task_signature(
            task_type=TASK_PRIMARY_ACTION,
            step_index=step.step_index,
            total_steps=step.total_steps,
            surface_text=step.surface_text,
            fields=step.fields,
        )
        stale_memory: ApplyActionMemory | None = None
        try:
            replay_root = await self._easy_apply_root(page)
            replay_footer_primary = await self._locate_easy_apply_footer_primary_button(replay_root)
            memory_entry, memory_action, _ = await self._attempt_replay_apply_memory(
                browser_agent=browser_agent,
                page=page,
                task_type=TASK_PRIMARY_ACTION,
                signature_payload=signature_payload,
                available_values={},
                focus_locator=replay_root,
                priority_locator=(
                    replay_footer_primary[0] if replay_footer_primary is not None else None
                ),
            )
            if memory_entry is not None and memory_action is not None:
                memory_succeeded = False
                if memory_action.action_intent == "advance_step":
                    await self._wait_for_easy_apply_surface(page)
                    current_step = await self._extract_step(
                        page,
                        last_known_step_index=step.step_index,
                        last_known_total_steps=step.total_steps,
                    )
                    memory_succeeded = (
                        current_step.step_index != step.step_index
                        or _step_surface_changed(step, current_step)
                    )
                elif self._action_indicates_submission(memory_action):
                    memory_succeeded = await self._submission_confirmation_visible(page)
                    if not memory_succeeded:
                        memory_succeeded = not await self._easy_apply_modal_visible(page)

                if memory_succeeded:
                    self._record_apply_memory_success(memory_entry, task_type=TASK_PRIMARY_ACTION)
                    return memory_action

                self._record_apply_memory_failure(memory_entry, task_type=TASK_PRIMARY_ACTION)
                stale_memory = memory_entry
            elif memory_entry is not None:
                self._record_apply_memory_failure(memory_entry, task_type=TASK_PRIMARY_ACTION)
                stale_memory = memory_entry

            for action_round in range(self._agentic_retry_budget(default=4)):
                root = await self._easy_apply_root(page)
                footer_primary = await self._locate_easy_apply_footer_primary_button(root)
                footer_snapshot = None
                footer_action = None
                if footer_primary is not None:
                    footer_button, footer_label = footer_primary
                    footer_snapshot = await browser_agent.capture_task_snapshot(
                        page=page,
                        focus_locator=root,
                        priority_locator=footer_button,
                    )
                    footer_action = await self._click_easy_apply_footer_primary_button(
                        page,
                        primary_button=footer_button,
                        primary_label=footer_label,
                        step=step,
                    )
                if footer_action is not None:
                    if footer_snapshot is not None and footer_action.element_id is None:
                        priority_element_id = next(
                            (
                                element.element_id
                                for element in footer_snapshot.elements
                                if element.is_priority_target
                            ),
                            None,
                        )
                        if priority_element_id is not None:
                            footer_action = replace(footer_action, element_id=priority_element_id)
                    last_action = footer_action
                    recent_actions.append(
                        {
                            "action_round": action_round,
                            "action_type": footer_action.action_type,
                            "action_intent": footer_action.action_intent,
                            "reasoning": footer_action.reasoning,
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
                            "action_type": footer_action.action_type,
                            "action_intent": footer_action.action_intent,
                            "reasoning": footer_action.reasoning,
                        },
                    )
                    action_succeeded = False
                    if footer_action.action_intent == "advance_step":
                        await self._wait_for_easy_apply_surface(page)
                        current_step = await self._extract_step(
                            page,
                            last_known_step_index=step.step_index,
                            last_known_total_steps=step.total_steps,
                        )
                        action_succeeded = (
                            current_step.step_index != step.step_index
                            or _step_surface_changed(step, current_step)
                        )
                    elif self._action_indicates_submission(footer_action):
                        # The final submit CTA can trigger a slow transition before LinkedIn
                        # shows confirmation or closes the modal. Return the submit action
                        # immediately and let _await_submission_outcome own that state check.
                        action_succeeded = True

                    if action_succeeded:
                        if footer_snapshot is not None:
                            self._promote_apply_memory(
                                task_type=TASK_PRIMARY_ACTION,
                                signature_payload=signature_payload,
                                action=footer_action,
                                snapshot=footer_snapshot,
                                existing_memory=stale_memory,
                                replace_existing=stale_memory is not None,
                            )
                        return footer_action
                snapshot = await browser_agent.capture_task_snapshot(
                    page=page,
                    focus_locator=root,
                    priority_locator=None,
                )
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
                            "When the current surface has zero visible form fields, treat it as "
                            "a review, confirmation, or finalization surface. Do not invent "
                            "missing inputs. Inspect the visible primary CTA and choose between "
                            "advance_step and submit_application based on that CTA."
                        ),
                        (
                            "If there are zero visible fields and a single dominant primary CTA "
                            "is already visible, prefer clicking that CTA immediately instead of "
                            "scrolling again."
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
                if action.action_type == "click" and (
                    action.action_intent == "advance_step"
                    or self._action_indicates_submission(action)
                ):
                    if action.action_intent == "advance_step":
                        await self._wait_for_easy_apply_surface(page)
                    self._promote_apply_memory(
                        task_type=TASK_PRIMARY_ACTION,
                        signature_payload=signature_payload,
                        action=action,
                        snapshot=snapshot,
                        existing_memory=stale_memory,
                        replace_existing=stale_memory is not None,
                    )
                    return action
                if action.action_type == "done" and self._action_indicates_submission(action):
                    return action
                if action.action_type in {"done", "fail"}:
                    return action
        except BrowserAutomationError as exc:
            raise LinkedInEasyApplyError(str(exc)) from exc
        if last_action is not None:
            return last_action
        msg = "Browser agent could not determine how to advance the Easy Apply step."
        raise LinkedInEasyApplyError(msg)

    async def _locate_easy_apply_footer_primary_button(
        self,
        root: Locator,
    ) -> tuple[Locator, str] | None:
        footers = root.locator("footer")
        footer_count = await footers.count()
        if footer_count == 0:
            return None

        primary_button: Locator | None = None
        primary_label: str | None = None
        for footer_index in range(footer_count - 1, -1, -1):
            footer = footers.nth(footer_index)
            footer_buttons = footer.locator("button, [role='button']")
            button_count = await footer_buttons.count()
            if button_count == 0:
                continue

            visible_buttons: list[tuple[Locator, str]] = []
            for button_index in range(button_count):
                button = footer_buttons.nth(button_index)
                try:
                    if not await button.is_visible():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                try:
                    if not await button.is_enabled():
                        continue
                except Exception:  # noqa: BLE001
                    pass
                label = await self._read_locator_text(button)
                if not label:
                    continue
                visible_buttons.append((button, label))
            if visible_buttons:
                primary_button, primary_label = visible_buttons[-1]
                break

        if primary_button is None or primary_label is None:
            return None

        return primary_button, primary_label

    async def _click_easy_apply_footer_primary_button(
        self,
        page: Page,
        *,
        primary_button: Locator,
        primary_label: str,
        step: EasyApplyStep,
    ) -> BrowserAgentAction | None:

        try:
            await primary_button.scroll_into_view_if_needed(timeout=2_000)
            await page.wait_for_timeout(150)
            await primary_button.click(timeout=3_000)
            await page.wait_for_timeout(250)
        except Exception:  # noqa: BLE001
            return None

        inferred_action_intent = self._infer_easy_apply_footer_action_intent(
            primary_label,
            step=step,
        )

        if inferred_action_intent == "submit_application":
            action_intent = "submit_application"
            reasoning = (
                "Clicked the primary button in the Easy Apply footer and treated it as the "
                f"final submit action based on the visible CTA label {primary_label!r}."
            )
        elif not await self._easy_apply_modal_visible(page):
            action_intent = "submit_application"
            reasoning = (
                "Clicked the primary button in the Easy Apply footer and the modal closed, "
                "which indicates the application was submitted."
            )
        else:
            action_intent = "advance_step"
            reasoning = (
                "Clicked the primary button in the Easy Apply footer to advance the current "
                f"step. The visible footer CTA label was {primary_label!r}."
            )
            await self._wait_for_easy_apply_surface(page)

        return BrowserAgentAction(
            action_type="click",
            element_id=None,
            value_source=None,
            value=None,
            action_intent=action_intent,
            key_name=None,
            scroll_target=None,
            scroll_direction=None,
            scroll_amount=0,
            wait_seconds=0,
            reasoning=reasoning,
        )

    def _infer_easy_apply_footer_action_intent(
        self,
        label: str,
        *,
        step: EasyApplyStep,
    ) -> str:
        normalized_label = normalize_text(label)
        if not normalized_label:
            return "advance_step"

        if any(token in normalized_label for token in ("next", "review", "continue")):
            return "advance_step"

        if any(token in normalized_label for token in ("submit", "send", "apply")):
            return "submit_application"

        if step.total_steps > 0 and step.step_index >= step.total_steps - 1:
            return "submit_application"

        return "advance_step"

    def _action_indicates_submission(self, action: BrowserAgentAction) -> bool:
        normalized_intent = normalize_text(action.action_intent or "")
        if normalized_intent in {
            "submit_application",
            "application_submitted",
            "submitted",
            "submission_complete",
        }:
            return True
        return action.action_type == "done" and any(
            token in normalized_intent for token in ("submit", "submitted", "complete", "completed")
        )

    async def _read_locator_text(self, locator: Locator) -> str:
        try:
            label = await locator.inner_text(timeout=500)
        except Exception:  # noqa: BLE001
            label = ""
        if not label:
            try:
                label = await locator.get_attribute("aria-label") or ""
            except Exception:  # noqa: BLE001
                label = ""
        return normalize_text(label)

    async def _wait_for_easy_apply_surface(self, page: Page, *, timeout_ms: int = 10_000) -> None:
        deadline = asyncio.get_running_loop().time() + max(1, timeout_ms) / 1_000
        while True:
            if await self._submission_confirmation_visible(page):
                return
            if await self._easy_apply_surface_ready(page):
                return
            if asyncio.get_running_loop().time() >= deadline:
                break
            await page.wait_for_timeout(350)
        if await self._submission_confirmation_visible(page):
            return
        if await self._easy_apply_modal_visible(page):
            msg = "LinkedIn Easy Apply dialog did not finish loading after the last step action."
        else:
            msg = "LinkedIn Easy Apply dialog is not visible after the last step action."
        raise LinkedInEasyApplyError(msg)

    async def _easy_apply_surface_ready(self, page: Page) -> bool:
        try:
            root = await self._easy_apply_root(page)
        except LinkedInEasyApplyError:
            return False
        try:
            readiness = await root.evaluate(
                """
                (node) => {
                  const isVisible = (element) => {
                    if (!element) {
                      return false;
                    }
                    const style = window.getComputedStyle(element);
                    if (
                      style.display === "none"
                      || style.visibility === "hidden"
                      || style.opacity === "0"
                    ) {
                      return false;
                    }
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };

                  const textOf = (element) => {
                    if (!element) {
                      return "";
                    }
                    const text = (
                      element.innerText
                      || element.textContent
                      || element.getAttribute("aria-label")
                      || ""
                    );
                    return text.replace(/\\s+/g, " ").trim();
                  };

                  const countVisible = (selector) =>
                    Array.from(node.querySelectorAll(selector)).filter(isVisible).length;
                  const visibleTexts = (selector) =>
                    Array.from(node.querySelectorAll(selector))
                      .filter(isVisible)
                      .map(textOf)
                      .filter(Boolean);

                  const controls = countVisible(
                    [
                      "input:not([type=hidden]):not([disabled])",
                      "select:not([disabled])",
                      "textarea:not([disabled])",
                      "[role='radiogroup']",
                      "[role='group'] input:not([type=hidden]):not([disabled])",
                      "[role='combobox']",
                    ].join(", "),
                  );
                  const visibleLabels = countVisible("label, legend");
                  const footerButtons = visibleTexts("footer button, footer [role='button']");
                  const nonDismissFooterButtons = footerButtons.filter((label) => {
                    const normalized = label.toLowerCase();
                    return !normalized.startsWith("dismiss") && !normalized.startsWith("close");
                  });
                  const headerTexts = visibleTexts("header h1, header h2, h1, h2");
                  const markerTexts = visibleTexts("p, span, div").filter((label) => {
                    const normalized = label.toLowerCase();
                    return (
                      /\\b\\d+\\s*\\/\\s*\\d+\\s*pages\\b/.test(normalized)
                      || /\\b\\d+\\s*percent complete\\b/.test(normalized)
                      || normalized.includes("apply to ")
                    );
                  });
                  const sduiMarker = Array.from(node.querySelectorAll("[data-sdui-screen]"))
                    .map((element) => element.getAttribute("data-sdui-screen") || "")
                    .some((value) => /EasyApply/i.test(value));
                  const loaders = countVisible("[data-testid='loader'], [aria-busy='true']");

                  if (controls > 0) {
                    return true;
                  }
                  if (sduiMarker && markerTexts.length > 0 && nonDismissFooterButtons.length > 0) {
                    return true;
                  }
                  if (headerTexts.some((label) => /apply to /i.test(label)) && controls > 0) {
                    return true;
                  }
                  if (visibleLabels > 0 && nonDismissFooterButtons.length > 0) {
                    return true;
                  }
                  if (loaders > 0) {
                    return false;
                  }
                  return nonDismissFooterButtons.length > 0 && markerTexts.length > 0;
                }
                """,
            )
            return bool(readiness)
        except PlaywrightTimeoutError:
            return False
        except Exception:  # noqa: BLE001
            return False

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
        if await self._submission_confirmation_visible(page):
            return True, "LinkedIn Easy Apply submitted successfully."
        for attempt_index in range(self._agentic_retry_budget(default=20, production_cap=6)):
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

    async def _submit_transition_requires_job_page_recheck(self, page: Page) -> bool:
        try:
            current_url = page.url
        except Exception:  # noqa: BLE001
            current_url = ""
        normalized_url = normalize_text(current_url or "")
        if normalized_url == "about:blank":
            return True
        if await self._easy_apply_modal_visible(page):
            return False
        try:
            visible_text = await page.evaluate(
                """
                () => (document.body?.innerText || '').replace(/\\s+/g, ' ').trim()
                """,
            )
        except Exception:  # noqa: BLE001
            return False
        return not bool(normalize_text(str(visible_text or "")))

    async def _confirm_submission_via_job_page(
        self,
        page: Page,
        *,
        posting: JobPosting,
        settings: UserAgentSettings,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
    ) -> tuple[bool, str | None]:
        self._record_event(
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=ExecutionEventType.STEP_REACHED,
            payload={
                "stage": "easy_apply_post_submit_recheck",
                "job_posting_id": str(posting.id),
                "url_before_recheck": page.url,
            },
        )
        try:
            await self._open_job_detail_page(page, posting=posting)
        except LinkedInEasyApplyError as exc:
            return False, str(exc)
        await page.wait_for_timeout(750)
        notes = await self._detect_existing_application_on_job_page(page, settings=settings)
        if notes is None:
            return False, None
        self._record_event(
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=ExecutionEventType.STEP_REACHED,
            payload={
                "stage": "easy_apply_post_submit_recheck_confirmed",
                "job_posting_id": str(posting.id),
                "notes": notes,
            },
        )
        return True, notes

    async def _submission_confirmation_visible(self, page: Page) -> bool:
        try:
            return bool(
                await page.evaluate(
                    """
                    () => {
                      const collapse = (value) =>
                        (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
                      const isVisible = (element) => {
                        if (!element) {
                          return false;
                        }
                        const style = window.getComputedStyle(element);
                        if (
                          style.display === "none" ||
                          style.visibility === "hidden" ||
                          style.opacity === "0"
                        ) {
                          return false;
                        }
                        const rect = element.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                      };
                      const visibleTexts = Array.from(
                        document.querySelectorAll("h1, h2, h3, p, span, div, button")
                      )
                        .filter(isVisible)
                        .map((element) => collapse(
                          element.innerText
                          || element.textContent
                          || element.getAttribute("aria-label")
                          || ""
                        ))
                        .filter(Boolean);
                      const pageText = visibleTexts.join(" ");
                      const hasSubmittedText =
                        pageText.includes("application submitted")
                        || pageText.includes("application sent")
                        || pageText.includes("your application was sent")
                        || pageText.includes("candidatura enviada")
                        || pageText.includes("candidatura enviada agora")
                        || pageText.includes("inscrição enviada")
                        || pageText.includes("aplicação enviada");
                      if (!hasSubmittedText) {
                        return false;
                      }
                      const dismissLikeButtons = visibleTexts.filter(
                        (text) =>
                          text.includes("done")
                          || text.includes("close")
                          || text.includes("dismiss")
                          || text.includes("fechar")
                          || text.includes("concluído")
                          || text.includes("concluido")
                          || text.includes("descartar")
                      );
                      const profileButtons = visibleTexts.filter(
                        (text) =>
                          text.includes("update profile")
                          || text.includes("atualizar perfil")
                      );
                      return (
                        dismissLikeButtons.length > 0
                        || profileButtons.length > 0
                        || hasSubmittedText
                      );
                    }
                    """,
                )
            )
        except Exception:  # noqa: BLE001
            return False

    async def _assess_easy_apply_step_state(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
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
            posting=posting,
            execution_id=execution_id,
            submission_id=submission_id,
            execution_events=execution_events,
            previous_step=step,
            step_answers=step_answers,
            repair_origin="post_primary_action",
        )
        current_step = await self._extract_step(
            page,
            last_known_step_index=step.step_index,
            last_known_total_steps=step.total_steps,
        )
        if current_step.step_index != step.step_index:
            return "complete", "The Easy Apply flow advanced to the next step."
        if _step_surface_changed(step, current_step):
            self._record_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "easy_apply_surface_changed_without_step_counter",
                    "previous_step_index": step.step_index,
                    "current_step_index": current_step.step_index,
                    "field_count": len(current_step.fields),
                    "field_summaries": [
                        _field_debug_summary(field) for field in current_step.fields
                    ],
                },
            )
            return (
                "complete",
                "The Easy Apply modal changed to a new surface without updating the step counter.",
            )
        root = await self._easy_apply_root(page)
        if current_step.fields:
            deterministic_blocker = await self._step_has_deterministic_blocker(
                root,
                current_step,
            )
            if not deterministic_blocker:
                return (
                    "pending",
                    "No visible invalid or incomplete Easy Apply field remains after the latest "
                    "action.",
                )
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
                    "Do not treat standalone helper counters like '1/20' or "
                    "'1 of 20 characters' as validation errors unless the same field also "
                    "shows explicit invalid/error wording or invalid styling."
                ),
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

    async def _resolve_easy_apply_bottleneck_with_agent(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
        step: EasyApplyStep,
        blocked_summary: str | None,
        step_answers: tuple[ApplicationAnswer, ...],
    ) -> tuple[str, str | None]:
        browser_agent = self._create_browser_agent(settings)
        available_values = self._build_easy_apply_remediation_values(
            settings=settings,
            posting=posting,
            step_answers=step_answers,
        )
        recent_actions: list[dict[str, object]] = []

        for remediation_round in range(self._agentic_retry_budget(default=4)):
            if not await self._easy_apply_modal_visible(page):
                return "blocked", "The Easy Apply modal is no longer visible during remediation."

            current_step = await self._extract_step(
                page,
                last_known_step_index=step.step_index,
                last_known_total_steps=step.total_steps,
            )
            if current_step.step_index != step.step_index:
                return "complete", "The Easy Apply flow advanced after blocker remediation."
            if _step_surface_changed(step, current_step):
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_surface_changed_during_remediation",
                        "previous_step_index": step.step_index,
                        "current_step_index": current_step.step_index,
                        "field_count": len(current_step.fields),
                        "field_summaries": [
                            _field_debug_summary(field) for field in current_step.fields
                        ],
                    },
                )
                return (
                    "complete",
                    "The Easy Apply modal changed to a new field surface during remediation.",
                )

            root = await self._easy_apply_root(page)
            if current_step.fields:
                deterministic_blocker = await self._step_has_deterministic_blocker(
                    root,
                    current_step,
                )
                if not deterministic_blocker:
                    return (
                        "complete",
                        "No visible invalid or incomplete Easy Apply field remains after "
                        "remediation.",
                    )
            diagnosis = await self._assess_browser_state_with_agent(
                page,
                settings=settings,
                task_name="linkedin_easy_apply_blocker_diagnosis",
                goal=(
                    "Assess the current blocker inside the LinkedIn Easy Apply flow and decide "
                    "whether the step is still blocked, already ready to continue, or still "
                    "settling after the last action."
                ),
                extra_rules=(
                    (
                        "Use blocked when a visible validation issue, chooser problem, or "
                        "blocking surface still prevents progress."
                    ),
                    (
                        "Standalone helper counters like '1/20' or '1 of 20 characters' are "
                        "not blocking errors by themselves. Treat them as helper text unless "
                        "the same field also shows explicit invalid/error wording or invalid "
                        "styling."
                    ),
                    (
                        "Use complete only when the current step is visibly ready for the "
                        "next primary action or already advanced."
                    ),
                    "Use pending while the current modal or blocker is still changing.",
                ),
                recent_actions=tuple(recent_actions[-6:]),
                step_index=step.step_index,
                focus_locator=root,
            )
            self._record_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "easy_apply_blocker_diagnosis",
                    "step_index": step.step_index,
                    "remediation_round": remediation_round,
                    "status": diagnosis.status,
                    "confidence": diagnosis.confidence,
                    "summary": diagnosis.summary,
                    "evidence": list(diagnosis.evidence),
                },
            )
            if diagnosis.status != "blocked":
                return diagnosis.status, diagnosis.summary

            priority_locator = await self._find_priority_blocker_locator(root, current_step)
            action = await browser_agent.perform_single_task_action(
                page=page,
                available_values=available_values,
                goal=(
                    "Resolve the current visible blocker inside the LinkedIn Easy Apply flow "
                    "so the step becomes ready for the main continue or submit action."
                ),
                task_name="linkedin_easy_apply_resolve_blocker",
                extra_rules=(
                    (
                        f"The latest blocker summary is {blocked_summary!r}."
                        if blocked_summary
                        else "There is no earlier blocker summary from the caller."
                    ),
                    f"The current diagnosis summary is {diagnosis.summary!r}.",
                    (
                        "Stay inside the current blocking surface and fix the visible issue. "
                        "This may include selecting an autocomplete option, confirming a "
                        "blocking dialog, or repairing an invalid field."
                    ),
                    (
                        "You may use field_value_* sources to re-apply answers that were "
                        "already computed for this step."
                    ),
                    ("Do not close, dismiss, save, or back out of the application."),
                    (
                        "Do not click the main Next, Review, or Submit button unless a "
                        "blocking confirmation surface specifically requires a continue or "
                        "confirm action to return to the form."
                    ),
                    (
                        "If the visible issue is below the fold inside the modal, scroll the "
                        "active surface instead of the page."
                    ),
                ),
                allowed_action_types=("click", "fill", "press", "scroll", "wait", "done", "fail"),
                recent_actions=recent_actions[-6:],
                step_index=remediation_round,
                focus_locator=root,
                priority_locator=priority_locator,
            )
            recent_actions.append(
                {
                    "remediation_round": remediation_round,
                    "action_type": action.action_type,
                    "action_intent": action.action_intent,
                    "reasoning": action.reasoning,
                    "diagnosis_summary": diagnosis.summary,
                }
            )
            self._record_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "easy_apply_blocker_resolution_action",
                    "step_index": step.step_index,
                    "remediation_round": remediation_round,
                    "action_type": action.action_type,
                    "action_intent": action.action_intent,
                    "reasoning": action.reasoning,
                },
            )

        final_diagnosis = await self._assess_browser_state_with_agent(
            page,
            settings=settings,
            task_name="linkedin_easy_apply_blocker_diagnosis",
            goal=(
                "Assess whether the current LinkedIn Easy Apply blocker was resolved after the "
                "latest remediation attempts."
            ),
            extra_rules=(
                "Use blocked when the visible issue is still preventing the step from continuing.",
                (
                    "Standalone helper counters like '1/20' or '1 of 20 characters' are not "
                    "blocking errors by themselves. Treat them as helper text unless the same "
                    "field also shows explicit invalid/error wording or invalid styling."
                ),
                (
                    "Use complete only when the current step is ready for the main "
                    "primary action or already advanced."
                ),
                "Use pending while the UI is still settling after the last remediation.",
            ),
            recent_actions=tuple(recent_actions[-6:]),
            step_index=step.step_index,
            focus_locator=await self._easy_apply_root(page),
        )
        return final_diagnosis.status, final_diagnosis.summary

    def _build_easy_apply_remediation_values(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        step_answers: tuple[ApplicationAnswer, ...],
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        seen_keys: dict[str, int] = {}
        for answer in step_answers:
            raw_value = answer.answer_raw.strip()
            if not raw_value:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", normalize_text(answer.normalized_key)).strip("_")
            slug = slug or "field"
            base_key = f"field_value_{slug}"
            suffix = seen_keys.get(base_key, 0)
            seen_keys[base_key] = suffix + 1
            key = base_key if suffix == 0 else f"{base_key}_{suffix + 1}"
            values[key] = raw_value
        values.setdefault("profile_full_name", settings.profile.name)
        values.setdefault("profile_first_name", _profile_first_name(settings.profile.name))
        values.setdefault("profile_last_name", _profile_last_name(settings.profile.name))
        values.setdefault("profile_city", settings.profile.city)
        values.setdefault("profile_email", settings.profile.email)
        values.setdefault("profile_phone", settings.profile.phone)
        values.setdefault("job_title", posting.title)
        values.setdefault("job_company_name", posting.company_name)
        if posting.location:
            values.setdefault("job_location", posting.location)
        if settings.profile.salary_expectation is not None:
            values.setdefault(
                "profile_salary_expectation",
                str(settings.profile.salary_expectation),
            )
        if settings.profile.availability.strip():
            values.setdefault("profile_availability", settings.profile.availability.strip())
        for key, value in settings.profile.default_responses.items():
            if not value.strip():
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", normalize_text(key)).strip("_") or "default"
            values.setdefault(f"default_response_{slug}", value.strip())
        return values

    async def _find_priority_blocker_locator(
        self,
        root: Locator,
        step: EasyApplyStep,
    ) -> Locator | None:
        for field in step.fields:
            locator = await self._find_control_locator(root, field)
            if locator is None:
                continue
            if field.control_kind in {"text", "textarea"}:
                state = await self._inspect_text_field_interaction(locator)
                if state.needs_agentic_follow_up or state.invalid:
                    return locator
                continue
            if await self._control_has_invalid_state(locator):
                return locator
        return None

    async def _step_has_deterministic_blocker(
        self,
        root: Locator,
        step: EasyApplyStep,
    ) -> bool:
        for field in step.fields:
            locator = await self._find_control_locator(root, field)
            if locator is None:
                continue
            if field.control_kind in {"text", "textarea"}:
                state = await self._inspect_text_field_interaction(locator)
                if state.invalid or state.needs_agentic_follow_up:
                    return True
                if field.required and not state.has_value:
                    return True
                continue
            control_state = await self._inspect_control_validation_state(locator)
            if control_state.invalid:
                return True
            if field.required and not await self._field_has_live_required_value(
                root,
                field,
                locator,
                control_state=control_state,
            ):
                return True
        return False

    async def _field_has_live_required_value(
        self,
        root: Locator,
        field: EasyApplyField,
        locator: Locator,
        *,
        control_state: ControlValidationState | None = None,
    ) -> bool:
        match field.control_kind:
            case "radio":
                return await self._radio_field_has_selected_option(root, field)
            case "checkbox":
                return await _checkbox_option_is_checked(locator)
            case _:
                state = control_state or await self._inspect_control_validation_state(locator)
                normalized_value = normalize_text(state.current_value)
                return bool(
                    normalized_value
                    and normalized_value
                    not in {
                        "select an option",
                        "choose an option",
                        "select",
                        "choose",
                        "selecione",
                        "selecione uma opcao",
                    }
                )

    async def _radio_field_has_selected_option(
        self,
        root: Locator,
        field: EasyApplyField,
    ) -> bool:
        if field.option_refs:
            for option_ref in field.option_refs:
                locator = root.locator(
                    _attribute_selector("data-job-applier-option-ref", option_ref),
                )
                if not await locator.count():
                    continue
                if await _radio_option_is_checked(locator.first):
                    return True
            return False

        if field.name:
            group = root.locator(
                f'input[type="radio"]{_attribute_selector("name", field.name)}',
            )
            count = await group.count()
            for index in range(count):
                if await _radio_option_is_checked(group.nth(index)):
                    return True
            return False

        group_locator = await self._resolve_radio_group_locator(root, field)
        if group_locator is not None:
            group = group_locator.locator('input[type="radio"]')
            count = await group.count()
            for index in range(count):
                if await _radio_option_is_checked(group.nth(index)):
                    return True
            return False

        fallback_locator = await self._find_control_locator(root, field)
        if fallback_locator is None:
            return False
        return await _radio_option_is_checked(fallback_locator)

    async def _control_has_invalid_state(self, locator: Locator) -> bool:
        return (await self._inspect_control_validation_state(locator)).invalid

    async def _inspect_control_validation_state(self, locator: Locator) -> ControlValidationState:
        try:
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
                    return (
                      rect.width > 0
                      && rect.height > 0
                      && style.visibility !== "hidden"
                      && style.display !== "none"
                    );
                  };
                  const fieldRect = node.getBoundingClientRect();
                  const overlapsHorizontally = (candidateRect) => {
                    const overlap = Math.min(fieldRect.right, candidateRect.right)
                      - Math.max(fieldRect.left, candidateRect.left);
                    return overlap > Math.min(fieldRect.width, candidateRect.width) * 0.25;
                  };
                  const looksLikeCharacterCounter = (text) =>
                    /^\\d+\\s*\\/\\s*\\d+(?:\\s+\\d+\\s+\\S+\\s+\\d+(?:\\s+\\S+)*)?$/i.test(text);
                  const hasErrorSignal = (candidate) => {
                    if (!candidate || candidate.nodeType !== 1) {
                      return false;
                    }
                    const role = collapse(candidate.getAttribute("role"));
                    const ariaLive = collapse(candidate.getAttribute("aria-live"));
                    const metadata = collapse(
                      [
                        candidate.getAttribute("class"),
                        candidate.getAttribute("id"),
                        candidate.getAttribute("data-test-form-element-error-messages"),
                      ]
                        .filter(Boolean)
                        .join(" ")
                    );
                    return (
                      role === "alert"
                      || ariaLive === "assertive"
                      || candidate.getAttribute("aria-invalid") === "true"
                      || /(?:^|\\s)(?:error|invalid|warning)(?:\\s|$)/i.test(metadata)
                      || metadata.includes("artdeco-inline-feedback__message")
                      || metadata.includes("fb-dash-form-element__error-message")
                    );
                  };
                  const validationTexts = [];
                  const seenValidationTexts = new Set();
                  const pushValidationText = (candidate) => {
                    if (!candidate || candidate.nodeType !== 1 || !isVisible(candidate)) {
                      return;
                    }
                    const text = collapse(candidate.innerText || candidate.textContent || "");
                    if (!text || seenValidationTexts.has(text) || text.length > 180) {
                      return;
                    }
                    if (looksLikeCharacterCounter(text) && !hasErrorSignal(candidate)) {
                      return;
                    }
                    const rect = candidate.getBoundingClientRect();
                    const verticalGap = rect.top - fieldRect.bottom;
                    if (verticalGap < -8 || verticalGap > 120 || !overlapsHorizontally(rect)) {
                      return;
                    }
                    seenValidationTexts.add(text);
                    validationTexts.push(text);
                  };
                  const splitIds = (value) =>
                    collapse(value)
                      .split(/\\s+/)
                      .map((item) => item.trim())
                      .filter(Boolean);
                  for (const attributeName of ["aria-errormessage", "aria-describedby"]) {
                    for (const id of splitIds(node.getAttribute(attributeName))) {
                      pushValidationText(document.getElementById(id));
                    }
                  }
                  const scope = node.closest(
                    [
                      ".fb-form-element",
                      ".jobs-easy-apply-form-section__grouping",
                      ".jobs-easy-apply-form-element",
                      "[role='group']",
                      "fieldset",
                      "section",
                      "form",
                    ].join(", ")
                  );
                  if (scope) {
                    for (const candidate of scope.querySelectorAll(
                      [
                        "[role='alert']",
                        "[aria-live='assertive']",
                        "[aria-live='polite']",
                        ".artdeco-inline-feedback__message",
                        ".fb-dash-form-element__error-message",
                        "[data-test-form-element-error-messages]",
                      ].join(", ")
                    )) {
                      pushValidationText(candidate);
                    }
                  }
                  const ariaInvalid = node.getAttribute("aria-invalid") === "true";
                  const invalidPseudoClass =
                    typeof node.matches === "function" ? node.matches(":invalid") : false;
                  const nativeValidity =
                    typeof node.checkValidity === "function" ? node.checkValidity() : true;
                  const validationMessage = collapse(node.validationMessage || "");
                  const combinedValidationTexts = [validationMessage, ...validationTexts]
                    .filter(Boolean);
                  return {
                    invalid: ariaInvalid || invalidPseudoClass || !nativeValidity
                      || combinedValidationTexts.length > 0,
                    validation_message: combinedValidationTexts[0] || "",
                    current_value: collapse(
                      node.value
                      || node.textContent
                      || node.getAttribute("value")
                      || ""
                    ),
                  };
                }
                """,
                timeout=_FIELD_STATE_INSPECTION_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError as exc:
            msg = (
                "Timed out while inspecting the validation state of the current LinkedIn "
                "Easy Apply control."
            )
            raise LinkedInEasyApplyError(msg) from exc
        except Exception:  # noqa: BLE001
            return ControlValidationState(invalid=False)
        return ControlValidationState(
            invalid=bool(payload.get("invalid")),
            validation_message=str(payload.get("validation_message") or "").strip() or None,
            current_value=str(payload.get("current_value") or "").strip(),
        )

    async def _retry_invalid_fields_after_primary_action(
        self,
        page: Page,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        execution_id: UUID,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
        previous_step: EasyApplyStep,
        step_answers: tuple[ApplicationAnswer, ...],
        repair_origin: str = "post_primary_action",
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
            answer.normalized_key: answer for answer in step_answers if answer.answer_raw.strip()
        }

        for field in current_step.fields:
            root = await self._easy_apply_root(page)
            locator = await self._find_control_locator(root, field)
            if locator is None:
                continue
            validation_message: str | None
            current_value: str
            if field.control_kind in {"text", "textarea"}:
                text_state = await self._inspect_text_field_interaction(locator)
                invalid = text_state.invalid
                validation_message = text_state.validation_message
                current_value = text_state.current_value
            else:
                control_state = await self._inspect_control_validation_state(locator)
                invalid = control_state.invalid
                validation_message = control_state.validation_message
                current_value = control_state.current_value
            if not invalid:
                continue

            prior_answer = answer_by_key.get(field.normalized_key)
            resolved_retry = await self._answer_resolver.resolve_with_validation_feedback(
                field,
                settings,
                posting=posting,
                validation_message=validation_message,
                current_value=current_value,
                previous_answer=prior_answer.answer_raw if prior_answer is not None else None,
            )
            requires_selection_retry = bool(
                validation_message and "selection" in normalize_text(validation_message)
            )
            target_value = (
                (
                    resolved_retry.value
                    if requires_selection_retry and resolved_retry is not None
                    else None
                )
                or (prior_answer.answer_raw if prior_answer is not None else None)
                or (resolved_retry.value if resolved_retry is not None else None)
                or current_value
            )
            if not target_value.strip():
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.STEP_REACHED,
                    payload={
                        "stage": "easy_apply_retry_invalid_field_missing_target",
                        "step_index": current_step.step_index,
                        "normalized_key": field.normalized_key,
                        "validation_message": validation_message,
                        "repair_origin": repair_origin,
                    },
                )
                continue
            effective_resolution = resolved_retry
            if effective_resolution is None and prior_answer is not None:
                effective_resolution = ResolvedFieldValue(
                    value=prior_answer.answer_raw,
                    answer_source=prior_answer.answer_source,
                    fill_strategy=prior_answer.fill_strategy,
                    ambiguity_flag=prior_answer.ambiguity_flag,
                    confidence=None,
                    reasoning="reuse_previous_step_answer_for_invalid_field",
                )
            if effective_resolution is None:
                effective_resolution = ResolvedFieldValue(
                    value=target_value,
                    answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
                    fill_strategy=FillStrategy.BEST_EFFORT,
                    ambiguity_flag=True,
                    confidence=0.1,
                    reasoning="retry_current_control_value",
                )
            self._record_event(
                execution_events,
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=ExecutionEventType.STEP_REACHED,
                payload={
                    "stage": "easy_apply_retry_invalid_field",
                    "step_index": current_step.step_index,
                    "normalized_key": field.normalized_key,
                    "control_kind": field.control_kind,
                    "validation_message": validation_message,
                    "target_value": target_value,
                    "answer_source": effective_resolution.answer_source.value,
                    "fill_strategy": effective_resolution.fill_strategy.value,
                    "confidence": effective_resolution.confidence,
                    "reasoning": effective_resolution.reasoning,
                    "repair_origin": repair_origin,
                },
            )
            if field.control_kind in {"text", "textarea"}:
                try:
                    applied_value = await asyncio.wait_for(
                        self._apply_field_value(
                            page,
                            root,
                            field,
                            effective_resolution,
                            settings,
                            submission_cv_path=None,
                            semantic_retry_required=True,
                            step_index=current_step.step_index,
                            total_steps=current_step.total_steps,
                        ),
                        timeout=self._runtime_settings.linkedin_field_interaction_timeout_seconds,
                    )
                    if applied_value is None:
                        self._record_event(
                            execution_events,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            event_type=ExecutionEventType.EXECUTION_FAILED,
                            payload={
                                "stage": "easy_apply_retry_invalid_field",
                                "step_index": current_step.step_index,
                                "normalized_key": field.normalized_key,
                                "message": (
                                    "Could not apply a new value while retrying the invalid "
                                    "Easy Apply text field."
                                ),
                                "repair_origin": repair_origin,
                            },
                        )
                except TimeoutError:
                    self._record_event(
                        execution_events,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        event_type=ExecutionEventType.EXECUTION_FAILED,
                        payload={
                            "stage": "easy_apply_retry_invalid_field_timeout",
                            "step_index": current_step.step_index,
                            "normalized_key": field.normalized_key,
                            "message": (
                                "Timed out while the browser agent was trying to finish an "
                                "interactive Easy Apply field."
                            ),
                            "repair_origin": repair_origin,
                        },
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
                            "repair_origin": repair_origin,
                        },
                    )
                continue
            try:
                applied_value = await self._apply_field_value(
                    page,
                    root,
                    field,
                    effective_resolution,
                    settings,
                    submission_cv_path=None,
                    semantic_retry_required=True,
                    step_index=current_step.step_index,
                    total_steps=current_step.total_steps,
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
                        "repair_origin": repair_origin,
                    },
                )
                continue
            if applied_value is None:
                self._record_event(
                    execution_events,
                    execution_id=execution_id,
                    submission_id=submission_id,
                    event_type=ExecutionEventType.EXECUTION_FAILED,
                    payload={
                        "stage": "easy_apply_retry_invalid_field",
                        "step_index": current_step.step_index,
                        "normalized_key": field.normalized_key,
                        "message": (
                            "Could not apply a new value while retrying the invalid Easy Apply "
                            "control."
                        ),
                        "repair_origin": repair_origin,
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

    async def _assess_interactive_field_with_agent(
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
    ) -> BrowserInteractiveFieldAssessment:
        browser_agent = self._create_browser_agent(settings)
        try:
            return await browser_agent.assess_interactive_field(
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
        best_candidate: Locator | None = None
        best_score = float("-inf")
        first_visible_candidate: Locator | None = None
        for selector in (
            "dialog[data-testid='dialog'][open]",
            "[data-testid='dialog'][open]",
            "dialog[open]",
            "[data-testid='dialog']",
            ".jobs-easy-apply-modal",
            "[data-test-modal] [role='dialog']",
            "[role='dialog']",
        ):
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                if first_visible_candidate is None:
                    first_visible_candidate = candidate
                try:
                    score = float(
                        await candidate.evaluate(
                            """
                            (node) => {
                              const isVisible = (element) => {
                                if (!element) {
                                  return false;
                                }
                                const style = window.getComputedStyle(element);
                                if (
                                  style.display === "none"
                                  || style.visibility === "hidden"
                                  || style.opacity === "0"
                                ) {
                                  return false;
                                }
                                const rect = element.getBoundingClientRect();
                                return rect.width > 0 && rect.height > 0;
                              };

                              const textOf = (element) => {
                                if (!element) {
                                  return "";
                                }
                                const text = (
                                  element.innerText
                                  || element.textContent
                                  || element.getAttribute("aria-label")
                                  || ""
                                );
                                return text.replace(/\\s+/g, " ").trim();
                              };

                              const countVisible = (selector) =>
                                Array.from(node.querySelectorAll(selector)).filter(isVisible).length;
                              const visibleTexts = (selector) =>
                                Array.from(node.querySelectorAll(selector))
                                  .filter(isVisible)
                                  .map(textOf)
                                  .filter(Boolean);

                              let score = 0;
                              if (node.matches("[data-job-applier-active-surface='true']")) {
                                score += 80;
                              }

                              const sduiMatches = Array.from(
                                node.querySelectorAll("[data-sdui-screen]"),
                              )
                                .map((element) => element.getAttribute("data-sdui-screen") || "")
                                .filter(Boolean);
                              if (sduiMatches.some((value) => /EasyApply/i.test(value))) {
                                score += 120;
                              }

                              const headerTexts = visibleTexts("header h1, header h2, h1, h2");
                              if (headerTexts.some((label) => /apply to /i.test(label))) {
                                score += 35;
                              }

                              const markerTexts = visibleTexts("p, span, div");
                              if (
                                markerTexts.some((label) =>
                                  /\\b\\d+\\s*\\/\\s*\\d+\\s*pages\\b/i.test(label),
                                )
                              ) {
                                score += 30;
                              }
                              if (
                                markerTexts.some((label) =>
                                  /\\b\\d+\\s*percent complete\\b/i.test(label),
                                )
                              ) {
                                score += 20;
                              }

                              score += Math.min(
                                20,
                                countVisible(
                                  [
                                    "input:not([type=hidden]):not([disabled])",
                                    "select:not([disabled])",
                                    "textarea:not([disabled])",
                                    "[role='radiogroup']",
                                    "[role='combobox']",
                                  ].join(", "),
                                ) * 4,
                              );
                              score += Math.min(
                                12,
                                countVisible("footer button, footer [role='button']") * 4,
                              );
                              score += Math.min(6, countVisible("label, legend"));
                              return score;
                            }
                            """,
                        ),
                    )
                except Exception:  # noqa: BLE001
                    score = 0.0
                if score > best_score:
                    best_score = score
                    best_candidate = candidate
        if best_candidate is not None:
            return best_candidate
        if first_visible_candidate is not None:
            return first_visible_candidate
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
            openai_responses_max_retries=(
                self._runtime_settings.resolved_openai_responses_max_retries
            ),
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
            single_action_max_attempts=(
                self._runtime_settings.resolved_browser_agent_single_action_max_attempts
            ),
            stall_threshold=self._runtime_settings.resolved_browser_agent_stall_threshold,
            min_action_delay_ms=self._runtime_settings.linkedin_min_action_delay_ms,
            max_action_delay_ms=self._runtime_settings.linkedin_max_action_delay_ms,
            openai_max_retries=self._runtime_settings.resolved_openai_responses_max_retries,
            openai_retry_max_delay_seconds=(
                self._runtime_settings.openai_responses_retry_max_delay_seconds
            ),
        )

    def _replay_field_resolution_memory(
        self,
        field: EasyApplyField,
    ) -> tuple[
        ApplyActionMemory | None,
        str | None,
        dict[str, object] | None,
        ResolvedFieldValue | None,
    ]:
        if self._apply_memory is None:
            return None, None, None, None
        task_type = resolution_task_type_for_field(field)
        if task_type is None:
            return None, None, None, None
        if _field_disallows_adaptive_resolution_memory(field):
            return None, None, None, None
        signature_payload = build_field_resolution_task_signature(
            task_type=task_type,
            field=field,
        )
        memory = self._apply_memory.find_active_memory(
            task_type=task_type,
            signature_payload=signature_payload,
        )
        if memory is None:
            return None, task_type, signature_payload, None
        remembered_value = self._apply_memory.replay_resolution(memory=memory, field=field)
        if remembered_value is None:
            return memory, task_type, signature_payload, None
        resolution = ResolvedFieldValue(
            value=remembered_value,
            answer_source=AnswerSource.ADAPTIVE_MEMORY,
            fill_strategy=FillStrategy.ADAPTIVE_MEMORY,
            ambiguity_flag=True,
            confidence=min(0.98, 0.62 + (0.04 * max(memory.success_count, 0))),
            reasoning="adaptive_field_resolution_memory",
        )
        self._append_apply_memory_timeline(
            "apply_memory_replayed",
            task_type=task_type,
            signature_hash=memory.signature_hash,
            success_count=memory.success_count,
            failure_count=memory.failure_count,
        )
        return memory, task_type, signature_payload, resolution

    def _should_promote_field_resolution_memory(
        self,
        field: EasyApplyField,
        resolution: ResolvedFieldValue,
    ) -> bool:
        if _field_disallows_adaptive_resolution_memory(field):
            return False
        task_type = resolution_task_type_for_field(field)
        if task_type not in {TASK_RESOLVE_CHECKBOX, TASK_RESOLVE_RADIO, TASK_RESOLVE_SELECT}:
            return False
        if field.question_type is QuestionType.RESUME_UPLOAD:
            return False
        if resolution.answer_source not in {
            AnswerSource.AI,
            AnswerSource.BEST_EFFORT_AUTOFILL,
            AnswerSource.ADAPTIVE_MEMORY,
        }:
            return False
        return True

    def _promote_field_resolution_memory(
        self,
        *,
        task_type: str,
        signature_payload: dict[str, object],
        field: EasyApplyField,
        resolved_value: str,
        existing_memory: ApplyActionMemory | None = None,
        replace_existing: bool = False,
    ) -> None:
        if self._apply_memory is None:
            return
        promoted = self._apply_memory.promote_successful_resolution(
            task_type=task_type,
            signature_payload=signature_payload,
            field=field,
            resolved_value=resolved_value,
            existing_memory=existing_memory,
            replace_existing=replace_existing,
        )
        if promoted is None:
            return
        logger.info(
            "linkedin_field_resolution_memory_promoted",
            extra={
                "task_type": task_type,
                "signature_hash": promoted.signature_hash,
                "replace_existing": replace_existing,
                "success_count": promoted.success_count,
            },
        )
        self._append_apply_memory_timeline(
            "apply_memory_promoted",
            task_type=task_type,
            signature_hash=promoted.signature_hash,
            success_count=promoted.success_count,
            failure_count=promoted.failure_count,
            replace_existing=replace_existing,
        )

    def _build_priority_target_click_action(
        self,
        *,
        snapshot: BrowserAgentSnapshot,
        action_intent: str,
    ) -> BrowserAgentAction | None:
        priority_element = next(
            (
                element
                for element in snapshot.elements
                if element.is_priority_target and not element.disabled
            ),
            None,
        )
        if priority_element is None:
            return None
        return BrowserAgentAction(
            action_type="click",
            element_id=priority_element.element_id,
            value_source=None,
            value=None,
            action_intent=action_intent,
            key_name=None,
            scroll_target=None,
            scroll_direction=None,
            scroll_amount=0,
            wait_seconds=0,
            reasoning=(
                "Promoted a deterministic priority-target click that completed the radio selection."
            ),
        )

    async def _attempt_replay_apply_memory(
        self,
        *,
        browser_agent: OpenAIResponsesBrowserAgent,
        page: Page,
        task_type: str,
        signature_payload: dict[str, object],
        available_values: dict[str, str],
        focus_locator: Locator | None = None,
        priority_locator: Locator | None = None,
    ) -> tuple[ApplyActionMemory | None, BrowserAgentAction | None, BrowserAgentSnapshot | None]:
        if self._apply_memory is None:
            return None, None, None

        memory = self._apply_memory.find_active_memory(
            task_type=task_type,
            signature_payload=signature_payload,
        )
        if memory is None:
            return None, None, None

        snapshot = await browser_agent.capture_task_snapshot(
            page=page,
            focus_locator=focus_locator,
            priority_locator=priority_locator,
        )
        action = self._apply_memory.replay_action(memory=memory, snapshot=snapshot)
        if action is None:
            return memory, None, snapshot

        try:
            await browser_agent.replay_action(
                page=page,
                action=action,
                values=available_values,
                snapshot=snapshot,
            )
        except BrowserAutomationError:
            return memory, None, snapshot

        logger.info(
            "linkedin_apply_memory_replayed",
            extra={
                "task_type": task_type,
                "signature_hash": memory.signature_hash,
                "action_type": action.action_type,
                "action_intent": action.action_intent,
            },
        )
        self._append_apply_memory_timeline(
            "apply_memory_replayed",
            task_type=task_type,
            signature_hash=memory.signature_hash,
            action_type=action.action_type,
            action_intent=action.action_intent,
            success_count=memory.success_count,
            failure_count=memory.failure_count,
        )
        return memory, action, snapshot

    def _append_apply_memory_timeline(
        self,
        event_type: str,
        *,
        task_type: str,
        signature_hash: str,
        success_count: int | None = None,
        failure_count: int | None = None,
        action_type: str | None = None,
        action_intent: str | None = None,
        replace_existing: bool | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "task_type": task_type,
            "signature_hash": signature_hash,
        }
        if success_count is not None:
            payload["success_count"] = success_count
        if failure_count is not None:
            payload["failure_count"] = failure_count
        if action_type is not None:
            payload["action_type"] = action_type
        if action_intent is not None:
            payload["action_intent"] = action_intent
        if replace_existing is not None:
            payload["replace_existing"] = replace_existing
        metric_by_event = {
            "apply_memory_promoted": "promoted",
            "apply_memory_replayed": "replayed",
            "apply_memory_refreshed": "refreshed",
            "apply_memory_degraded": "degraded",
        }
        metric = metric_by_event.get(event_type)
        if metric is not None:
            record_efficiency_counter(
                group="apply_memory",
                metric=metric,
                extra={"task_type": task_type, "signature_hash": signature_hash},
            )
        append_timeline_event(event_type, payload)

    def _record_apply_memory_success(
        self,
        memory: ApplyActionMemory | None,
        *,
        task_type: str,
    ) -> None:
        if self._apply_memory is None or memory is None:
            return
        refreshed = self._apply_memory.record_memory_hit_success(memory)
        logger.info(
            "linkedin_apply_memory_refreshed",
            extra={
                "task_type": task_type,
                "signature_hash": refreshed.signature_hash,
                "success_count": refreshed.success_count,
            },
        )
        self._append_apply_memory_timeline(
            "apply_memory_refreshed",
            task_type=task_type,
            signature_hash=refreshed.signature_hash,
            success_count=refreshed.success_count,
            failure_count=refreshed.failure_count,
        )

    def _record_apply_memory_failure(
        self,
        memory: ApplyActionMemory | None,
        *,
        task_type: str,
    ) -> None:
        if self._apply_memory is None or memory is None:
            return
        updated = self._apply_memory.record_memory_hit_failure(memory)
        logger.info(
            "linkedin_apply_memory_degraded",
            extra={
                "task_type": task_type,
                "signature_hash": updated.signature_hash,
                "failure_count": updated.failure_count,
            },
        )
        self._append_apply_memory_timeline(
            "apply_memory_degraded",
            task_type=task_type,
            signature_hash=updated.signature_hash,
            success_count=updated.success_count,
            failure_count=updated.failure_count,
        )

    def _promote_apply_memory(
        self,
        *,
        task_type: str,
        signature_payload: dict[str, object],
        action: BrowserAgentAction,
        snapshot: BrowserAgentSnapshot,
        existing_memory: ApplyActionMemory | None = None,
        replace_existing: bool = False,
    ) -> None:
        if self._apply_memory is None:
            return
        promoted = self._apply_memory.promote_successful_action(
            task_type=task_type,
            signature_payload=signature_payload,
            action=action,
            snapshot=snapshot,
            existing_memory=existing_memory,
            replace_existing=replace_existing,
        )
        if promoted is None:
            return
        logger.info(
            "linkedin_apply_memory_promoted",
            extra={
                "task_type": task_type,
                "signature_hash": promoted.signature_hash,
                "replace_existing": replace_existing,
                "success_count": promoted.success_count,
            },
        )
        self._append_apply_memory_timeline(
            "apply_memory_promoted",
            task_type=task_type,
            signature_hash=promoted.signature_hash,
            success_count=promoted.success_count,
            failure_count=promoted.failure_count,
            action_type=action.action_type,
            action_intent=action.action_intent,
            replace_existing=replace_existing,
        )

    async def _pause_before_navigation(self, page: Page, *, reason: str) -> None:
        delay_ms = random.randint(
            self._runtime_settings.linkedin_min_navigation_delay_ms,
            self._runtime_settings.linkedin_max_navigation_delay_ms,
        )
        logger.info("linkedin_navigation_delay", extra={"reason": reason, "delay_ms": delay_ms})
        await page.wait_for_timeout(delay_ms)

    async def _open_job_detail_page(self, page: Page, *, posting: JobPosting) -> None:
        default_timeout_ms = self._runtime_settings.linkedin_default_timeout_ms
        timeout_attempts = (
            default_timeout_ms,
            max(default_timeout_ms * 2, 30_000),
        )
        last_error: Exception | None = None
        for attempt_index, timeout_ms in enumerate(timeout_attempts):
            await self._pause_before_navigation(page, reason="job_detail_open")
            try:
                await page.goto(
                    posting.url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                return
            except PlaywrightTimeoutError as exc:
                last_error = exc
                append_timeline_event(
                    "easy_apply_job_page_retry",
                    {
                        "job_posting_id": str(posting.id),
                        "external_job_id": posting.external_job_id,
                        "attempt_index": attempt_index,
                        "timeout_ms": timeout_ms,
                        "error": str(exc),
                    },
                )
                if attempt_index >= len(timeout_attempts) - 1:
                    break
                try:
                    await page.goto("about:blank", wait_until="domcontentloaded", timeout=5_000)
                except Exception:  # noqa: BLE001
                    pass
                await page.wait_for_timeout(500)
        msg = "LinkedIn job page did not finish loading in time for the current Easy Apply flow."
        raise LinkedInEasyApplyError(msg) from last_error

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

        append_artifact_reference(
            artifact_type=ArtifactType.PLAYWRIGHT_TRACE.value,
            label="playwright_trace",
            path=trace_path,
            sha256=_sha256_file(trace_path),
        )
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
        digest = _sha256_file(path)
        append_artifact_reference(
            artifact_type=ArtifactType.SCREENSHOT.value,
            label=path.stem,
            path=path,
            sha256=digest,
        )
        return ArtifactSnapshot(
            submission_id=submission_id,
            artifact_type=ArtifactType.SCREENSHOT,
            path=str(path),
            sha256=digest,
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
        digest = _sha256_file(path)
        append_artifact_reference(
            artifact_type=ArtifactType.HTML_DUMP.value,
            label=path.stem,
            path=path,
            sha256=digest,
        )
        return ArtifactSnapshot(
            submission_id=submission_id,
            artifact_type=ArtifactType.HTML_DUMP,
            path=str(path),
            sha256=digest,
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

    async def _prepare_submission_cv_path(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        scored_job: ScoredJobPosting,
        execution_id: UUID,
        run_dir: Path,
        submission_id: UUID,
        execution_events: list[ExecutionEvent],
    ) -> PreparedSubmissionCv | None:
        prepared = await asyncio.to_thread(
            self._dynamic_resume_builder.build_for_job,
            settings=settings,
            posting=posting,
            matched_role_target=scored_job.matched_role_target,
            matched_specializations=scored_job.matched_specializations,
            run_dir=run_dir,
            submission_id=submission_id,
        )
        if prepared is None:
            return None
        artifacts = self._build_dynamic_resume_artifacts(
            prepared=prepared,
            submission_id=submission_id,
        )
        self._record_event(
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=ExecutionEventType.STEP_REACHED,
            payload={
                "stage": "dynamic_resume_variant_selected",
                "job_posting_id": str(posting.id),
                "source_cv_path": str(prepared.source_cv_path),
                "submission_cv_path": str(prepared.submission_cv_path),
                "resume_mode": prepared.resume_mode.value,
                "target_language": prepared.target_language.value,
                "matched_role_target": prepared.matched_role_target,
                "matched_specializations": list(prepared.matched_specializations),
                "cv_version": prepared.cv_version,
                "used_dynamic_variant": prepared.used_dynamic_variant,
                "notes": prepared.notes,
            },
        )
        return PreparedSubmissionCv(
            path=prepared.submission_cv_path,
            cv_version=prepared.cv_version,
            resume_mode=prepared.resume_mode,
            target_language=prepared.target_language,
            matched_role_target=prepared.matched_role_target,
            matched_specializations=prepared.matched_specializations,
            artifacts=artifacts,
            used_dynamic_variant=prepared.used_dynamic_variant,
            notes=prepared.notes,
        )

    def _build_dynamic_resume_artifacts(
        self,
        *,
        prepared: DynamicResumeBuildResult,
        submission_id: UUID,
    ) -> tuple[ArtifactSnapshot, ...]:
        paths: list[Path] = []
        if prepared.used_dynamic_variant:
            paths.append(prepared.submission_cv_path)
        if prepared.markdown_path is not None:
            paths.append(prepared.markdown_path)
        if prepared.css_path is not None:
            paths.append(prepared.css_path)
        unique_paths: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            key = str(path.resolve())
            if key in seen or not path.exists() or not path.is_file():
                continue
            seen.add(key)
            unique_paths.append(path)
        artifacts: list[ArtifactSnapshot] = []
        for path in unique_paths:
            append_artifact_reference(
                artifact_type=ArtifactType.CV_METADATA.value,
                label=path.name,
                path=path,
                sha256=_sha256_file(path),
            )
            artifacts.append(
                _build_file_artifact(
                    submission_id=submission_id,
                    path=path,
                    artifact_type=ArtifactType.CV_METADATA,
                ),
            )
        return tuple(artifacts)

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

    def _build_progress_job(
        self,
        posting: JobPosting,
        submission_id: UUID,
        *,
        status: str | None = None,
        skip_reason: str | None = None,
    ) -> dict[str, str]:
        payload = {
            "job_posting_id": str(posting.id),
            "external_job_id": posting.external_job_id or "",
            "submission_id": str(submission_id),
            "company_name": posting.company_name,
            "title": posting.title,
            "url": posting.url,
        }
        if status is not None:
            payload["status"] = status
        if skip_reason is not None:
            payload["skip_reason"] = skip_reason
        return payload

    def _record_event(
        self,
        execution_events: list[ExecutionEvent],
        *,
        execution_id: UUID,
        event_type: ExecutionEventType,
        payload: dict[str, object],
        submission_id: UUID | None = None,
    ) -> None:
        payload_json = json.dumps(payload, sort_keys=True)
        execution_events.append(
            ExecutionEvent(
                execution_id=execution_id,
                submission_id=submission_id,
                event_type=event_type,
                payload_json=payload_json,
            ),
        )
        append_timeline_event(event_type.value, payload)
        progress_payload: dict[str, object] = {
            "current_stage": str(payload.get("stage") or event_type.value),
        }
        if submission_id is not None:
            progress_payload["current_submission_id"] = str(submission_id)
        step_index = payload.get("step_index")
        if isinstance(step_index, int):
            progress_payload["current_step"] = step_index + 1
        current_job: dict[str, object] = {}
        for key in (
            "job_posting_id",
            "external_job_id",
            "company_name",
            "title",
            "url",
            "status",
            "score",
            "skip_reason",
        ):
            if key in payload:
                current_job[key] = payload[key]
        if current_job and submission_id is not None:
            current_job["submission_id"] = str(submission_id)
        if current_job:
            progress_payload["current_job"] = current_job
        if event_type in {
            ExecutionEventType.EXCEPTION_CAPTURED,
            ExecutionEventType.EXECUTION_FAILED,
        }:
            message = payload.get("message")
            if isinstance(message, str) and message:
                progress_payload["last_error"] = message
        update_progress_snapshot(progress_payload)

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
        scored_job: ScoredJobPosting,
        *,
        execution_id: UUID,
        origin: ExecutionOrigin,
    ) -> SubmissionAttempt:
        result = await self._executor.execute(
            settings,
            posting,
            scored_job,
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

        submission = self._persist_attempt_submission(result, posting, settings, origin)
        self._persist_execution_events(result.execution_events, keep_submission_link=True)
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
            resume_mode=result.resume_mode,
            target_language=result.target_language,
            matched_role_target=result.matched_role_target,
            matched_specializations=result.matched_specializations,
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

    def _persist_attempt_submission(
        self,
        result: EasyApplyExecutionResult,
        posting: JobPosting,
        settings: UserAgentSettings,
        origin: ExecutionOrigin,
    ) -> ApplicationSubmission:
        submission = ApplicationSubmission(
            id=result.submission_id,
            job_posting_id=posting.id,
            status=result.status,
            started_at=result.started_at,
            resume_mode=result.resume_mode,
            target_language=result.target_language,
            matched_role_target=result.matched_role_target,
            matched_specializations=result.matched_specializations,
            cv_version=result.cv_version or settings.profile.cv_filename,
            cover_letter_version=result.cover_letter_version,
            execution_origin=origin,
            notes=result.notes,
        )
        self._submission_repository.save(submission)
        for answer in result.answers:
            self._answer_repository.save(answer)
        for recruiter_interaction in result.recruiter_interactions:
            self._recruiter_repository.save(recruiter_interaction)
        for artifact in result.artifacts:
            self._artifact_repository.save(artifact)
        return submission

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


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    quoted_parts = [f"'{part}'" for part in parts]
    return "concat(" + ', "\'", '.join(quoted_parts) + ")"


def _existing_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.exists() and path.is_file():
        return path
    return None


def _pick_option_index(options: tuple[str, ...], *, preferred: str | None) -> int | None:
    if not options:
        return None
    placeholder_tokens = {
        "",
        "select an option",
        "choose an option",
        "select one",
        "choose one",
        "selecione uma opcao",
        "selecionar opcao",
        "selecione",
        "selecionar",
    }
    yes_tokens = {"yes", "y", "true", "sim", "s"}
    no_tokens = {"no", "n", "false", "nao"}

    def canonical_binary(value: str) -> str | None:
        if value in yes_tokens:
            return "yes"
        if value in no_tokens:
            return "no"
        return None

    candidate_indexes = [
        index
        for index, option in enumerate(options)
        if normalize_text(option) not in placeholder_tokens
    ]
    if preferred is None:
        return candidate_indexes[0] if candidate_indexes else 0
    normalized_preferred = normalize_text(preferred)
    canonical_preferred = canonical_binary(normalized_preferred)
    for index in candidate_indexes:
        option = options[index]
        normalized_option = normalize_text(option)
        if normalized_option == normalized_preferred:
            return index
        if (
            canonical_preferred is not None
            and canonical_binary(normalized_option) == canonical_preferred
        ):
            return index
    for index in candidate_indexes:
        option = options[index]
        normalized_option = normalize_text(option)
        if normalized_preferred in normalized_option or normalized_option in normalized_preferred:
            return index
    return candidate_indexes[0] if candidate_indexes else 0


def _pick_resume_option_index(options: tuple[str, ...], filename: str) -> int | None:
    if not options:
        return None
    for index, option in enumerate(options):
        if _resume_text_matches_requested_cv(option, filename):
            return index
    return None


def _pick_alternate_resume_option_index(options: tuple[str, ...], filename: str) -> int | None:
    if len(options) < 2:
        return None
    for index, option in enumerate(options):
        if not _resume_text_matches_requested_cv(option, filename):
            return index
    return None


def _resume_option_match_score(option_text: str, filename: str) -> int:
    normalized_option = normalize_text(option_text)
    normalized_filename = normalize_text(filename)
    filename_stem = normalize_text(Path(filename).stem)
    if not normalized_option:
        return 0
    score = 0
    if normalized_option == normalized_filename or normalized_option == filename_stem:
        score += 100
    if normalized_filename and normalized_filename in normalized_option:
        score += 60
    if filename_stem and filename_stem in normalized_option:
        score += 50
    option_tokens = set(re.findall(r"[a-z0-9]+", normalized_option))
    filename_tokens = set(re.findall(r"[a-z0-9]+", filename_stem or normalized_filename))
    shared_tokens = option_tokens & filename_tokens
    score += len(shared_tokens) * 10
    if "2026" in shared_tokens:
        score += 15
    return score


def _resume_text_matches_requested_cv(text: str, target_filename: str) -> bool:
    normalized_text = normalize_text(text)
    if not normalized_text:
        return False

    normalized_filename = normalize_text(target_filename)
    normalized_stem = normalize_text(Path(target_filename).stem)
    if normalized_filename and normalized_filename in normalized_text:
        return True
    if normalized_stem and normalized_stem in normalized_text:
        return True

    extracted_filenames = re.findall(
        r"[A-Za-z0-9][A-Za-z0-9._-]*\.(?:pdf|docx?|rtf)\b",
        text,
        flags=re.IGNORECASE,
    )
    for candidate in extracted_filenames:
        if normalize_text(candidate) == normalized_filename:
            return True
        if normalize_text(Path(candidate).stem) == normalized_stem:
            return True
    return False


async def _radio_option_is_checked(locator: Locator) -> bool:
    try:
        if await locator.is_checked():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        checked = await locator.evaluate(
            """
            (node) => {
              if (!(node instanceof Element)) {
                return false;
              }
              const input = node instanceof HTMLInputElement
                ? node
                : node.querySelector('input[type="radio"]');
              if (input instanceof HTMLInputElement && input.checked) {
                return true;
              }
              const roleRadio = node.matches('[role="radio"]')
                ? node
                : node.closest('[role="radio"]') || node.querySelector('[role="radio"]');
              return (
                roleRadio instanceof Element &&
                roleRadio.getAttribute('aria-checked') === 'true'
              );
            }
            """
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(checked)


async def _radio_option_input_id(locator: Locator) -> str | None:
    try:
        input_id = await locator.get_attribute("id")
    except Exception:  # noqa: BLE001
        input_id = None
    if input_id:
        return input_id
    try:
        nested_input_id = await locator.evaluate(
            """
            (node) => {
              if (!(node instanceof Element)) {
                return null;
              }
              const input = node instanceof HTMLInputElement
                ? node
                : node.querySelector('input[type="radio"]');
              return input instanceof HTMLInputElement && input.id ? input.id : null;
            }
            """
        )
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(nested_input_id, str) or not nested_input_id:
        return None
    return nested_input_id


async def _checkbox_option_is_checked(locator: Locator) -> bool:
    try:
        return await locator.is_checked()
    except Exception:  # noqa: BLE001
        pass
    try:
        payload = await locator.evaluate(
            """
            (node) => {
              if (!(node instanceof Element)) {
                return null;
              }
              const roleCheckbox = node.getAttribute("role") === "checkbox"
                ? node
                : node.closest('[role="checkbox"]') || node.querySelector('[role="checkbox"]');
              const ariaChecked = (
                roleCheckbox instanceof Element
                  ? roleCheckbox.getAttribute("aria-checked")
                  : ""
              ).toLowerCase();
              if (ariaChecked === "true") {
                return true;
              }
              if (ariaChecked === "false") {
                return false;
              }
              const checkbox = node instanceof HTMLInputElement
                ? node
                : node.querySelector('input[type="checkbox"]');
              if (checkbox instanceof HTMLInputElement) {
                return checkbox.checked;
              }
              return null;
            }
            """
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(payload)


async def _maybe_send_job_application_email_from_executor(
    executor: PlaywrightLinkedInEasyApplyExecutor,
    *,
    posting: JobPosting,
    settings: UserAgentSettings,
    submission_cv_path: Path | None,
    execution_id: UUID,
    submission_id: UUID,
    execution_events: list[ExecutionEvent],
) -> None:
    if not settings.agent.auto_send_job_email:
        return

    if not executor._runtime_settings.feature_job_email_enabled:  # noqa: SLF001
        executor._record_event(  # noqa: SLF001
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=ExecutionEventType.JOB_EMAIL_ATTEMPTED,
            payload={
                "job_posting_id": str(posting.id),
                "status": "skipped",
                "reason": "feature_flag_disabled",
            },
        )
        return

    email_target = detect_job_application_email_target(posting.description_raw)
    if email_target is None:
        executor._record_event(  # noqa: SLF001
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=ExecutionEventType.JOB_EMAIL_ATTEMPTED,
            payload={
                "job_posting_id": str(posting.id),
                "status": "skipped",
                "reason": "application_email_not_found",
            },
        )
        return

    resume_path = submission_cv_path or _existing_path(settings.profile.cv_path)
    if resume_path is None:
        executor._record_event(  # noqa: SLF001
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=ExecutionEventType.JOB_EMAIL_ATTEMPTED,
            payload={
                "job_posting_id": str(posting.id),
                "status": "skipped",
                "reason": "resume_attachment_missing",
                "recipient_email": email_target.recipient_email,
            },
        )
        return

    try:
        attempt = await asyncio.to_thread(
            executor._job_email_sender.send,  # noqa: SLF001
            posting=posting,
            settings=settings,
            recipient_email=email_target.recipient_email,
            resume_path=resume_path,
        )
    except Exception as exc:  # noqa: BLE001
        executor._record_exception_event(  # noqa: SLF001
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            stage="job_email",
            error=exc,
        )
        executor._record_event(  # noqa: SLF001
            execution_events,
            execution_id=execution_id,
            submission_id=submission_id,
            event_type=ExecutionEventType.JOB_EMAIL_ATTEMPTED,
            payload={
                "job_posting_id": str(posting.id),
                "status": "failed",
                "reason": "smtp_delivery_failed",
                "recipient_email": email_target.recipient_email,
                "notes": str(exc),
            },
        )
        logger.exception(
            "linkedin_job_email_failed",
            extra={
                "job_posting_id": str(posting.id),
                "submission_id": str(submission_id),
                "recipient_email": email_target.recipient_email,
            },
        )
        return

    executor._record_event(  # noqa: SLF001
        execution_events,
        execution_id=execution_id,
        submission_id=submission_id,
        event_type=ExecutionEventType.JOB_EMAIL_ATTEMPTED,
        payload={
            "job_posting_id": str(posting.id),
            "status": attempt.status,
            "reason": attempt.notes,
            "recipient_email": attempt.recipient_email,
            "subject": attempt.subject,
            "resume_filename": resume_path.name,
        },
    )


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
