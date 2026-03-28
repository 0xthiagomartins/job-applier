"""LinkedIn Easy Apply automation and submission persistence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from playwright.async_api import BrowserContext, Locator, Page, async_playwright

from job_applier.application.agent_execution import JobSubmitter, SubmissionAttempt
from job_applier.application.config import UserAgentSettings
from job_applier.application.repositories import (
    AnswerRepository,
    ArtifactSnapshotRepository,
    ProfileSnapshotRepository,
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
    JobPosting,
    utc_now,
)
from job_applier.domain.enums import (
    AnswerSource,
    ArtifactType,
    ExecutionOrigin,
    FillStrategy,
    QuestionType,
    SubmissionStatus,
)
from job_applier.infrastructure.linkedin.auth import (
    LinkedInAuthError,
    LinkedInCredentials,
    LinkedInSessionManager,
)
from job_applier.settings import RuntimeSettings

logger = logging.getLogger(__name__)

ControlKind = Literal["text", "textarea", "select", "radio", "checkbox", "file"]
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
class EasyApplyField:
    """One normalized control discovered in the current Easy Apply step."""

    question_raw: str
    normalized_key: str
    question_type: QuestionType
    control_kind: ControlKind
    dom_id: str | None = None
    name: str | None = None
    input_type: str | None = None
    required: bool = False
    prefilled: bool = False
    current_value: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EasyApplyStep:
    """Current Easy Apply step metadata and discovered controls."""

    step_index: int
    total_steps: int
    fields: tuple[EasyApplyField, ...]


@dataclass(frozen=True, slots=True)
class ResolvedFieldValue:
    """Value selected for one field plus the audit metadata."""

    value: str
    answer_source: AnswerSource
    fill_strategy: FillStrategy
    ambiguity_flag: bool = False


@dataclass(frozen=True, slots=True)
class EasyApplyExecutionResult:
    """Structured result produced by the Playwright Easy Apply executor."""

    submission_id: UUID
    started_at: datetime
    status: SubmissionStatus
    notes: str | None = None
    answers: tuple[ApplicationAnswer, ...] = ()
    artifacts: tuple[ArtifactSnapshot, ...] = ()
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
        origin: ExecutionOrigin,
    ) -> EasyApplyExecutionResult:
        """Run the Easy Apply flow for one posting."""


def normalize_text(value: str) -> str:
    """Collapse repeated whitespace and lowercase for comparisons."""

    return re.sub(r"\s+", " ", value).strip().lower()


def normalize_key(value: str) -> str:
    """Convert free-form labels into stable snake_case keys."""

    normalized = normalize_text(value)
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return slug or "unknown"


def classify_question(
    question_raw: str,
    *,
    control_kind: ControlKind,
    input_type: str | None,
    options: tuple[str, ...],
) -> tuple[QuestionType, str]:
    """Infer the question type and normalized key from the raw label."""

    normalized = normalize_text(question_raw)

    if control_kind == "file" and any(term in normalized for term in ("resume", "cv")):
        return QuestionType.RESUME_UPLOAD, "resume_upload"
    if "cover letter" in normalized:
        return QuestionType.COVER_LETTER, "cover_letter"
    if input_type == "email" or "email" in normalized or "e-mail" in normalized:
        return QuestionType.EMAIL, "email"
    if input_type == "tel" or "phone" in normalized or "mobile" in normalized:
        return QuestionType.PHONE, "phone"
    if "linkedin" in normalized:
        return QuestionType.LINKEDIN_URL, "linkedin_url"
    if "github" in normalized:
        return QuestionType.GITHUB_URL, "github_url"
    if any(term in normalized for term in ("portfolio", "personal site", "website", "site")):
        return QuestionType.PORTFOLIO_URL, "portfolio_url"
    if any(
        term in normalized
        for term in (
            "authorized to work",
            "work authorization",
            "legally authorized",
            "eligible to work",
        )
    ):
        return QuestionType.WORK_AUTHORIZATION, "work_authorization"
    if any(term in normalized for term in ("sponsorship", "visa", "require sponsor")):
        return QuestionType.VISA_SPONSORSHIP, "visa_sponsorship"
    if any(term in normalized for term in ("salary", "compensation", "pay expectation")):
        return QuestionType.SALARY_EXPECTATION, "salary_expectation"
    if any(
        term in normalized
        for term in ("start date", "availability", "notice period", "when can you start")
    ):
        return QuestionType.START_DATE, "start_date"
    if "city" in normalized or ("location" in normalized and control_kind in {"text", "textarea"}):
        return QuestionType.CITY, "city"
    if "experience" in normalized and "year" in normalized:
        return QuestionType.YEARS_EXPERIENCE, normalize_key(question_raw)
    yes_no_options = {normalize_text(option) for option in options if option.strip()}
    if yes_no_options and yes_no_options.issubset({"yes", "no"}):
        return QuestionType.YES_NO_GENERIC, normalize_key(question_raw)
    if control_kind == "textarea":
        return QuestionType.FREE_TEXT_GENERIC, normalize_key(question_raw)
    return QuestionType.UNKNOWN, normalize_key(question_raw)


def _build_field(payload: dict[str, object]) -> EasyApplyField:
    question_raw = str(
        payload.get("question_raw")
        or payload.get("name")
        or payload.get("dom_id")
        or payload.get("input_type")
        or "unknown question"
    )
    control_kind = str(payload.get("control_kind") or "text")
    control = (
        control_kind
        if control_kind
        in {
            "text",
            "textarea",
            "select",
            "radio",
            "checkbox",
            "file",
        }
        else "text"
    )
    typed_control = cast(ControlKind, control)
    raw_options = payload.get("options", ())
    option_items = raw_options if isinstance(raw_options, (list, tuple)) else ()
    options = tuple(item.strip() for item in option_items if isinstance(item, str) and item.strip())
    input_type = str(payload.get("input_type")) if payload.get("input_type") else None
    question_type, normalized_key = classify_question(
        question_raw,
        control_kind=typed_control,
        input_type=input_type,
        options=options,
    )
    return EasyApplyField(
        question_raw=question_raw,
        normalized_key=normalized_key,
        question_type=question_type,
        control_kind=typed_control,
        dom_id=str(payload.get("dom_id")) if payload.get("dom_id") else None,
        name=str(payload.get("name")) if payload.get("name") else None,
        input_type=input_type,
        required=bool(payload.get("required")),
        prefilled=bool(payload.get("prefilled")),
        current_value=str(payload.get("current_value") or ""),
        options=options,
    )


class LinkedInAnswerResolver:
    """Resolve known Easy Apply fields against the user profile and defaults."""

    def resolve(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> ResolvedFieldValue | None:
        """Return the selected value for a field, preserving prefilled controls."""

        if field.prefilled and field.current_value.strip():
            return None

        default_value = self._lookup_default_response(field.normalized_key, settings)
        if default_value is not None:
            return ResolvedFieldValue(
                value=default_value,
                answer_source=AnswerSource.DEFAULT_RESPONSE,
                fill_strategy=FillStrategy.DETERMINISTIC,
            )

        direct_value = self._resolve_direct_value(field, settings)
        if direct_value is not None:
            return ResolvedFieldValue(
                value=direct_value,
                answer_source=AnswerSource.PROFILE_SNAPSHOT,
                fill_strategy=FillStrategy.DETERMINISTIC,
            )

        if not settings.ruleset.allow_best_effort_autofill:
            return None

        best_effort = self._resolve_best_effort(field, settings)
        if best_effort is None:
            return None
        return ResolvedFieldValue(
            value=best_effort,
            answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
            fill_strategy=FillStrategy.BEST_EFFORT,
            ambiguity_flag=True,
        )

    def _lookup_default_response(
        self,
        normalized_key: str,
        settings: UserAgentSettings,
    ) -> str | None:
        for key, value in settings.profile.default_responses.items():
            if normalize_key(key) == normalized_key and value.strip():
                return value.strip()
        return None

    def _resolve_direct_value(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> str | None:
        profile = settings.profile

        match field.question_type:
            case QuestionType.EMAIL:
                return str(profile.email)
            case QuestionType.PHONE:
                return profile.phone
            case QuestionType.CITY:
                return profile.city
            case QuestionType.LINKEDIN_URL:
                return str(profile.linkedin_url) if profile.linkedin_url else None
            case QuestionType.GITHUB_URL:
                return str(profile.github_url) if profile.github_url else None
            case QuestionType.PORTFOLIO_URL:
                return str(profile.portfolio_url) if profile.portfolio_url else None
            case QuestionType.WORK_AUTHORIZATION:
                return "Yes" if profile.work_authorized else "No"
            case QuestionType.VISA_SPONSORSHIP:
                return "Yes" if profile.needs_sponsorship else "No"
            case QuestionType.SALARY_EXPECTATION:
                if profile.salary_expectation is None:
                    return None
                return str(profile.salary_expectation)
            case QuestionType.START_DATE:
                return profile.availability
            case QuestionType.RESUME_UPLOAD:
                return profile.cv_path
            case QuestionType.COVER_LETTER:
                return self._lookup_default_response("cover_letter", settings)
            case QuestionType.YEARS_EXPERIENCE:
                return self._resolve_years_experience(field, settings)
            case _:
                return None

    def _resolve_years_experience(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> str | None:
        normalized_question = normalize_text(f"{field.question_raw} {field.normalized_key}")
        for stack_name, years in settings.profile.years_experience_by_stack.items():
            if normalize_key(stack_name) in normalized_question:
                return str(years)

        years_values = tuple(settings.profile.years_experience_by_stack.values())
        if years_values:
            return str(max(years_values))
        return None

    def _resolve_best_effort(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> str | None:
        profile = settings.profile
        normalized_question = normalize_text(field.question_raw)

        if field.question_type is QuestionType.RESUME_UPLOAD:
            return profile.cv_path

        if field.options:
            preferred = "No" if "follow" in normalized_question else None
            return _pick_option(field.options, preferred=preferred)

        if field.control_kind == "checkbox":
            return "No" if "follow" in normalized_question else "Yes"
        if field.control_kind == "file":
            return profile.cv_path
        if field.control_kind == "textarea":
            return self._first_default_response(settings) or "Open to discuss."
        if field.input_type == "email":
            return str(profile.email)
        if field.input_type == "tel":
            return profile.phone
        if field.input_type == "url":
            return str(profile.linkedin_url)
        if field.input_type == "number":
            if profile.salary_expectation is not None:
                return str(profile.salary_expectation)
            years_values = tuple(profile.years_experience_by_stack.values())
            return str(max(years_values)) if years_values else "0"
        return self._first_default_response(settings) or profile.availability or profile.city

    def _first_default_response(self, settings: UserAgentSettings) -> str | None:
        for value in settings.profile.default_responses.values():
            if value.strip():
                return value.strip()
        return None


class PlaywrightLinkedInEasyApplyExecutor:
    """Use Playwright to run the LinkedIn Easy Apply modal end to end."""

    def __init__(
        self,
        runtime_settings: RuntimeSettings,
        *,
        answer_resolver: LinkedInAnswerResolver | None = None,
    ) -> None:
        self._runtime_settings = runtime_settings
        self._answer_resolver = answer_resolver or LinkedInAnswerResolver()
        self._session_manager: LinkedInSessionManager | None = None

    async def execute(
        self,
        settings: UserAgentSettings,
        posting: JobPosting,
        *,
        origin: ExecutionOrigin,
    ) -> EasyApplyExecutionResult:
        submission_id = uuid4()
        started_at = utc_now()
        run_dir = self._build_run_dir(posting, submission_id)
        answers: list[ApplicationAnswer] = []
        artifacts: list[ArtifactSnapshot] = []
        uploaded_cv_paths: set[str] = set()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self._runtime_settings.playwright_headless,
            )
            try:
                context = await self._get_session_manager().create_authenticated_context(browser)
                try:
                    return await self._execute_once(
                        context,
                        settings,
                        posting,
                        origin=origin,
                        submission_id=submission_id,
                        started_at=started_at,
                        run_dir=run_dir,
                        answers=answers,
                        artifacts=artifacts,
                        uploaded_cv_paths=uploaded_cv_paths,
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
        origin: ExecutionOrigin,
        submission_id: UUID,
        started_at: datetime,
        run_dir: Path,
        answers: list[ApplicationAnswer],
        artifacts: list[ArtifactSnapshot],
        uploaded_cv_paths: set[str],
    ) -> EasyApplyExecutionResult:
        page = await context.new_page()
        page.set_default_timeout(self._runtime_settings.linkedin_default_timeout_ms)

        try:
            await page.goto(posting.url, wait_until="domcontentloaded")
            await self._ensure_authenticated_page(page)
            artifacts.append(
                await self._capture_screenshot(
                    page,
                    run_dir / "job-opened.png",
                    submission_id=submission_id,
                ),
            )

            easy_apply_button = await self._find_easy_apply_button(page)
            if easy_apply_button is None:
                notes = "Easy Apply button not available for this posting."
                logger.info(
                    "linkedin_easy_apply_skipped",
                    extra={"job_posting_id": str(posting.id), "origin": origin.value},
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
                logger.info(
                    "linkedin_easy_apply_step",
                    extra={
                        "job_posting_id": str(posting.id),
                        "step_index": step.step_index,
                        "total_steps": step.total_steps,
                        "field_count": len(step.fields),
                    },
                )
                artifacts.append(
                    await self._capture_screenshot(
                        page,
                        run_dir / f"step-{step.step_index + 1}.png",
                        submission_id=submission_id,
                    ),
                )
                step_answers, step_artifacts = await self._fill_step_fields(
                    page,
                    step,
                    settings,
                    submission_id=submission_id,
                    uploaded_cv_paths=uploaded_cv_paths,
                )
                answers.extend(step_answers)
                artifacts.extend(step_artifacts)

                action = await self._find_primary_action(page)
                if action is None:
                    errors = await self._collect_validation_errors(page)
                    notes = _join_errors(errors) or "No LinkedIn step action was available."
                    return EasyApplyExecutionResult(
                        submission_id=submission_id,
                        started_at=started_at,
                        status=SubmissionStatus.FAILED,
                        notes=notes,
                    )

                action_kind, action_locator = action
                if action_kind == "submit":
                    await self._prepare_submit_step(page)
                    await action_locator.click()
                    success, outcome_notes = await self._await_submission_outcome(page)
                    artifacts.append(
                        await self._capture_screenshot(
                            page,
                            run_dir / "post-submit.png",
                            submission_id=submission_id,
                        ),
                    )
                    if success:
                        return EasyApplyExecutionResult(
                            submission_id=submission_id,
                            started_at=started_at,
                            status=SubmissionStatus.SUBMITTED,
                            notes=outcome_notes or "LinkedIn Easy Apply submitted successfully.",
                            answers=tuple(answers),
                            artifacts=tuple(artifacts),
                            submitted_at=utc_now(),
                            cv_version=settings.profile.cv_filename,
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
                    return EasyApplyExecutionResult(
                        submission_id=submission_id,
                        started_at=started_at,
                        status=SubmissionStatus.FAILED,
                        notes=_join_errors(errors),
                    )

            return EasyApplyExecutionResult(
                submission_id=submission_id,
                started_at=started_at,
                status=SubmissionStatus.FAILED,
                notes="LinkedIn Easy Apply exceeded the maximum number of steps.",
            )
        finally:
            await page.close()

    async def _fill_step_fields(
        self,
        page: Page,
        step: EasyApplyStep,
        settings: UserAgentSettings,
        *,
        submission_id: UUID,
        uploaded_cv_paths: set[str],
    ) -> tuple[list[ApplicationAnswer], list[ArtifactSnapshot]]:
        root = await self._easy_apply_root(page)
        answers: list[ApplicationAnswer] = []
        artifacts: list[ArtifactSnapshot] = []

        for field in step.fields:
            resolution = self._answer_resolver.resolve(field, settings)
            if resolution is None:
                continue

            applied_value = await self._apply_field_value(root, field, resolution, settings)
            if applied_value is None:
                continue

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
                option = _pick_option(field.options, preferred=resolution.value)
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
                option = _pick_option(field.options, preferred=resolution.value)
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
        fields = tuple(_build_field(item) for item in raw_fields if isinstance(item, dict))
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


class LinkedInEasyApplySubmitter(JobSubmitter):
    """Persist successful LinkedIn Easy Apply runs into the SQLite audit model."""

    def __init__(
        self,
        *,
        executor: EasyApplyExecutor,
        submission_repository: SubmissionRepository,
        answer_repository: AnswerRepository,
        profile_snapshot_repository: ProfileSnapshotRepository,
        artifact_repository: ArtifactSnapshotRepository,
    ) -> None:
        self._executor = executor
        self._submission_repository = submission_repository
        self._answer_repository = answer_repository
        self._profile_snapshot_repository = profile_snapshot_repository
        self._artifact_repository = artifact_repository

    async def submit(
        self,
        settings: UserAgentSettings,
        posting: JobPosting,
        *,
        origin: ExecutionOrigin,
    ) -> SubmissionAttempt:
        result = await self._executor.execute(settings, posting, origin=origin)

        if result.status is SubmissionStatus.SUBMITTED:
            record = self._persist_successful_submission(result, posting, settings, origin)
            return SubmissionAttempt(
                submission=record.submission,
                successful_record=record,
            )

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
        for artifact in result.artifacts:
            self._artifact_repository.save(artifact)
        return record


def _attribute_selector(attribute: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{attribute}="{escaped}"]'


def _pick_option(options: tuple[str, ...], *, preferred: str | None = None) -> str | None:
    if not options:
        return None

    if preferred is not None:
        normalized_preferred = normalize_text(preferred)
        for option in options:
            normalized_option = normalize_text(option)
            if normalized_option == normalized_preferred:
                return option
        for option in options:
            normalized_option = normalize_text(option)
            if (
                normalized_preferred in normalized_option
                or normalized_option in normalized_preferred
            ):
                return option

    for option in options:
        if normalize_text(option) not in {"", "select an option", "choose an option"}:
            return option
    return options[0]


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
