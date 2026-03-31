"""Question extraction, classification, and answer resolution for Easy Apply."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from urllib import error, request

from job_applier.application.config import UserAgentSettings
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import AnswerSource, FillStrategy, QuestionType

logger = logging.getLogger(__name__)

ControlKind = Literal["text", "textarea", "select", "radio", "checkbox", "file"]

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "answer": {
            "type": ["string", "null"],
            "description": (
                "Chosen answer. Must be one of the available options when options exist."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Confidence score between 0 and 1.",
        },
        "reasoning": {
            "type": "string",
            "description": "Short explanation for audit logs.",
        },
    },
    "required": ["answer", "confidence", "reasoning"],
}


def normalize_text(value: str) -> str:
    """Collapse repeated whitespace and lowercase for comparisons."""

    return re.sub(r"\s+", " ", value).strip().lower()


def normalize_key(value: str) -> str:
    """Convert free-form labels into stable snake_case keys."""

    normalized = normalize_text(value)
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return slug or "unknown"


@dataclass(frozen=True, slots=True)
class QuestionClassification:
    """Normalized classification result for one extracted question."""

    question_type: QuestionType
    normalized_key: str
    confidence: float
    matched_rule: str | None = None


@dataclass(frozen=True, slots=True)
class EasyApplyField:
    """One normalized control discovered in the current Easy Apply step."""

    question_raw: str
    normalized_key: str
    question_type: QuestionType
    control_kind: ControlKind
    classification_confidence: float = 0.0
    classification_rule: str | None = None
    dom_ref: str | None = None
    dom_id: str | None = None
    name: str | None = None
    input_type: str | None = None
    required: bool = False
    prefilled: bool = False
    current_value: str = ""
    options: tuple[str, ...] = ()
    option_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedFieldValue:
    """Value selected for one field plus the audit metadata."""

    value: str
    answer_source: AnswerSource
    fill_strategy: FillStrategy
    ambiguity_flag: bool = False
    confidence: float | None = None
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    """Structured answer returned by the AI ambiguous-answer generator."""

    value: str
    confidence: float
    reasoning: str


class AmbiguousAnswerGenerator(Protocol):
    """Generate a best-effort answer when deterministic resolution is not possible."""

    async def generate(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> GeneratedAnswer | None:
        """Return a generated answer or `None` when the generator cannot help."""


class LinkedInQuestionClassifier:
    """Deterministic classifier for the minimum Easy Apply question types."""

    def classify(
        self,
        *,
        question_raw: str,
        control_kind: ControlKind,
        input_type: str | None,
        options: tuple[str, ...],
    ) -> QuestionClassification:
        """Infer the question type and confidence from the extracted control."""

        normalized = normalize_text(question_raw)
        default_key = normalize_key(question_raw)
        normalized_options = {normalize_text(option) for option in options if option.strip()}
        options_text = " ".join(option for option in normalized_options if option)

        if self._looks_like_resume_control(
            question_text=normalized,
            control_kind=control_kind,
            options_text=options_text,
        ):
            return QuestionClassification(
                question_type=QuestionType.RESUME_UPLOAD,
                normalized_key="resume_upload",
                confidence=0.99 if control_kind == "file" else 0.94,
                matched_rule="file_resume" if control_kind == "file" else "choice_resume",
            )
        if "cover letter" in normalized:
            return QuestionClassification(
                question_type=QuestionType.COVER_LETTER,
                normalized_key="cover_letter",
                confidence=0.99,
                matched_rule="cover_letter",
            )
        if input_type == "email" or self._contains_any(normalized, "email", "e-mail"):
            return QuestionClassification(
                question_type=QuestionType.EMAIL,
                normalized_key="email",
                confidence=0.99,
                matched_rule="email",
            )
        if input_type == "tel" or self._contains_any(normalized, "phone", "mobile", "telephone"):
            return QuestionClassification(
                question_type=QuestionType.PHONE,
                normalized_key="phone",
                confidence=0.99,
                matched_rule="phone",
            )
        if "linkedin" in normalized:
            return QuestionClassification(
                question_type=QuestionType.LINKEDIN_URL,
                normalized_key="linkedin_url",
                confidence=0.98,
                matched_rule="linkedin_url",
            )
        if "github" in normalized:
            return QuestionClassification(
                question_type=QuestionType.GITHUB_URL,
                normalized_key="github_url",
                confidence=0.98,
                matched_rule="github_url",
            )
        if self._contains_any(normalized, "portfolio", "personal site", "website", "site"):
            return QuestionClassification(
                question_type=QuestionType.PORTFOLIO_URL,
                normalized_key="portfolio_url",
                confidence=0.92,
                matched_rule="portfolio_url",
            )
        if self._contains_any(
            normalized,
            "authorized to work",
            "work authorization",
            "legally authorized",
            "eligible to work",
        ):
            return QuestionClassification(
                question_type=QuestionType.WORK_AUTHORIZATION,
                normalized_key="work_authorization",
                confidence=0.98,
                matched_rule="work_authorization",
            )
        if self._contains_any(normalized, "sponsorship", "visa", "require sponsor"):
            return QuestionClassification(
                question_type=QuestionType.VISA_SPONSORSHIP,
                normalized_key="visa_sponsorship",
                confidence=0.98,
                matched_rule="visa_sponsorship",
            )
        if self._contains_any(normalized, "salary", "compensation", "pay expectation"):
            return QuestionClassification(
                question_type=QuestionType.SALARY_EXPECTATION,
                normalized_key="salary_expectation",
                confidence=0.95,
                matched_rule="salary_expectation",
            )
        if self._contains_any(
            normalized,
            "start date",
            "availability",
            "notice period",
            "when can you start",
        ):
            return QuestionClassification(
                question_type=QuestionType.START_DATE,
                normalized_key="start_date",
                confidence=0.95,
                matched_rule="start_date",
            )
        if "city" in normalized or (
            "location" in normalized and control_kind in {"text", "textarea"}
        ):
            return QuestionClassification(
                question_type=QuestionType.CITY,
                normalized_key="city",
                confidence=0.9,
                matched_rule="city",
            )
        if "experience" in normalized and "year" in normalized:
            return QuestionClassification(
                question_type=QuestionType.YEARS_EXPERIENCE,
                normalized_key=default_key,
                confidence=0.9,
                matched_rule="years_experience",
            )
        if normalized_options and normalized_options.issubset({"yes", "no"}):
            return QuestionClassification(
                question_type=QuestionType.YES_NO_GENERIC,
                normalized_key=default_key,
                confidence=0.75,
                matched_rule="yes_no_generic",
            )
        if control_kind == "textarea":
            return QuestionClassification(
                question_type=QuestionType.FREE_TEXT_GENERIC,
                normalized_key=default_key,
                confidence=0.7,
                matched_rule="free_text",
            )
        return QuestionClassification(
            question_type=QuestionType.UNKNOWN,
            normalized_key=default_key,
            confidence=0.25,
            matched_rule=None,
        )

    def _contains_any(self, value: str, *terms: str) -> bool:
        return any(term in value for term in terms)

    def _looks_like_resume_control(
        self,
        *,
        question_text: str,
        control_kind: ControlKind,
        options_text: str,
    ) -> bool:
        resume_terms = ("resume", "cv", "curriculo", "curriculum")
        if self._contains_any(question_text, *resume_terms):
            return True
        if control_kind in {"radio", "checkbox", "select"} and self._contains_any(
            options_text,
            *resume_terms,
        ):
            return True
        return False


class LinkedInQuestionExtractor:
    """Normalize raw browser payloads into standardized question structures."""

    def __init__(self, classifier: LinkedInQuestionClassifier | None = None) -> None:
        self._classifier = classifier or LinkedInQuestionClassifier()

    def build_field(self, payload: dict[str, object]) -> EasyApplyField:
        """Build one standardized extracted field from browser payload data."""

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
            if control_kind in {"text", "textarea", "select", "radio", "checkbox", "file"}
            else "text"
        )
        typed_control = cast(ControlKind, control)
        raw_options = payload.get("options", ())
        option_items = raw_options if isinstance(raw_options, (list, tuple)) else ()
        options = tuple(
            item.strip() for item in option_items if isinstance(item, str) and item.strip()
        )
        input_type = str(payload.get("input_type")) if payload.get("input_type") else None
        classification = self._classifier.classify(
            question_raw=question_raw,
            control_kind=typed_control,
            input_type=input_type,
            options=options,
        )
        return EasyApplyField(
            question_raw=question_raw,
            normalized_key=classification.normalized_key,
            question_type=classification.question_type,
            control_kind=typed_control,
            classification_confidence=classification.confidence,
            classification_rule=classification.matched_rule,
            dom_ref=str(payload.get("dom_ref")) if payload.get("dom_ref") else None,
            dom_id=str(payload.get("dom_id")) if payload.get("dom_id") else None,
            name=str(payload.get("name")) if payload.get("name") else None,
            input_type=input_type,
            required=bool(payload.get("required")),
            prefilled=bool(payload.get("prefilled")),
            current_value=str(payload.get("current_value") or ""),
            options=options,
            option_refs=self._extract_option_refs(payload),
        )

    def _extract_option_refs(self, payload: dict[str, object]) -> tuple[str, ...]:
        raw_option_refs = payload.get("option_refs", ())
        if not isinstance(raw_option_refs, (list, tuple)):
            return ()
        return tuple(
            item.strip() for item in raw_option_refs if isinstance(item, str) and item.strip()
        )


class OpenAIResponsesAnswerGenerator:
    """Use the OpenAI Responses API for ambiguous best-effort autofill."""

    endpoint = "https://api.openai.com/v1/responses"

    async def generate(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> GeneratedAnswer | None:
        """Generate a structured answer using the user's configured model and key."""

        if settings.ai.api_key is None:
            return None
        if field.question_type not in {
            QuestionType.UNKNOWN,
            QuestionType.YES_NO_GENERIC,
            QuestionType.FREE_TEXT_GENERIC,
        }:
            return None

        prompt_payload = self._build_prompt_payload(field=field, settings=settings, posting=posting)
        logger.info(
            "linkedin_ai_autofill_prompt",
            extra={
                "normalized_key": field.normalized_key,
                "question_type": field.question_type.value,
                "model": settings.ai.model,
                "prompt_payload": prompt_payload,
            },
        )

        try:
            response_data = await asyncio.to_thread(
                self._create_response,
                api_key=settings.ai.api_key.get_secret_value(),
                model=settings.ai.model,
                prompt_payload=prompt_payload,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "linkedin_ai_autofill_failed",
                extra={
                    "normalized_key": field.normalized_key,
                    "question_type": field.question_type.value,
                    "model": settings.ai.model,
                },
            )
            return None

        raw_output = self._extract_output_text(response_data)
        logger.info(
            "linkedin_ai_autofill_response",
            extra={
                "normalized_key": field.normalized_key,
                "question_type": field.question_type.value,
                "model": settings.ai.model,
                "response_text": raw_output,
            },
        )

        if not raw_output:
            return None
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError:
            logger.warning(
                "linkedin_ai_autofill_invalid_json",
                extra={"normalized_key": field.normalized_key, "response_text": raw_output},
            )
            return None

        answer = str(payload.get("answer") or "").strip()
        if not answer:
            return None
        confidence = float(payload.get("confidence") or 0.0)
        reasoning = str(payload.get("reasoning") or "").strip()
        if field.options:
            selected_option = pick_option(field.options, preferred=answer)
            if selected_option is None:
                return None
            answer = selected_option

        return GeneratedAnswer(
            value=answer,
            confidence=confidence,
            reasoning=reasoning,
        )

    def _create_response(
        self,
        *,
        api_key: str,
        model: str,
        prompt_payload: dict[str, object],
    ) -> dict[str, object]:
        body = {
            "model": model,
            "input": [
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "You resolve ambiguous LinkedIn Easy Apply questions. "
                                "Return one best-effort answer only. "
                                "If options exist, answer with exactly one available option. "
                                "Keep free-text answers concise and plausible."
                            ),
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(prompt_payload, ensure_ascii=True),
                        },
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "autofill_answer",
                    "schema": STRUCTURED_OUTPUT_SCHEMA,
                    "strict": True,
                },
            },
        }
        payload_bytes = json.dumps(body, ensure_ascii=True).encode("utf-8")
        http_request = request.Request(
            self.endpoint,
            data=payload_bytes,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=30) as response:  # noqa: S310
                return cast(dict[str, object], json.loads(response.read().decode("utf-8")))
        except error.HTTPError as exc:
            error_text = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "openai_responses_http_error",
                extra={"status": exc.code, "body": error_text},
            )
            raise

    def _build_prompt_payload(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> dict[str, object]:
        profile_payload = {
            "name": settings.profile.name,
            "email": str(settings.profile.email),
            "phone": settings.profile.phone,
            "city": settings.profile.city,
            "linkedin_url": (
                str(settings.profile.linkedin_url) if settings.profile.linkedin_url else None
            ),
            "github_url": str(settings.profile.github_url) if settings.profile.github_url else None,
            "portfolio_url": (
                str(settings.profile.portfolio_url) if settings.profile.portfolio_url else None
            ),
            "years_experience_by_stack": settings.profile.years_experience_by_stack,
            "work_authorized": settings.profile.work_authorized,
            "needs_sponsorship": settings.profile.needs_sponsorship,
            "salary_expectation": settings.profile.salary_expectation,
            "availability": settings.profile.availability,
            "default_responses": settings.profile.default_responses,
        }
        return {
            "question": field.question_raw,
            "normalized_key": field.normalized_key,
            "question_type": field.question_type.value,
            "control_kind": field.control_kind,
            "input_type": field.input_type,
            "options": list(field.options),
            "current_value": field.current_value,
            "job": {
                "title": posting.title,
                "company_name": posting.company_name,
                "location": posting.location,
                "description_raw": posting.description_raw,
            },
            "candidate_profile": profile_payload,
        }

    def _extract_output_text(self, response_data: dict[str, object]) -> str:
        direct_output = response_data.get("output_text")
        if isinstance(direct_output, str):
            return direct_output.strip()

        output_items = response_data.get("output", ())
        if not isinstance(output_items, list):
            return ""

        parts: list[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            content_items = item.get("content", ())
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part.strip()).strip()


class LinkedInAnswerResolver:
    """Resolve extracted Easy Apply fields using the defined priority chain."""

    def __init__(
        self,
        *,
        ambiguous_answer_generator: AmbiguousAnswerGenerator | None = None,
    ) -> None:
        self._ambiguous_answer_generator = (
            ambiguous_answer_generator or OpenAIResponsesAnswerGenerator()
        )

    async def resolve(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
        *,
        posting: JobPosting,
    ) -> ResolvedFieldValue | None:
        """Return the selected value for a field, preserving prefilled controls."""

        if field.prefilled and field.current_value.strip():
            return None

        rule_value = self._resolve_explicit_rule_value(field, settings)
        if rule_value is not None:
            return ResolvedFieldValue(
                value=rule_value,
                answer_source=AnswerSource.RULE,
                fill_strategy=FillStrategy.DETERMINISTIC,
                confidence=1.0,
            )

        profile_value = self._resolve_profile_value(field, settings)
        if profile_value is not None:
            return ResolvedFieldValue(
                value=profile_value,
                answer_source=AnswerSource.PROFILE_SNAPSHOT,
                fill_strategy=FillStrategy.DETERMINISTIC,
                confidence=field.classification_confidence or 0.95,
            )

        default_value = self._lookup_default_response(field.normalized_key, settings)
        if default_value is not None:
            return ResolvedFieldValue(
                value=default_value,
                answer_source=AnswerSource.DEFAULT_RESPONSE,
                fill_strategy=FillStrategy.DETERMINISTIC,
                confidence=0.9,
            )

        if not settings.ruleset.allow_best_effort_autofill:
            return None

        ai_answer = await self._generate_ai_answer(field=field, settings=settings, posting=posting)
        if ai_answer is not None:
            return ResolvedFieldValue(
                value=ai_answer.value,
                answer_source=AnswerSource.AI,
                fill_strategy=FillStrategy.AUTOFILL_AI,
                ambiguity_flag=True,
                confidence=ai_answer.confidence,
                reasoning=ai_answer.reasoning,
            )

        best_effort = self._resolve_best_effort_fallback(field, settings)
        if best_effort is None:
            return None
        return ResolvedFieldValue(
            value=best_effort,
            answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
            fill_strategy=FillStrategy.BEST_EFFORT,
            ambiguity_flag=True,
            confidence=0.35,
            reasoning="heuristic_fallback",
        )

    async def _generate_ai_answer(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> GeneratedAnswer | None:
        try:
            return await self._ambiguous_answer_generator.generate(
                field=field,
                settings=settings,
                posting=posting,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "linkedin_ai_autofill_unhandled_error",
                extra={"normalized_key": field.normalized_key},
            )
            return None

    def _lookup_default_response(
        self,
        normalized_key: str,
        settings: UserAgentSettings,
    ) -> str | None:
        for key, value in settings.profile.default_responses.items():
            if normalize_key(key) == normalized_key and value.strip():
                return value.strip()
        return None

    def _resolve_explicit_rule_value(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> str | None:
        profile = settings.profile

        match field.question_type:
            case QuestionType.WORK_AUTHORIZATION:
                return "Yes" if profile.work_authorized else "No"
            case QuestionType.VISA_SPONSORSHIP:
                return "Yes" if profile.needs_sponsorship else "No"
            case _:
                return None

    def _resolve_profile_value(
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
            case QuestionType.SALARY_EXPECTATION:
                return (
                    str(profile.salary_expectation)
                    if profile.salary_expectation is not None
                    else None
                )
            case QuestionType.START_DATE:
                return profile.availability
            case QuestionType.RESUME_UPLOAD:
                return profile.cv_path
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

    def _resolve_best_effort_fallback(
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
            return pick_option(field.options, preferred=preferred)

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
            return str(profile.linkedin_url) if profile.linkedin_url else None
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


def pick_option(options: tuple[str, ...], *, preferred: str | None = None) -> str | None:
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
