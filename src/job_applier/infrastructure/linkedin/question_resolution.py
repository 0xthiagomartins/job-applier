"""Question extraction, classification, and answer resolution for Easy Apply."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Literal, Protocol, cast
from urllib import error, request

from job_applier.application.config import UserAgentSettings
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import AnswerSource, FillStrategy, QuestionType

logger = logging.getLogger(__name__)

ControlKind = Literal["text", "textarea", "select", "radio", "checkbox", "file"]

_PLACEHOLDER_OPTION_TOKENS = frozenset(
    {
        "",
        "select an option",
        "choose an option",
        "select one",
        "choose one",
        "select",
        "choose",
        "selecione uma opcao",
        "selecionar opcao",
        "selecionar uma opcao",
        "selecione",
        "selecionar",
        "escolha uma opcao",
        "escolher uma opcao",
    }
)
_YES_OPTION_TOKENS = frozenset({"yes", "y", "true", "sim", "s"})
_NO_OPTION_TOKENS = frozenset({"no", "n", "false", "nao"})
_SENSITIVE_OPT_OUT_TOKENS = frozenset(
    {
        "prefer not to say",
        "prefer not to answer",
        "prefer not to disclose",
        "decline to answer",
        "decline to self identify",
        "choose not to disclose",
        "do not wish to answer",
        "rather not say",
        "not specified",
        "undisclosed",
        "prefiro nao informar",
        "prefiro nao responder",
        "prefiro nao dizer",
        "nao desejo informar",
        "nao desejo responder",
        "nao quero informar",
        "nao informado",
        "prefiro nao me identificar",
    }
)
_NUMERIC_VALIDATION_TOKENS = frozenset(
    {
        "number",
        "numeric",
        "decimal",
        "integer",
        "float",
        "whole number",
        "plain number",
        "plain integer",
        "numero",
        "numérico",
        "numerico",
        "inteiro",
    }
)

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

    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", ascii_only).strip().lower()


def normalize_key(value: str) -> str:
    """Convert free-form labels into stable snake_case keys."""

    normalized = normalize_text(value)
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return slug or "unknown"


def _non_empty_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _canonical_binary_token(value: str) -> str | None:
    normalized = normalize_text(value)
    if normalized in _YES_OPTION_TOKENS:
        return "yes"
    if normalized in _NO_OPTION_TOKENS:
        return "no"
    return None


def field_has_meaningful_current_value(field: EasyApplyField) -> bool:
    current_value = _non_empty_value(field.current_value)
    if current_value is None:
        return False
    return normalize_text(current_value) not in _PLACEHOLDER_OPTION_TOKENS


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


@dataclass(frozen=True, slots=True)
class GuardrailAnswer:
    """Conservative local fallback answer used when the AI path cannot help."""

    value: str
    confidence: float
    reasoning: str


@dataclass(frozen=True, slots=True)
class ValidationFeedbackContext:
    """Structured context describing why the previous field attempt was rejected."""

    validation_message: str | None = None
    current_value: str = ""
    previous_answer: str | None = None


class AmbiguousAnswerGenerator(Protocol):
    """Generate a best-effort answer when deterministic resolution is not possible."""

    async def generate(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
        validation_context: ValidationFeedbackContext | None = None,
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
        meaningful_options = {
            option for option in normalized_options if option not in _PLACEHOLDER_OPTION_TOKENS
        }
        options_text = " ".join(option for option in meaningful_options if option)

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
        if self._contains_any(normalized, "cover letter", "carta de apresentacao"):
            return QuestionClassification(
                question_type=QuestionType.COVER_LETTER,
                normalized_key="cover_letter",
                confidence=0.99,
                matched_rule="cover_letter",
            )
        if self._contains_any(normalized, "first name", "given name", "forename"):
            return QuestionClassification(
                question_type=QuestionType.FIRST_NAME,
                normalized_key="first_name",
                confidence=0.98,
                matched_rule="first_name",
            )
        if self._contains_any(normalized, "last name", "family name", "surname"):
            return QuestionClassification(
                question_type=QuestionType.LAST_NAME,
                normalized_key="last_name",
                confidence=0.98,
                matched_rule="last_name",
            )
        if input_type == "email" or self._contains_any(
            normalized,
            "email",
            "e-mail",
            "endereco de email",
            "endereco de e mail",
            "correio eletronico",
        ):
            return QuestionClassification(
                question_type=QuestionType.EMAIL,
                normalized_key="email",
                confidence=0.99,
                matched_rule="email",
            )
        if self._contains_any(
            normalized,
            "country code",
            "codigo do pais",
            "codigo de pais",
            "codigo do telefone",
            "ddd",
        ):
            return QuestionClassification(
                question_type=QuestionType.PHONE,
                normalized_key="phone_country_code",
                confidence=0.96,
                matched_rule="phone_country_code",
            )
        if input_type == "tel" or self._contains_any(
            normalized,
            "phone",
            "mobile",
            "telephone",
            "telefone",
            "celular",
            "numero de celular",
            "numero do celular",
            "numero de telefone",
        ):
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
            "autorizado a trabalhar",
            "autorizacao de trabalho",
            "autorizacao para trabalhar",
        ):
            return QuestionClassification(
                question_type=QuestionType.WORK_AUTHORIZATION,
                normalized_key="work_authorization",
                confidence=0.98,
                matched_rule="work_authorization",
            )
        if self._contains_any(
            normalized,
            "sponsorship",
            "visa",
            "require sponsor",
            "patrocinio",
            "patrocinio de visto",
            "necessita de visto",
        ):
            return QuestionClassification(
                question_type=QuestionType.VISA_SPONSORSHIP,
                normalized_key="visa_sponsorship",
                confidence=0.98,
                matched_rule="visa_sponsorship",
            )
        if self._contains_any(
            normalized,
            "salary",
            "compensation",
            "pay expectation",
            "pretensao salarial",
            "faixa salarial",
            "remuneracao",
        ):
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
            "data de inicio",
            "disponibilidade",
            "quando pode comecar",
            "quando voce pode comecar",
        ):
            return QuestionClassification(
                question_type=QuestionType.START_DATE,
                normalized_key="start_date",
                confidence=0.95,
                matched_rule="start_date",
            )
        if self._contains_any(normalized, "city", "cidade", "municipio") or (
            self._contains_any(normalized, "location", "localizacao", "localidade")
            and control_kind in {"text", "textarea"}
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
        if self._is_binary_option_set(meaningful_options) or self._looks_like_binary_question(
            normalized
        ):
            return QuestionClassification(
                question_type=QuestionType.YES_NO_GENERIC,
                normalized_key=default_key,
                confidence=0.9,
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

    def _is_binary_option_set(self, options: set[str]) -> bool:
        if not options:
            return False
        canonical_tokens = {_canonical_binary_token(option) for option in options}
        canonical_values = {token for token in canonical_tokens if token is not None}
        return bool(canonical_values) and canonical_values.issubset({"yes", "no"})

    def _looks_like_binary_question(self, normalized_question: str) -> bool:
        return self._contains_any(
            normalized_question,
            "are you comfortable",
            "do you feel comfortable",
            "comfortable working",
            "feel comfortable working",
            "do you feel confident",
            "voce se sente confortavel",
            "se sente confortavel",
            "confortavel trabalhando",
            "confortavel em um ambiente",
        )

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
        validation_context: ValidationFeedbackContext | None = None,
    ) -> GeneratedAnswer | None:
        """Generate a structured answer using the user's configured model and key."""

        if settings.ai.api_key is None:
            return None
        if field.question_type not in {
            QuestionType.UNKNOWN,
            QuestionType.YES_NO_GENERIC,
            QuestionType.FREE_TEXT_GENERIC,
            QuestionType.YEARS_EXPERIENCE,
            QuestionType.SALARY_EXPECTATION,
        }:
            return None

        prompt_payload = self._build_prompt_payload(
            field=field,
            settings=settings,
            posting=posting,
            validation_context=validation_context,
        )
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
            if (
                selected_option is None
                and field.question_type is QuestionType.YEARS_EXPERIENCE
                and re.fullmatch(r"\d+(?:[.,]\d+)?", answer)
            ):
                selected_option = pick_numeric_option(
                    field.options,
                    target_value=float(answer.replace(",", ".")),
                )
            if selected_option is None:
                return None
            answer = selected_option
        if _answer_matches_unreliable_negative_option_label(field=field, answer=answer):
            logger.info(
                "linkedin_ai_autofill_guardrail_rejected",
                extra={
                    "normalized_key": field.normalized_key,
                    "question_type": field.question_type.value,
                    "answer": answer,
                    "reason": "unreliable_negative_option_label",
                },
            )
            return None
        if _answer_uses_target_employer_for_candidate_field(
            field=field,
            answer=answer,
            posting=posting,
        ):
            logger.info(
                "linkedin_ai_autofill_guardrail_rejected",
                extra={
                    "normalized_key": field.normalized_key,
                    "question_type": field.question_type.value,
                    "answer": answer,
                    "reason": "target_employer_reused_as_candidate_employer",
                },
            )
            return None

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
                                "Respect the control type and visible options exactly. "
                                "If options exist, answer with exactly one available option label. "
                                "If the field expects years of experience and there are no "
                                "options, answer with a plain integer string such as '2'. "
                                "If the field expects a salary or numeric value, answer with a "
                                "plain positive number string without currency symbols. "
                                "Some LinkedIn radio/select groups expose a broken field label "
                                "that is actually just one visible option, sometimes the most "
                                "negative one. When that happens, treat the raw field label as "
                                "unreliable and infer the real intent from the full option set, "
                                "the job context, and the candidate profile. "
                                "Prefer plausible and conservative answers over exactness when "
                                "the candidate profile lacks the precise detail. "
                                "For derived technologies, infer from the closest parent stack, "
                                "stay internally consistent, and never exceed the broader stack "
                                "experience. Prefer modest values for newer frameworks. "
                                "For proficiency ladders such as Basic/Intermediate/Advanced/"
                                "Native, never choose the lowest or negative option merely "
                                "because it repeats the raw field label. Prefer the lowest "
                                "plausible working level instead. "
                                "If the job description requires English or international remote "
                                "collaboration, do not answer with no English at all unless the "
                                "candidate profile explicitly says that. "
                                "Never reuse the target job company as the candidate's current "
                                "or previous employer unless the profile explicitly says so. "
                                "If current employer data is missing, prefer a neutral plausible "
                                "non-target answer such as Freelancer or Self-employed. "
                                "For language proficiency ladders, prefer conservative middle "
                                "options such as intermediate when exact data is missing. "
                                "Do not invent legal, visa, or certification facts. "
                                "Keep free-text answers concise, professional, and believable. "
                                "When validation feedback is provided, assume the previous "
                                "attempt was rejected, use that feedback to repair the answer, "
                                "and avoid repeating the same invalid value."
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
        validation_context: ValidationFeedbackContext | None = None,
    ) -> dict[str, object]:
        profile_payload = {
            "name": settings.profile.name,
            "first_name": _profile_first_name(settings.profile.name),
            "last_name": _profile_last_name(settings.profile.name),
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
        prompt_payload: dict[str, object] = {
            "question": field.question_raw,
            "normalized_key": field.normalized_key,
            "question_type": field.question_type.value,
            "control_kind": field.control_kind,
            "input_type": field.input_type,
            "expected_answer_shape": (
                "integer_years"
                if field.question_type is QuestionType.YEARS_EXPERIENCE
                else "numeric_salary"
                if field.question_type is QuestionType.SALARY_EXPECTATION
                else None
            ),
            "response_contract": {
                "must_choose_visible_option": bool(field.options),
                "must_return_yes_or_no": (
                    field.question_type is QuestionType.YES_NO_GENERIC and not field.options
                ),
                "must_return_plain_integer": field.question_type is QuestionType.YEARS_EXPERIENCE,
                "must_return_plain_number": (
                    field.question_type is QuestionType.SALARY_EXPECTATION
                    or field.input_type == "number"
                ),
                "keep_free_text_concise": field.control_kind == "textarea",
            },
            "options": list(field.options),
            "field_label_reliability": (
                "low" if _field_label_matches_visible_option(field) else "normal"
            ),
            "option_set_observations": _build_option_set_observations(field),
            "current_value": field.current_value,
            "experience_inference_context": self._build_experience_inference_context(
                field=field,
                settings=settings,
                posting=posting,
            ),
            "inference_policy": [
                "Prefer exact profile facts when they exist.",
                "When exact data is missing, choose a conservative plausible answer.",
                (
                    "If a tool or framework is implied by a broader stack, infer from that "
                    "stack conservatively."
                ),
                "Never exceed the candidate's broader stack experience or total experience.",
                (
                    "For newer frameworks, prefer a modest number such as 1, 2, or 3 instead "
                    "of an aggressive claim."
                ),
                (
                    "If visible options exist, choose the closest plausible visible option "
                    "rather than inventing text."
                ),
                (
                    "Never use the target job company as the candidate's current employer "
                    "unless the profile explicitly confirms that fact."
                ),
                (
                    "If current employer information is missing, prefer a neutral plausible "
                    "non-target answer such as Freelancer or Self-employed."
                ),
                (
                    "For language proficiency ladders, prefer conservative middle options "
                    "such as intermediate instead of extreme claims."
                ),
                (
                    "For legal authorization, visa, certifications, or other compliance facts, "
                    "do not invent missing facts."
                ),
            ],
            "job": {
                "title": posting.title,
                "company_name": posting.company_name,
                "location": posting.location,
                "description_raw": posting.description_raw,
            },
            "candidate_profile": profile_payload,
        }
        if validation_context is not None and any(
            (
                _non_empty_value(validation_context.validation_message),
                _non_empty_value(validation_context.current_value),
                _non_empty_value(validation_context.previous_answer),
            )
        ):
            prompt_payload["validation_feedback"] = {
                "validation_message": validation_context.validation_message,
                "current_value": validation_context.current_value,
                "previous_answer": validation_context.previous_answer,
                "repair_policy": [
                    (
                        "Treat the validation message as direct evidence of why the prior "
                        "answer failed."
                    ),
                    ("Do not repeat the same invalid answer unless you are only repairing format."),
                    "Prefer the smallest plausible correction that satisfies the validation.",
                ],
            }
        return prompt_payload

    def _build_experience_inference_context(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> dict[str, object]:
        exact_stack_matches: list[dict[str, object]] = []
        normalized_question = normalize_text(
            " ".join(
                (
                    field.question_raw,
                    field.normalized_key,
                    posting.title,
                    posting.description_raw,
                )
            )
        )
        ordered_stacks = sorted(
            settings.profile.years_experience_by_stack.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        for stack_name, years in ordered_stacks:
            normalized_stack = normalize_text(stack_name)
            normalized_stack_key = normalize_key(stack_name)
            if (normalized_stack and normalized_stack in normalized_question) or (
                normalized_stack_key and normalized_stack_key in normalized_question
            ):
                exact_stack_matches.append({"stack": stack_name, "years": years})

        strongest_stack = ordered_stacks[0] if ordered_stacks else None
        conservative_years = _infer_conservative_related_years(
            settings.profile.years_experience_by_stack
        )
        return {
            "exact_stack_matches": exact_stack_matches,
            "top_known_stacks": [
                {"stack": stack_name, "years": years} for stack_name, years in ordered_stacks[:6]
            ],
            "strongest_known_stack": (
                {"stack": strongest_stack[0], "years": strongest_stack[1]}
                if strongest_stack is not None
                else None
            ),
            "conservative_inferred_years_for_related_tooling": conservative_years,
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

        if field.prefilled and field_has_meaningful_current_value(field):
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

        if _looks_like_sensitive_demographic_question(field):
            sensitive_opt_out = _resolve_sensitive_opt_out_answer(field)
            if sensitive_opt_out is None:
                return None
            return ResolvedFieldValue(
                value=sensitive_opt_out.value,
                answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
                fill_strategy=FillStrategy.BEST_EFFORT,
                ambiguity_flag=True,
                confidence=sensitive_opt_out.confidence,
                reasoning=sensitive_opt_out.reasoning,
            )

        ai_answer = await self._generate_ai_answer(field=field, settings=settings, posting=posting)
        if ai_answer is not None:
            if _answer_uses_target_employer_for_candidate_field(
                field=field,
                answer=ai_answer.value,
                posting=posting,
            ):
                ai_answer = None
            elif _answer_matches_unreliable_negative_option_label(
                field=field,
                answer=ai_answer.value,
            ):
                ai_answer = None
            else:
                return ResolvedFieldValue(
                    value=ai_answer.value,
                    answer_source=AnswerSource.AI,
                    fill_strategy=FillStrategy.AUTOFILL_AI,
                    ambiguity_flag=True,
                    confidence=ai_answer.confidence,
                    reasoning=ai_answer.reasoning,
                )

        guardrail_answer = self._resolve_guardrail_fallback(field, settings)
        if guardrail_answer is None:
            return None
        return ResolvedFieldValue(
            value=guardrail_answer.value,
            answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
            fill_strategy=FillStrategy.BEST_EFFORT,
            ambiguity_flag=True,
            confidence=guardrail_answer.confidence,
            reasoning=guardrail_answer.reasoning,
        )

    async def resolve_with_validation_feedback(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
        *,
        posting: JobPosting,
        validation_message: str | None,
        current_value: str = "",
        previous_answer: str | None = None,
    ) -> ResolvedFieldValue | None:
        normalized_validation = normalize_text(validation_message or "")
        adapted_field = self._adapt_field_for_validation_feedback(field, normalized_validation)
        validation_context = ValidationFeedbackContext(
            validation_message=validation_message,
            current_value=current_value,
            previous_answer=previous_answer,
        )
        candidates: list[ResolvedFieldValue] = []
        has_feedback_ai_candidate = False

        if (
            settings.ruleset.allow_best_effort_autofill
            and not _looks_like_sensitive_demographic_question(adapted_field)
        ):
            feedback_ai_answer = await self._generate_ai_answer(
                field=adapted_field,
                settings=settings,
                posting=posting,
                validation_context=validation_context,
            )
            if feedback_ai_answer is not None:
                candidates.append(
                    ResolvedFieldValue(
                        value=feedback_ai_answer.value,
                        answer_source=AnswerSource.AI,
                        fill_strategy=FillStrategy.AUTOFILL_AI,
                        ambiguity_flag=True,
                        confidence=feedback_ai_answer.confidence,
                        reasoning=feedback_ai_answer.reasoning,
                    )
                )
                has_feedback_ai_candidate = True

        if adapted_field != field and not has_feedback_ai_candidate:
            adapted_resolution = await self.resolve(adapted_field, settings, posting=posting)
            if adapted_resolution is not None:
                candidates.append(adapted_resolution)

        if not has_feedback_ai_candidate:
            base_resolution = await self.resolve(field, settings, posting=posting)
            if base_resolution is not None:
                candidates.append(base_resolution)

        for raw_value, reasoning in (
            (previous_answer, "reuse_previous_answer_with_validation_feedback"),
            (current_value, "reuse_current_value_with_validation_feedback"),
        ):
            if raw_value is None or not raw_value.strip():
                continue
            coerced_value = _coerce_value_for_validation_feedback(
                raw_value,
                validation_message=normalized_validation,
            )
            if coerced_value is None:
                continue
            candidates.append(
                ResolvedFieldValue(
                    value=coerced_value,
                    answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
                    fill_strategy=FillStrategy.BEST_EFFORT,
                    ambiguity_flag=True,
                    confidence=0.12,
                    reasoning=reasoning,
                )
            )

        for candidate in candidates:
            coerced_candidate = _coerce_value_for_validation_feedback(
                candidate.value,
                validation_message=normalized_validation,
            )
            if coerced_candidate is None:
                continue
            if field.options:
                if adapted_field.question_type is QuestionType.YEARS_EXPERIENCE and re.fullmatch(
                    r"\d+(?:[.,]\d+)?",
                    coerced_candidate,
                ):
                    selected_option = pick_numeric_option(
                        field.options,
                        target_value=float(coerced_candidate.replace(",", ".")),
                    )
                else:
                    selected_option = pick_option(field.options, preferred=coerced_candidate)
                if selected_option is None:
                    continue
                coerced_candidate = selected_option
            return replace(candidate, value=coerced_candidate)

        return None

    async def _generate_ai_answer(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
        validation_context: ValidationFeedbackContext | None = None,
    ) -> GeneratedAnswer | None:
        try:
            return await self._ambiguous_answer_generator.generate(
                field=field,
                settings=settings,
                posting=posting,
                validation_context=validation_context,
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
            case QuestionType.FIRST_NAME:
                return _non_empty_value(_profile_first_name(profile.name))
            case QuestionType.LAST_NAME:
                return _non_empty_value(_profile_last_name(profile.name))
            case QuestionType.EMAIL:
                return _non_empty_value(str(profile.email))
            case QuestionType.PHONE:
                if normalize_key(field.normalized_key) == "phone_country_code":
                    return self._resolve_phone_country_code(field)
                return _non_empty_value(profile.phone)
            case QuestionType.CITY:
                return _non_empty_value(profile.city)
            case QuestionType.LINKEDIN_URL:
                return str(profile.linkedin_url) if profile.linkedin_url else None
            case QuestionType.GITHUB_URL:
                return str(profile.github_url) if profile.github_url else None
            case QuestionType.PORTFOLIO_URL:
                return str(profile.portfolio_url) if profile.portfolio_url else None
            case QuestionType.SALARY_EXPECTATION:
                return (
                    _non_empty_value(str(profile.salary_expectation))
                    if profile.salary_expectation is not None
                    else None
                )
            case QuestionType.START_DATE:
                return _non_empty_value(profile.availability)
            case QuestionType.RESUME_UPLOAD:
                return _non_empty_value(profile.cv_path)
            case QuestionType.YEARS_EXPERIENCE:
                return self._resolve_exact_years_experience(field, settings)
            case _:
                return self._resolve_profile_value_by_normalized_key(field.normalized_key, settings)

    def _resolve_profile_value_by_normalized_key(
        self,
        normalized_key: str,
        settings: UserAgentSettings,
    ) -> str | None:
        match normalize_key(normalized_key):
            case "first_name" | "given_name" | "forename":
                return _non_empty_value(_profile_first_name(settings.profile.name))
            case "last_name" | "family_name" | "surname":
                return _non_empty_value(_profile_last_name(settings.profile.name))
            case "city" | "current_city" | "location_city":
                return _non_empty_value(settings.profile.city)
            case "email":
                return _non_empty_value(str(settings.profile.email))
            case "phone_country_code":
                synthetic_field = EasyApplyField(
                    question_raw="Country code",
                    normalized_key="phone_country_code",
                    question_type=QuestionType.PHONE,
                    control_kind="select",
                )
                return self._resolve_phone_country_code(synthetic_field)
            case "phone" | "phone_number" | "mobile" | "mobile_number":
                return _non_empty_value(settings.profile.phone)
            case _:
                return None

    def _resolve_phone_country_code(self, field: EasyApplyField) -> str | None:
        if field.options:
            preferred_tokens = (
                normalize_text("Brazil"),
                normalize_text("Brasil"),
                normalize_text("+55"),
                normalize_text("55"),
            )
            for option in field.options:
                normalized_option = normalize_text(option)
                if normalized_option in _PLACEHOLDER_OPTION_TOKENS:
                    continue
                if any(token and token in normalized_option for token in preferred_tokens):
                    return option
            fallback_option = pick_option(field.options, preferred=None)
            if fallback_option is not None:
                return fallback_option
        return "+55"

    def _resolve_exact_years_experience(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> str | None:
        normalized_question = normalize_text(f"{field.question_raw} {field.normalized_key}")
        matched_years: list[int] = []
        for stack_name, years in settings.profile.years_experience_by_stack.items():
            normalized_stack = normalize_text(stack_name)
            normalized_stack_key = normalize_key(stack_name)
            if (normalized_stack and normalized_stack in normalized_question) or (
                normalized_stack_key and normalized_stack_key in normalized_question
            ):
                matched_years.append(years)

        if matched_years:
            return str(matched_years[0] if len(matched_years) == 1 else min(matched_years))
        return None

    def _resolve_guardrail_fallback(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> GuardrailAnswer | None:
        profile = settings.profile
        normalized_question = normalize_text(field.question_raw)

        if field.question_type is QuestionType.RESUME_UPLOAD:
            cv_path = _non_empty_value(profile.cv_path)
            if cv_path is None:
                return None
            return GuardrailAnswer(
                value=cv_path,
                confidence=0.55,
                reasoning="guardrail_resume_path",
            )
        if field.question_type is QuestionType.YEARS_EXPERIENCE:
            inferred_years = self._infer_plausible_years_experience(field, settings)
            if inferred_years is None:
                return None
            resolved_value = inferred_years
            if field.options:
                numeric_option = pick_numeric_option(
                    field.options,
                    target_value=float(inferred_years),
                )
                if numeric_option is not None:
                    resolved_value = numeric_option
            return GuardrailAnswer(
                value=resolved_value,
                confidence=0.42,
                reasoning="plausible_profile_inference",
            )
        normalized_key = normalize_key(field.normalized_key)
        if normalized_key in {"first_name", "given_name", "forename"}:
            first_name = _non_empty_value(_profile_first_name(profile.name))
            if first_name is None:
                return None
            return GuardrailAnswer(
                value=first_name,
                confidence=0.72,
                reasoning="guardrail_profile_identity",
            )
        if normalized_key in {"last_name", "family_name", "surname"}:
            last_name = _non_empty_value(_profile_last_name(profile.name))
            if last_name is None:
                return None
            return GuardrailAnswer(
                value=last_name,
                confidence=0.72,
                reasoning="guardrail_profile_identity",
            )
        if normalized_key in {"city", "current_city", "location_city"}:
            city = _non_empty_value(profile.city)
            if city is None:
                return None
            return GuardrailAnswer(
                value=city,
                confidence=0.68,
                reasoning="guardrail_profile_location",
            )
        if normalized_key == "phone_country_code":
            phone_country_code = self._resolve_phone_country_code(field)
            if phone_country_code is None:
                return None
            return GuardrailAnswer(
                value=phone_country_code,
                confidence=0.7,
                reasoning="guardrail_phone_country_code",
            )
        if _looks_like_current_employer_question(field):
            return GuardrailAnswer(
                value="Freelancer",
                confidence=0.24,
                reasoning="guardrail_unknown_current_employer",
            )
        if field.options and _looks_like_language_proficiency_question(field):
            proficiency_option = _pick_conservative_language_proficiency_option(field.options)
            if proficiency_option is not None:
                return GuardrailAnswer(
                    value=proficiency_option,
                    confidence=0.22,
                    reasoning="guardrail_conservative_language_proficiency",
                )

        if field.options:
            preferred = "No" if _prefers_negative_answer(normalized_question) else None
            option = pick_option(field.options, preferred=preferred)
            if option is None:
                return None
            return GuardrailAnswer(
                value=option,
                confidence=0.3,
                reasoning="typed_guardrail_option_pick",
            )
        if field.question_type is QuestionType.YES_NO_GENERIC:
            return GuardrailAnswer(
                value="No" if _prefers_negative_answer(normalized_question) else "Yes",
                confidence=0.34,
                reasoning="typed_guardrail_binary",
            )

        if field.control_kind == "checkbox":
            return GuardrailAnswer(
                value="No" if _prefers_negative_answer(normalized_question) else "Yes",
                confidence=0.28,
                reasoning="typed_guardrail_checkbox",
            )
        if field.control_kind == "file":
            cv_path = _non_empty_value(profile.cv_path)
            if cv_path is None:
                return None
            return GuardrailAnswer(
                value=cv_path,
                confidence=0.55,
                reasoning="guardrail_resume_path",
            )
        if field.control_kind == "textarea":
            textarea_value = self._first_default_response(settings) or "Open to discuss."
            return GuardrailAnswer(
                value=textarea_value,
                confidence=0.4,
                reasoning="guardrail_concise_free_text",
            )
        if field.input_type == "email":
            email = _non_empty_value(str(profile.email))
            if email is None:
                return None
            return GuardrailAnswer(
                value=email,
                confidence=0.7,
                reasoning="guardrail_profile_contact",
            )
        if field.input_type == "tel":
            phone = _non_empty_value(profile.phone)
            if phone is None:
                return None
            return GuardrailAnswer(
                value=phone,
                confidence=0.7,
                reasoning="guardrail_profile_contact",
            )
        if field.input_type == "url":
            if profile.linkedin_url is None:
                return None
            return GuardrailAnswer(
                value=str(profile.linkedin_url),
                confidence=0.5,
                reasoning="guardrail_profile_url",
            )
        if field.input_type == "number":
            if field.question_type is QuestionType.SALARY_EXPECTATION:
                if profile.salary_expectation is None:
                    return None
                return GuardrailAnswer(
                    value=str(profile.salary_expectation),
                    confidence=0.6,
                    reasoning="guardrail_profile_salary",
                )
            inferred_years = self._infer_plausible_years_experience(field, settings)
            if inferred_years is None:
                return None
            return GuardrailAnswer(
                value=inferred_years,
                confidence=0.38,
                reasoning="plausible_profile_inference",
            )
        if field.question_type is QuestionType.FREE_TEXT_GENERIC:
            free_text_value = self._first_default_response(settings) or "Open to discuss."
            return GuardrailAnswer(
                value=free_text_value,
                confidence=0.36,
                reasoning="guardrail_concise_free_text",
            )
        return None

    def _adapt_field_for_validation_feedback(
        self,
        field: EasyApplyField,
        normalized_validation: str,
    ) -> EasyApplyField:
        if not _validation_requires_numeric(normalized_validation):
            return field

        normalized_question = normalize_text(f"{field.question_raw} {field.normalized_key}")
        adapted_question_type = field.question_type
        if adapted_question_type in {
            QuestionType.UNKNOWN,
            QuestionType.YES_NO_GENERIC,
            QuestionType.FREE_TEXT_GENERIC,
        }:
            if any(
                token in normalized_question
                for token in (
                    "experience",
                    "years",
                    "anos",
                    "automation",
                    "framework",
                    "langchain",
                    "python",
                    "sql",
                    "java",
                    "javascript",
                )
            ):
                adapted_question_type = QuestionType.YEARS_EXPERIENCE
            elif any(
                token in normalized_question
                for token in (
                    "salary",
                    "compensation",
                    "pretensao",
                    "faixa salarial",
                    "remuneracao",
                )
            ):
                adapted_question_type = QuestionType.SALARY_EXPECTATION

        return replace(
            field,
            question_type=adapted_question_type,
            input_type="number",
        )

    def _infer_plausible_years_experience(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> str | None:
        exact_match = self._resolve_exact_years_experience(field, settings)
        if exact_match is not None:
            return exact_match
        inferred_years = _infer_conservative_related_years(
            settings.profile.years_experience_by_stack
        )
        if inferred_years is None:
            return None
        return str(inferred_years)

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
        canonical_preferred = _canonical_binary_token(normalized_preferred)
        for option in options:
            normalized_option = normalize_text(option)
            if normalized_option == normalized_preferred:
                return option
            if (
                canonical_preferred is not None
                and _canonical_binary_token(normalized_option) == canonical_preferred
            ):
                return option
        for option in options:
            normalized_option = normalize_text(option)
            if (
                normalized_preferred in normalized_option
                or normalized_option in normalized_preferred
            ):
                return option

    for option in options:
        if normalize_text(option) not in _PLACEHOLDER_OPTION_TOKENS:
            return option
    return options[0]


def pick_numeric_option(options: tuple[str, ...], *, target_value: float) -> str | None:
    """Pick the option whose numeric meaning best matches the requested target."""

    best_option: str | None = None
    best_score: tuple[int, float, float] | None = None
    for option in options:
        normalized_option = normalize_text(option)
        if normalized_option in _PLACEHOLDER_OPTION_TOKENS:
            continue
        numeric_bounds = _parse_numeric_option_bounds(option)
        if numeric_bounds is None:
            continue
        lower_bound, upper_bound = numeric_bounds
        if lower_bound <= target_value <= upper_bound:
            range_width = upper_bound - lower_bound
            score = (3, -range_width, -abs(target_value - lower_bound))
        else:
            nearest_bound = lower_bound if target_value < lower_bound else upper_bound
            distance = abs(target_value - nearest_bound)
            score = (2, -distance, -(upper_bound - lower_bound))
        if best_score is None or score > best_score:
            best_score = score
            best_option = option
    return best_option


def _parse_numeric_option_bounds(option: str) -> tuple[float, float] | None:
    numbers = [
        float(match.group(0).replace(",", ".")) for match in re.finditer(r"\d+(?:[.,]\d+)?", option)
    ]
    if not numbers:
        return None
    normalized_option = normalize_text(option)
    if len(numbers) >= 2 and any(token in normalized_option for token in {"-", " to ", " a "}):
        lower_bound, upper_bound = sorted(numbers[:2])
        return lower_bound, upper_bound
    if "+" in option:
        return numbers[0], numbers[0] + 100.0
    if any(token in normalized_option for token in {"less than", "up to", "ate ", "até ", "<"}):
        return 0.0, numbers[0]
    return numbers[0], numbers[0]


def _infer_conservative_related_years(years_by_stack: dict[str, int]) -> int | None:
    if not years_by_stack:
        return None
    strongest_years = max(years_by_stack.values())
    if strongest_years <= 0:
        return None
    conservative_years = round(strongest_years * 0.25)
    return max(1, min(3, conservative_years))


def _prefers_negative_answer(normalized_question: str) -> bool:
    return any(
        token in normalized_question
        for token in (
            "follow",
            "talent community",
            "newsletter",
            "mailing list",
            "job alerts",
            "marketing",
            "future opportunities",
        )
    )


def _validation_requires_numeric(validation_message: str) -> bool:
    if not validation_message:
        return False
    return any(token in validation_message for token in _NUMERIC_VALIDATION_TOKENS) or any(
        token in validation_message
        for token in (
            "larger than",
            "greater than",
            "less than",
            "at least",
            "at most",
            "maior que",
            "menor que",
            "maior ou igual",
            "menor ou igual",
        )
    )


def _validation_requires_integer(validation_message: str) -> bool:
    if not validation_message:
        return False
    return any(
        token in validation_message
        for token in (
            "integer",
            "whole number",
            "plain integer",
            "inteiro",
        )
    )


def _extract_numeric_validation_floor(
    validation_message: str,
) -> tuple[float | None, bool]:
    if not validation_message:
        return None, False
    match = re.search(r"(-?\d+(?:[.,]\d+)?)", validation_message)
    if match is None:
        return None, False
    lower_bound = float(match.group(1).replace(",", "."))
    exclusive = any(
        token in validation_message
        for token in (
            "larger than",
            "greater than",
            "more than",
            "maior que",
            ">",
        )
    )
    return lower_bound, exclusive


def _coerce_value_for_validation_feedback(
    value: str,
    *,
    validation_message: str,
) -> str | None:
    stripped_value = value.strip()
    if not stripped_value:
        return None
    if not _validation_requires_numeric(validation_message):
        return stripped_value

    numeric_match = re.search(r"-?\d+(?:[.,]\d+)?", stripped_value)
    if numeric_match is None:
        return None
    numeric_value = float(numeric_match.group(0).replace(",", "."))
    lower_bound, exclusive = _extract_numeric_validation_floor(validation_message)
    if lower_bound is not None:
        if exclusive and numeric_value <= lower_bound:
            numeric_value = lower_bound + 1.0
        elif not exclusive and numeric_value < lower_bound:
            numeric_value = lower_bound
    if numeric_value <= 0 and "positive" in validation_message:
        numeric_value = 1.0
    if _validation_requires_integer(validation_message):
        return str(max(1, int(round(numeric_value))))
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return f"{numeric_value:.1f}".rstrip("0").rstrip(".")


def _looks_like_current_employer_question(field: EasyApplyField) -> bool:
    normalized = normalize_text(f"{field.question_raw} {field.normalized_key}")
    return any(
        token in normalized
        for token in (
            "current company",
            "current employer",
            "company where you work",
            "company you work for",
            "empresa onde voce trabalha",
            "empresa onde vc trabalha",
            "empresa em que voce trabalha",
            "empresa atual",
            "empregador atual",
        )
    )


def _answer_uses_target_employer_for_candidate_field(
    *,
    field: EasyApplyField,
    answer: str,
    posting: JobPosting,
) -> bool:
    if not _looks_like_current_employer_question(field):
        return False
    normalized_answer = normalize_text(answer)
    normalized_company = normalize_text(posting.company_name)
    if not normalized_answer or not normalized_company:
        return False
    return normalized_answer == normalized_company or normalized_company in normalized_answer


def _looks_like_language_proficiency_question(field: EasyApplyField) -> bool:
    normalized = normalize_text(
        " ".join((field.question_raw, field.normalized_key, *field.options))
    )
    if not any(
        token in normalized
        for token in ("english", "ingles", "spanish", "espanhol", "language", "idioma")
    ):
        return False
    if any(
        token in normalized
        for token in (
            "proficiency",
            "level",
            "nivel",
            "fluency",
            "fluencia",
            "comfortable",
            "confortavel",
            "comfort",
        )
    ):
        return True
    return any(
        token in normalized
        for token in (
            "beginner",
            "basic",
            "intermediate",
            "advanced",
            "native",
            "fluent",
            "fluente",
            "working proficiency",
            "professional working proficiency",
        )
    )


def _field_label_matches_visible_option(field: EasyApplyField) -> bool:
    normalized_question = normalize_text(field.question_raw)
    if not normalized_question:
        return False
    return any(normalize_text(option) == normalized_question for option in field.options)


def _build_option_set_observations(field: EasyApplyField) -> list[str]:
    observations: list[str] = []
    if _field_label_matches_visible_option(field):
        observations.append("raw_field_label_matches_one_visible_option")
    if _looks_like_language_proficiency_question(field):
        observations.append("visible_options_form_language_or_proficiency_ladder")
    if len(field.options) >= 3:
        observations.append("multiple_visible_options_available")
    return observations


def _answer_matches_unreliable_negative_option_label(
    *,
    field: EasyApplyField,
    answer: str,
) -> bool:
    if not _field_label_matches_visible_option(field):
        return False
    if not _looks_like_language_proficiency_question(field):
        return False
    normalized_answer = normalize_text(answer)
    normalized_question = normalize_text(field.question_raw)
    if normalized_answer != normalized_question:
        return False
    return any(
        token in normalized_answer
        for token in (
            "don't know",
            "do not know",
            "no english",
            "not know english",
            "nenhum ingles",
            "nao sei ingles",
            "i don't know",
            "zero english",
            "sem ingles",
        )
    )


def _pick_conservative_language_proficiency_option(options: tuple[str, ...]) -> str | None:
    meaningful_options = [
        option for option in options if normalize_text(option) not in _PLACEHOLDER_OPTION_TOKENS
    ]
    if not meaningful_options:
        return None
    preferred_tokens = (
        "intermediate",
        "intermediario",
        "b2",
        "b1",
        "working proficiency",
        "professional working proficiency",
        "conversational",
    )
    for option in meaningful_options:
        normalized_option = normalize_text(option)
        if any(token in normalized_option for token in preferred_tokens):
            return option
    if len(meaningful_options) >= 3:
        return meaningful_options[len(meaningful_options) // 2]
    return meaningful_options[0]


def _looks_like_sensitive_demographic_question(field: EasyApplyField) -> bool:
    normalized = normalize_text(f"{field.question_raw} {field.normalized_key}")
    return any(
        token in normalized
        for token in (
            "gender",
            "genero",
            "pronoun",
            "pronome",
            "sexual orientation",
            "orientacao sexual",
            "lgbt",
            "lgbtq",
            "race",
            "ethnicity",
            "ethnic",
            "raca",
            "etnia",
            "cor",
            "disability",
            "disabled",
            "deficiencia",
            "pcd",
            "veteran",
            "veterano",
        )
    )


def _pick_sensitive_opt_out_option(options: tuple[str, ...]) -> str | None:
    for option in options:
        normalized_option = normalize_text(option)
        if normalized_option in _PLACEHOLDER_OPTION_TOKENS:
            continue
        if normalized_option in _SENSITIVE_OPT_OUT_TOKENS:
            return option
        if any(token in normalized_option for token in _SENSITIVE_OPT_OUT_TOKENS):
            return option
    return None


def _resolve_sensitive_opt_out_answer(field: EasyApplyField) -> GuardrailAnswer | None:
    if not field.options:
        return None
    opt_out_option = _pick_sensitive_opt_out_option(field.options)
    if opt_out_option is None:
        return None
    return GuardrailAnswer(
        value=opt_out_option,
        confidence=0.92,
        reasoning="sensitive_question_opt_out",
    )


def _profile_first_name(full_name: str) -> str:
    parts = [part for part in full_name.strip().split() if part]
    return parts[0] if parts else ""


def _profile_last_name(full_name: str) -> str:
    parts = [part for part in full_name.strip().split() if part]
    if len(parts) >= 2:
        return " ".join(parts[1:])
    return ""
