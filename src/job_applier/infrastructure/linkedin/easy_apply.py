"""LinkedIn Easy Apply automation and submission persistence."""

from __future__ import annotations

import json
import logging
import re
import traceback
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol
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
from job_applier.infrastructure.linkedin.question_resolution import (
    EasyApplyField,
    LinkedInAnswerResolver,
    LinkedInQuestionExtractor,
    ResolvedFieldValue,
    normalize_text,
    pick_option,
)
from job_applier.infrastructure.linkedin.recruiter_connect import (
    LinkedInRecruiterCandidateFinder,
    PlaywrightRecruiterConnector,
)
from job_applier.observability import bind_submission_context
from job_applier.settings import RuntimeSettings

logger = logging.getLogger(__name__)

ActionKind = Literal["next", "review", "submit"]

SUCCESS_PATTERNS = (
    r"your application was sent",
    r"application submitted",
    r"application sent",
    r"you.re all set",
)


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
                context = await self._get_session_manager().create_authenticated_context(browser)
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

                easy_apply_button = await self._find_easy_apply_button(page)
                if easy_apply_button is None:
                    notes = "Easy Apply button not available for this posting."
                    logger.info(
                        "linkedin_easy_apply_skipped",
                        extra={"job_posting_id": str(posting.id), "origin": origin.value},
                    )
                    self._record_event(
                        execution_events,
                        execution_id=execution_id,
                        submission_id=submission_id,
                        event_type=ExecutionEventType.JOB_PROCESSED,
                        payload={
                            "job_posting_id": str(posting.id),
                            "origin": origin.value,
                            "reason": "easy_apply_unavailable",
                            "status": SubmissionStatus.SKIPPED.value,
                        },
                    )
                    return EasyApplyExecutionResult(
                        submission_id=submission_id,
                        started_at=started_at,
                        status=SubmissionStatus.SKIPPED,
                        notes=notes,
                    )

                await easy_apply_button.click()
                await self._wait_for_easy_apply_modal(page)

                max_steps = 10
                for fallback_step_index in range(max_steps):
                    step = await self._extract_step(page, fallback_step_index=fallback_step_index)
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

                    action = await self._find_primary_action(page)
                    if action is None:
                        errors = await self._collect_validation_errors(page)
                        notes = _join_errors(errors) or "No LinkedIn step action was available."
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
                                "reason": "missing_primary_action",
                                "status": SubmissionStatus.FAILED.value,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.FAILED,
                            notes=notes,
                        )

                    action_kind, action_locator = action
                    if action_kind == "submit":
                        await self._prepare_submit_step(page)
                        self._record_event(
                            execution_events,
                            execution_id=execution_id,
                            submission_id=submission_id,
                            event_type=ExecutionEventType.SUBMIT_TRIGGERED,
                            payload={
                                "job_posting_id": str(posting.id),
                                "step_index": step.step_index,
                            },
                        )
                        await action_locator.click()
                        success, outcome_notes = await self._await_submission_outcome(page)
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

                    await action_locator.click()
                    await page.wait_for_timeout(700)

                    errors = await self._collect_validation_errors(page)
                    if errors:
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
                                "reason": "validation_error",
                                "status": SubmissionStatus.FAILED.value,
                            },
                        )
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.FAILED,
                            notes=_join_errors(errors),
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

            applied_value = await self._apply_field_value(root, field, resolution, settings)
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
                await locator.fill(resolution.value)
                return resolution.value
            case "select":
                locator = await self._find_control_locator(root, field)
                if locator is None:
                    return None
                option = pick_option(field.options, preferred=resolution.value)
                if option is None:
                    return None
                await locator.select_option(label=option)
                return option
            case "checkbox":
                locator = await self._find_control_locator(root, field)
                if locator is None:
                    return None
                should_check = normalize_text(resolution.value) in {"yes", "true", "1"}
                if should_check:
                    await locator.check()
                    return "Yes"
                await locator.uncheck()
                return "No"
            case "radio":
                option = pick_option(field.options, preferred=resolution.value)
                if option is None:
                    return None
                if await self._check_radio_option(root, field, option):
                    return option
                return None
            case "file":
                locator = await self._find_control_locator(root, field)
                if locator is None or settings.profile.cv_path is None:
                    return None
                await locator.set_input_files(settings.profile.cv_path)
                return settings.profile.cv_filename or Path(settings.profile.cv_path).name

    async def _check_radio_option(
        self,
        root: Locator,
        field: EasyApplyField,
        option: str,
    ) -> bool:
        option_pattern = re.compile(re.escape(option), re.I)
        for locator in (
            root.get_by_role("radio", name=option_pattern),
            root.get_by_label(option_pattern),
        ):
            if await locator.count():
                await locator.first.check()
                return True

        if field.name:
            group = root.locator(
                f'input[type="radio"]{_attribute_selector("name", field.name)}',
            )
            option_index = field.options.index(option)
            if await group.count() > option_index:
                await group.nth(option_index).check()
                return True
        return False

    async def _find_control_locator(self, root: Locator, field: EasyApplyField) -> Locator | None:
        if field.dom_id:
            locator = root.locator(_attribute_selector("id", field.dom_id))
            if await locator.count():
                return locator.first
        if field.name:
            locator = root.locator(_attribute_selector("name", field.name))
            if await locator.count():
                return locator.first
        label_pattern = re.compile(re.escape(field.question_raw[:60]), re.I)
        locator = root.get_by_label(label_pattern)
        if await locator.count():
            return locator.first
        return None

    async def _extract_step(self, page: Page, *, fallback_step_index: int) -> EasyApplyStep:
        root = await self._easy_apply_root(page)
        payload = await root.evaluate(
            """
            (node) => {
              const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const labels = Array.from(node.querySelectorAll("label"));

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
                "input:not([type=radio]):not([type=checkbox])",
                ":not([type=hidden]):not([disabled])",
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

                fields.push({
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
                fields.push({
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
                });
              }

              const checkboxes = node.querySelectorAll("input[type=checkbox]:not([disabled])");
              for (const input of checkboxes) {
                fields.push({
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
            step_index = fallback_step_index
        if isinstance(total_steps, int) and total_steps >= 1:
            total = int(total_steps)
        else:
            total = max(step_index + 1, 1)
        raw_fields = payload.get("fields", [])
        fields = tuple(
            self._question_extractor.build_field(item)
            for item in raw_fields
            if isinstance(item, dict)
        )
        return EasyApplyStep(step_index=step_index, total_steps=total, fields=fields)

    async def _find_primary_action(self, page: Page) -> tuple[ActionKind, Locator] | None:
        root = await self._easy_apply_root(page)
        candidates: tuple[tuple[ActionKind, Locator], ...] = (
            (
                "submit",
                root.get_by_role(
                    "button",
                    name=re.compile(r"submit application|send application", re.I),
                ),
            ),
            ("review", root.get_by_role("button", name=re.compile(r"review", re.I))),
            ("next", root.get_by_role("button", name=re.compile(r"next", re.I))),
        )
        for kind, locator in candidates:
            if await locator.count():
                return kind, locator.first
        return None

    async def _prepare_submit_step(self, page: Page) -> None:
        root = await self._easy_apply_root(page)
        for pattern in (r"follow", r"job alert", r"stay up to date"):
            checkbox = root.get_by_label(re.compile(pattern, re.I))
            if await checkbox.count():
                try:
                    await checkbox.first.uncheck()
                except Exception:  # noqa: BLE001
                    logger.debug("linkedin_easy_apply_optional_checkbox_skip", exc_info=True)

    async def _await_submission_outcome(self, page: Page) -> tuple[bool, str | None]:
        for _ in range(20):
            if await self._submission_success_detected(page):
                return True, "LinkedIn Easy Apply submitted successfully."
            errors = await self._collect_validation_errors(page)
            if errors:
                return False, _join_errors(errors)
            await page.wait_for_timeout(500)
        return False, "LinkedIn did not confirm the submission result in time."

    async def _submission_success_detected(self, page: Page) -> bool:
        for pattern in SUCCESS_PATTERNS:
            locator = page.get_by_text(re.compile(pattern, re.I))
            if await locator.count():
                return True

        done_button = page.get_by_role("button", name=re.compile(r"done|close|dismiss", re.I))
        submit_button = page.get_by_role(
            "button",
            name=re.compile(r"submit application|send application", re.I),
        )
        return await done_button.count() > 0 and await submit_button.count() == 0

    async def _collect_validation_errors(self, page: Page) -> tuple[str, ...]:
        try:
            root = await self._easy_apply_root(page)
        except LinkedInEasyApplyError:
            return ()

        messages: list[str] = []
        for selector in (
            ".artdeco-inline-feedback__message",
            ".fb-form-element__error",
            "[role='alert']",
            "[aria-live='assertive']",
        ):
            locator = root.locator(selector)
            count = await locator.count()
            for index in range(min(count, 8)):
                text = normalize_text(await locator.nth(index).inner_text())
                if text:
                    messages.append(text)
        unique = tuple(dict.fromkeys(messages))
        return unique

    async def _find_easy_apply_button(self, page: Page) -> Locator | None:
        button = page.get_by_role("button", name=re.compile(r"easy apply", re.I))
        if await button.count():
            return button.first
        return None

    async def _wait_for_easy_apply_modal(self, page: Page) -> None:
        for _ in range(20):
            try:
                await self._easy_apply_root(page)
                return
            except LinkedInEasyApplyError:
                await page.wait_for_timeout(250)
        msg = "LinkedIn Easy Apply modal did not open."
        raise LinkedInEasyApplyError(msg)

    async def _easy_apply_root(self, page: Page) -> Locator:
        for selector in (
            ".jobs-easy-apply-modal",
            "[data-test-modal] [role='dialog']",
            "[role='dialog']",
        ):
            locator = page.locator(selector)
            if await locator.count():
                return locator.first
        msg = "LinkedIn Easy Apply dialog is not visible."
        raise LinkedInEasyApplyError(msg)

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

    def _get_session_manager(self) -> LinkedInSessionManager:
        if self._session_manager is None:
            self._session_manager = LinkedInSessionManager(
                credentials=self._credentials_from_settings(self._runtime_settings),
                storage_state_path=self._runtime_settings.resolved_linkedin_storage_state_path,
                login_timeout_seconds=self._runtime_settings.linkedin_login_timeout_seconds,
            )
        return self._session_manager

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


def _join_errors(errors: tuple[str, ...]) -> str:
    return "; ".join(error for error in errors if error)


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
