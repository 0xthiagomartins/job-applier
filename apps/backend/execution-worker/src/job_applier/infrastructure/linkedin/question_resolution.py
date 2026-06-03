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
from job_applier.infrastructure.candidate_capabilities import (
    build_candidate_capability_profile,
    capability_profile_to_payload,
    find_capability_range_for_text,
)
from job_applier.infrastructure.language_support import (
    combine_language_signals,
    detect_job_posting_language,
    detect_text_language,
)

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

SEMANTIC_STEP_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "field_plans": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field_ref": {
                        "type": "string",
                        "description": "Stable field reference echoed from the prompt payload.",
                    },
                    "semantic_slot": {
                        "type": ["string", "null"],
                        "description": (
                            "Short English semantic identifier such as "
                            "candidate.contact.email when inferable."
                        ),
                    },
                    "answer": {
                        "type": ["string", "null"],
                        "description": (
                            "Chosen answer. Must exactly match one visible option label when "
                            "options exist. Use null when the answer is too uncertain."
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
                        "description": "Short explanation grounded in visible evidence.",
                    },
                },
                "required": ["field_ref", "semantic_slot", "answer", "confidence", "reasoning"],
            },
        },
    },
    "required": ["field_plans"],
}


def collapse_whitespace(value: str) -> str:
    """Collapse repeated whitespace while preserving original casing."""

    return re.sub(r"\s+", " ", value).strip()


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


def _looks_like_salary_expectation_text(normalized: str) -> bool:
    direct_terms = (
        "salary",
        "compensation",
        "pay expectation",
        "salary expectation",
        "expected salary",
        "expectativa salarial",
        "pretensao salarial",
        "faixa salarial",
        "remuneracao",
    )
    if any(term in normalized for term in direct_terms):
        return True
    expectation_terms = (
        "expectation",
        "expected",
        "expectativa",
        "pretensao",
        "pretensão",
        "rate",
        "valor",
    )
    compensation_context_terms = (
        "currency",
        "monthly",
        "annual",
        "yearly",
        "hourly",
        "daily",
        "per month",
        "per year",
        "per hour",
        "salario",
        "salário",
        "salarial",
        "compensation",
        "pay",
        "remuneracao",
        "remuneração",
    )
    return any(term in normalized for term in expectation_terms) and any(
        term in normalized for term in compensation_context_terms
    )


def field_reference(field: EasyApplyField) -> str:
    """Return the most stable reference we have for one extracted field."""

    return field.dom_ref or field.name or field.dom_id or field.normalized_key


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
    field_context: str = ""
    helper_text: str | None = None
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


@dataclass(frozen=True, slots=True)
class SemanticFieldPlan:
    """One semantic answer plan inferred from the whole Easy Apply step."""

    field_ref: str
    semantic_slot: str | None
    answer: str | None
    confidence: float
    reasoning: str


@dataclass(frozen=True, slots=True)
class SemanticStepPlan:
    """Structured AI plan for the fields in one Easy Apply step."""

    field_plans: tuple[SemanticFieldPlan, ...] = ()


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


class SemanticStepPlanner(Protocol):
    """Plan answers for one whole Easy Apply step using shared multilingual context."""

    async def plan(
        self,
        *,
        fields: tuple[EasyApplyField, ...],
        candidate_fields: tuple[EasyApplyField, ...],
        step_index: int,
        total_steps: int,
        surface_text: str,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> SemanticStepPlan | None:
        """Return a structured plan or `None` when the planner cannot help."""


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
        has_binary_options = self._is_binary_option_set(meaningful_options)

        if "linkedin" in normalized and not has_binary_options:
            return QuestionClassification(
                question_type=QuestionType.LINKEDIN_URL,
                normalized_key="linkedin_url",
                confidence=0.98,
                matched_rule="linkedin_url",
            )
        if "github" in normalized and not has_binary_options:
            return QuestionClassification(
                question_type=QuestionType.GITHUB_URL,
                normalized_key="github_url",
                confidence=0.98,
                matched_rule="github_url",
            )
        if (
            self._contains_any(normalized, "portfolio", "personal site", "website", "site")
            and not has_binary_options
        ):
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
        if _looks_like_salary_expectation_text(normalized) and not has_binary_options:
            return QuestionClassification(
                question_type=QuestionType.SALARY_EXPECTATION,
                normalized_key="salary_expectation",
                confidence=0.95,
                matched_rule="salary_expectation",
            )
        if self._looks_like_referral_source_question(normalized):
            return QuestionClassification(
                question_type=QuestionType.FREE_TEXT_GENERIC,
                normalized_key="referral_source",
                confidence=0.88,
                matched_rule="referral_source",
            )
        if self._looks_like_current_employer_question(normalized):
            return QuestionClassification(
                question_type=QuestionType.FREE_TEXT_GENERIC,
                normalized_key="current_employer",
                confidence=0.88,
                matched_rule="current_employer",
            )
        if self._looks_like_workplace_availability_question(normalized):
            return QuestionClassification(
                question_type=QuestionType.FREE_TEXT_GENERIC,
                normalized_key="workplace_availability",
                confidence=0.84,
                matched_rule="workplace_availability",
            )
        if self._looks_like_language_working_comfort_question(normalized):
            return QuestionClassification(
                question_type=QuestionType.FREE_TEXT_GENERIC,
                normalized_key=self._language_working_comfort_normalized_key(normalized),
                confidence=0.84,
                matched_rule="language_working_comfort",
            )
        if self._looks_like_proficiency_ladder_question(
            normalized_question=normalized,
            options_text=options_text,
            control_kind=control_kind,
        ):
            return QuestionClassification(
                question_type=QuestionType.FREE_TEXT_GENERIC,
                normalized_key=self._proficiency_ladder_normalized_key(normalized),
                confidence=0.82,
                matched_rule="proficiency_ladder",
            )
        if self._looks_like_disability_status_question(normalized):
            return QuestionClassification(
                question_type=QuestionType.YES_NO_GENERIC,
                normalized_key="disability_status",
                confidence=0.9,
                matched_rule="disability_status",
            )
        if self._looks_like_disability_type_question(normalized):
            return QuestionClassification(
                question_type=QuestionType.FREE_TEXT_GENERIC,
                normalized_key="disability_type",
                confidence=0.84,
                matched_rule="disability_type",
            )
        if self._looks_like_start_date_question(normalized):
            return QuestionClassification(
                question_type=QuestionType.START_DATE,
                normalized_key="start_date",
                confidence=0.95,
                matched_rule="start_date",
            )
        if self._looks_like_experience_duration_question(normalized):
            return QuestionClassification(
                question_type=QuestionType.YEARS_EXPERIENCE,
                normalized_key=default_key,
                confidence=0.9,
                matched_rule="years_experience_duration",
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

    def _looks_like_experience_duration_question(self, normalized: str) -> bool:
        experience_terms = (
            "experience",
            "experiencia",
            "expérience",
            "erfahrung",
            "esperienza",
            "using",
            "used",
            "usa",
            "usar",
            "utiliza",
            "utilizou",
            "trabalhou",
            "working with",
            "worked with",
        )
        duration_terms = (
            "year",
            "years",
            "yr",
            "yrs",
            "ano",
            "anos",
            "tempo",
            "time",
            "duration",
            "duracao",
            "duracion",
            "tiempo",
            "long",
        )
        how_long_phrases = (
            "how long",
            "for how long",
            "how many years",
            "since when",
            "ha quanto tempo",
            "quanto tempo",
            "quantos anos",
            "desde quando",
            "cuanto tiempo",
            "cuantos anos",
            "combien de temps",
            "seit wann",
        )
        work_context_terms = (
            "at work",
            "on the job",
            "professionally",
            "in production",
            "no trabalho",
            "em producao",
            "em produção",
            "profissionalmente",
        )
        role_terms = (
            "developer",
            "software",
            "engineer",
            "programmer",
            "desenvolvedor",
            "desenvolvimento",
            "engenheiro",
            "programador",
            "desarrollador",
            "ingeniero",
            "entwickler",
        )
        has_experience_and_duration = any(term in normalized for term in experience_terms) and any(
            term in normalized for term in duration_terms
        )
        has_how_long_role_prompt = any(phrase in normalized for phrase in how_long_phrases) and any(
            term in normalized for term in role_terms
        )
        has_how_long_work_prompt = any(phrase in normalized for phrase in how_long_phrases) and any(
            term in normalized for term in work_context_terms
        )
        return has_experience_and_duration or has_how_long_role_prompt or has_how_long_work_prompt

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

    def _looks_like_start_date_question(self, normalized_question: str) -> bool:
        if self._contains_any(
            normalized_question,
            "start date",
            "notice period",
            "when can you start",
            "data de inicio",
            "quando pode comecar",
            "quando voce pode comecar",
        ):
            return True
        if "availability" in normalized_question or "disponibilidade" in normalized_question:
            return not self._looks_like_workplace_availability_question(normalized_question)
        return False

    def _looks_like_referral_source_question(self, normalized_question: str) -> bool:
        return self._contains_any(
            normalized_question,
            "how did you hear",
            "how did you find",
            "where did you find",
            "como ficou sabendo",
            "como soube",
            "onde viu nossa vaga",
            "onde viu a vaga",
        )

    def _looks_like_current_employer_question(self, normalized_question: str) -> bool:
        return self._contains_any(
            normalized_question,
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

    def _looks_like_workplace_availability_question(self, normalized_question: str) -> bool:
        return self._contains_any(
            normalized_question,
            "trabalho presencial",
            "work onsite",
            "work on site",
            "onsite",
            "presencial",
            "mudanca",
            "mudar para",
            "relocation",
            "campinas",
        )

    def _looks_like_language_working_comfort_question(self, normalized_question: str) -> bool:
        if not self._contains_any(
            normalized_question,
            "english-speaking environment",
            "spanish-speaking environment",
            "portuguese-speaking environment",
            "ambiente onde se fala ingles",
            "ambiente onde se fala espanhol",
            "ambiente onde se fala portugues",
            "english work environment",
            "spanish work environment",
            "portuguese work environment",
        ):
            return False
        return self._contains_any(
            normalized_question,
            "comfortable",
            "comfort",
            "confortavel",
            "confortável",
            "confidence",
            "confianca",
            "confiança",
        )

    def _looks_like_proficiency_ladder_question(
        self,
        *,
        normalized_question: str,
        options_text: str,
        control_kind: ControlKind,
    ) -> bool:
        if control_kind not in {"radio", "select"}:
            return False
        combined = f"{normalized_question} {options_text}"
        if not self._contains_any(
            combined,
            "level",
            "nivel",
            "proficiency",
            "fluency",
            "fluencia",
            "confidence",
            "confianca",
            "knowledge",
            "conhecimento",
        ):
            return False
        return self._contains_any(
            combined,
            "advanced",
            "avancado",
            "intermediate",
            "intermediario",
            "basic",
            "basico",
            "beginner",
            "iniciante",
            "fluent",
            "fluente",
        )

    def _looks_like_disability_status_question(self, normalized_question: str) -> bool:
        return self._contains_any(
            normalized_question,
            "person with disabilities",
            "person with disability",
            "people with disabilities",
            "disabled person",
            "pessoa com deficiencia",
            "pessoa com deficiência",
            "pcd",
        )

    def _looks_like_disability_type_question(self, normalized_question: str) -> bool:
        return self._contains_any(
            normalized_question,
            "type of disability",
            "what type of disability",
            "qual o tipo de deficiencia",
            "tipo de deficiência",
        )

    def _proficiency_ladder_normalized_key(self, normalized_question: str) -> str:
        subject = self._proficiency_subject_key(normalized_question)
        return f"{subject}_proficiency"

    def _language_working_comfort_normalized_key(self, normalized_question: str) -> str:
        subject = self._proficiency_subject_key(normalized_question)
        return f"{subject}_work_environment_comfort"

    def _proficiency_subject_key(self, normalized_question: str) -> str:
        if self._contains_any(normalized_question, "english", "ingles"):
            return "english"
        if self._contains_any(normalized_question, "spanish", "espanhol", "espanol"):
            return "spanish"
        if self._contains_any(normalized_question, "portuguese", "portugues", "português"):
            return "portuguese"
        if self._contains_any(normalized_question, "java"):
            return "java"
        return "generic"

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
            field_context=str(payload.get("field_context") or ""),
            helper_text=str(payload.get("helper_text")) if payload.get("helper_text") else None,
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


def _build_candidate_profile_payload(settings: UserAgentSettings) -> dict[str, object]:
    capability_profile = build_candidate_capability_profile(settings)
    return {
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
        "preferred_language": settings.profile.preferred_language.value,
        "capability_profile": capability_profile_to_payload(capability_profile),
    }


def _extract_openai_output_text(response_data: dict[str, object]) -> str:
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


class OpenAISemanticStepPlanner:
    """Use the OpenAI Responses API to plan one whole Easy Apply step at a time."""

    endpoint = "https://api.openai.com/v1/responses"

    async def plan(
        self,
        *,
        fields: tuple[EasyApplyField, ...],
        candidate_fields: tuple[EasyApplyField, ...],
        step_index: int,
        total_steps: int,
        surface_text: str,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> SemanticStepPlan | None:
        if settings.ai.api_key is None or not candidate_fields:
            return None

        prompt_payload = self._build_prompt_payload(
            fields=fields,
            candidate_fields=candidate_fields,
            step_index=step_index,
            total_steps=total_steps,
            surface_text=surface_text,
            settings=settings,
            posting=posting,
        )
        logger.info(
            "linkedin_easy_apply_semantic_step_prompt",
            extra={
                "step_index": step_index,
                "total_steps": total_steps,
                "candidate_field_refs": [field_reference(field) for field in candidate_fields],
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
                "linkedin_easy_apply_semantic_step_failed",
                extra={"step_index": step_index, "model": settings.ai.model},
            )
            return None

        raw_output = _extract_openai_output_text(response_data)
        logger.info(
            "linkedin_easy_apply_semantic_step_response",
            extra={
                "step_index": step_index,
                "total_steps": total_steps,
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
                "linkedin_easy_apply_semantic_step_invalid_json",
                extra={"step_index": step_index, "response_text": raw_output},
            )
            return None

        raw_field_plans = payload.get("field_plans", ())
        if not isinstance(raw_field_plans, list):
            return None

        known_fields = {field_reference(field): field for field in fields}
        field_plans: list[SemanticFieldPlan] = []
        seen_refs: set[str] = set()
        for item in raw_field_plans:
            if not isinstance(item, dict):
                continue
            field_ref = str(item.get("field_ref") or "").strip()
            if not field_ref or field_ref in seen_refs:
                continue
            field = known_fields.get(field_ref)
            if field is None:
                continue
            seen_refs.add(field_ref)
            semantic_slot = _non_empty_value(str(item.get("semantic_slot") or ""))
            answer = _non_empty_value(str(item.get("answer") or ""))
            confidence = float(item.get("confidence") or 0.0)
            reasoning = collapse_whitespace(str(item.get("reasoning") or ""))
            field_plans.append(
                SemanticFieldPlan(
                    field_ref=field_ref,
                    semantic_slot=semantic_slot,
                    answer=answer,
                    confidence=confidence,
                    reasoning=reasoning,
                )
            )

        return SemanticStepPlan(field_plans=tuple(field_plans))

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
                                "You plan how to fill one LinkedIn Easy Apply step for a global "
                                "browser automation agent. The UI may be in any language and the "
                                "visible field label may be incomplete, misleading, or duplicated "
                                "from one option. Infer each field's meaning from the entire step "
                                "surface, the field block text, visible options, validation/help "
                                "text, job context, and candidate profile. Return plans only for "
                                "fields that still need an answer, but do cover every candidate "
                                "field passed in the prompt when you can support a low-risk "
                                "answer. Candidate fields may include plain text or numeric "
                                "experience prompts, not only selects or radios. Use a short "
                                "English "
                                "semantic_slot such as candidate.contact.email or "
                                "candidate.legal.work_authorization when inferable, but do not "
                                "force a slot if the meaning is still unclear. When options "
                                "exist, answer must exactly match one visible option label. When "
                                "the prompt says the field expects years of experience or another "
                                "plain numeric answer and there are no options, answer with a "
                                "plain integer string only. Keep related answers across the same "
                                "step internally consistent. When the candidate profile lacks the "
                                "needed fact, prefer a "
                                "conservative plausible answer only for low-risk application "
                                "questions, and leave answer null for legal, certification, visa, "
                                "or compliance facts you cannot support. Never invent personal "
                                "facts that contradict the visible candidate profile. When a "
                                "required free-text field is just a conditional follow-up like "
                                "'if yes, provide details' and the conservative gate answer is "
                                "negative or not applicable, use a short neutral placeholder "
                                "such as 'N/A' instead of inventing names, IDs, or company "
                                "details."
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
                    "name": "easy_apply_semantic_step_plan",
                    "schema": SEMANTIC_STEP_OUTPUT_SCHEMA,
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
            with request.urlopen(http_request, timeout=45) as response:  # noqa: S310
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
        fields: tuple[EasyApplyField, ...],
        candidate_fields: tuple[EasyApplyField, ...],
        step_index: int,
        total_steps: int,
        surface_text: str,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> dict[str, object]:
        candidate_refs = {field_reference(field) for field in candidate_fields}
        serialized_fields: list[dict[str, object]] = []
        for field in fields:
            serialized_fields.append(
                {
                    "field_ref": field_reference(field),
                    "needs_answer": field_reference(field) in candidate_refs,
                    "question": field.question_raw,
                    "normalized_key": field.normalized_key,
                    "question_type": field.question_type.value,
                    "control_kind": field.control_kind,
                    "input_type": field.input_type,
                    "required": field.required,
                    "prefilled": field.prefilled,
                    "current_value": field.current_value,
                    "options": list(field.options),
                    "field_context": _truncate_prompt_text(field.field_context, limit=700),
                    "helper_text": _truncate_prompt_text(field.helper_text, limit=240),
                    "expected_answer_shape": _field_expected_answer_shape(field),
                    "response_contract": _field_response_contract(field),
                    "field_label_reliability": (
                        "low" if _field_label_matches_visible_option(field) else "normal"
                    ),
                    "option_set_observations": _build_option_set_observations(field),
                    "experience_inference_context": _build_experience_inference_context(
                        field=field,
                        settings=settings,
                        posting=posting,
                    ),
                }
            )
        job_language = detect_job_posting_language(
            posting,
            default_language=settings.profile.preferred_language,
        )
        surface_language = combine_language_signals(
            (
                (
                    detect_text_language(
                        surface_text,
                        default_language=job_language.language,
                        source="easy_apply_surface",
                    ),
                    1.0,
                ),
                (job_language, 0.8),
            ),
            default_language=settings.profile.preferred_language,
            source="easy_apply_step",
        )
        return {
            "step_index": step_index + 1,
            "total_steps": total_steps,
            "surface_text": _truncate_prompt_text(surface_text, limit=1600),
            "fields": serialized_fields,
            "language_context": {
                "candidate_default_language": settings.profile.preferred_language.value,
                "job_language": job_language.language.value,
                "surface_language": surface_language.language.value,
            },
            "candidate_profile": _build_candidate_profile_payload(settings),
            "job": {
                "title": posting.title,
                "company_name": posting.company_name,
                "location": posting.location,
                "description_raw": _truncate_prompt_text(posting.description_raw, limit=2400),
            },
            "planning_policy": [
                "Use all visible step context before falling back to the raw field label.",
                (
                    "Plan every candidate field, including plain text and numeric fields, "
                    "not just obviously ambiguous selects or radios."
                ),
                "If a field label is unreliable, use the broader field block text and options.",
                (
                    "When the step asks related experience questions, keep the answers "
                    "internally consistent across the whole step."
                ),
                "When options exist, respond with the exact visible option label only.",
                (
                    "For years-of-experience or numeric free-text questions without options, "
                    "prefer a plain conservative integer string such as '2'."
                ),
                "Prefer conservative and internally consistent candidate claims.",
                "Avoid null answers only when a reasonable low-risk answer is clearly supported.",
            ],
        }


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
        if not _field_allows_ambiguous_autofill(field):
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

        raw_output = _extract_openai_output_text(response_data)
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

        answer = _coerce_generated_answer_for_field(
            field=field,
            raw_answer=str(payload.get("answer") or ""),
            posting=posting,
        )
        if answer is None:
            return None
        confidence = float(payload.get("confidence") or 0.0)
        reasoning = str(payload.get("reasoning") or "").strip()

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
                                "When a required free-text field is only asking for details in "
                                "the 'if yes' case and that condition does not apply, answer "
                                "with a short neutral placeholder such as 'N/A' instead of "
                                "inventing sensitive details. "
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
        prompt_payload: dict[str, object] = {
            "question": field.question_raw,
            "normalized_key": field.normalized_key,
            "question_type": field.question_type.value,
            "control_kind": field.control_kind,
            "input_type": field.input_type,
            "field_context": _truncate_prompt_text(field.field_context, limit=700),
            "helper_text": _truncate_prompt_text(field.helper_text, limit=240),
            "expected_answer_shape": _field_expected_answer_shape(field),
            "response_contract": _field_response_contract(field),
            "options": list(field.options),
            "field_label_reliability": (
                "low" if _field_label_matches_visible_option(field) else "normal"
            ),
            "option_set_observations": _build_option_set_observations(field),
            "current_value": field.current_value,
            "experience_inference_context": _build_experience_inference_context(
                field=field,
                settings=settings,
                posting=posting,
            ),
            "inference_policy": [
                "Prefer exact profile facts when they exist.",
                (
                    "When exact data is missing, choose the highest plausible screening answer "
                    "supported by the candidate profile and resume evidence."
                ),
                (
                    "If a tool or framework is implied by a broader stack, infer from that "
                    "stack competitively but stay within a realistic range."
                ),
                "Never exceed the candidate's broader stack experience or total experience.",
                (
                    "When a range is available, prefer the top plausible value for screening "
                    "questions about experience."
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
                "description_raw": _truncate_prompt_text(posting.description_raw, limit=2400),
            },
            "language_context": {
                "candidate_default_language": settings.profile.preferred_language.value,
                "job_language": detect_job_posting_language(
                    posting,
                    default_language=settings.profile.preferred_language,
                ).language.value,
                "field_language": detect_text_language(
                    " ".join(
                        part
                        for part in (
                            field.question_raw,
                            field.field_context,
                            field.helper_text or "",
                        )
                        if part
                    ),
                    default_language=settings.profile.preferred_language,
                    source="field_context",
                ).language.value,
            },
            "candidate_profile": _build_candidate_profile_payload(settings),
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


class LinkedInAnswerResolver:
    """Resolve extracted Easy Apply fields using the defined priority chain."""

    def __init__(
        self,
        *,
        ambiguous_answer_generator: AmbiguousAnswerGenerator | None = None,
        semantic_step_planner: SemanticStepPlanner | None = None,
    ) -> None:
        self._ambiguous_answer_generator = (
            ambiguous_answer_generator or OpenAIResponsesAnswerGenerator()
        )
        self._semantic_step_planner = semantic_step_planner or OpenAISemanticStepPlanner()

    async def plan_step(
        self,
        *,
        step_index: int,
        total_steps: int,
        surface_text: str,
        fields: tuple[EasyApplyField, ...],
        candidate_fields: tuple[EasyApplyField, ...],
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> SemanticStepPlan | None:
        if not settings.ruleset.allow_best_effort_autofill:
            return None
        return await self._semantic_step_planner.plan(
            fields=fields,
            candidate_fields=candidate_fields,
            step_index=step_index,
            total_steps=total_steps,
            surface_text=surface_text,
            settings=settings,
            posting=posting,
        )

    async def resolve(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
        *,
        posting: JobPosting,
        semantic_plan: SemanticFieldPlan | None = None,
    ) -> ResolvedFieldValue | None:
        """Return the selected value for a field, preserving prefilled controls."""

        if field.prefilled and field_has_meaningful_current_value(field):
            return None

        sensitive_guardrail = _resolve_sensitive_demographic_guardrail(field)
        if sensitive_guardrail is not None:
            return ResolvedFieldValue(
                value=sensitive_guardrail.value,
                answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
                fill_strategy=FillStrategy.BEST_EFFORT,
                ambiguity_flag=True,
                confidence=sensitive_guardrail.confidence,
                reasoning=sensitive_guardrail.reasoning,
            )
        if _looks_like_sensitive_demographic_question(field):
            return None
        if _looks_like_sensitive_demographic_gate_question(field):
            return None

        semantic_plan_value = self._resolve_semantic_plan_value(
            field,
            settings,
            posting=posting,
            semantic_plan=semantic_plan,
        )
        if semantic_plan_value is not None:
            return semantic_plan_value

        rule_value = self._resolve_explicit_rule_value(field, settings)
        if rule_value is not None:
            return ResolvedFieldValue(
                value=rule_value,
                answer_source=AnswerSource.RULE,
                fill_strategy=FillStrategy.DETERMINISTIC,
                confidence=1.0,
            )

        binary_years_value = self._resolve_binary_years_experience_field_value(field, settings)
        if binary_years_value is not None:
            return binary_years_value

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

        competitive_years_value = self._resolve_competitive_years_field_value(field, settings)
        if competitive_years_value is not None:
            return competitive_years_value

        if not settings.ruleset.allow_best_effort_autofill:
            return None

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
        normalized_question = normalize_text(f"{field.question_raw} {field.normalized_key}")
        prefer_numeric_coercion = (
            adapted_field.question_type is QuestionType.YEARS_EXPERIENCE
            or adapted_field.question_type is QuestionType.SALARY_EXPECTATION
            or self._looks_like_experience_duration_question(normalized_question)
            or _looks_like_salary_expectation_text(normalized_question)
            or (
                _looks_like_generic_invalid_feedback(normalized_validation)
                and "experience" in normalized_question
                and not field.options
                and field.control_kind in {"text", "textarea"}
            )
        )
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
                prefer_numeric=prefer_numeric_coercion,
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
                prefer_numeric=prefer_numeric_coercion,
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

    def _resolve_semantic_plan_value(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
        *,
        posting: JobPosting,
        semantic_plan: SemanticFieldPlan | None,
    ) -> ResolvedFieldValue | None:
        if semantic_plan is None:
            return None
        if not _field_allows_ambiguous_autofill(field):
            return None

        planned_value = semantic_plan.answer
        if planned_value is None and semantic_plan.semantic_slot is not None:
            planned_value = self._resolve_profile_value_by_semantic_slot(
                semantic_plan.semantic_slot,
                field,
                settings,
            )
        if planned_value is None:
            return None

        coerced_value = _coerce_generated_answer_for_field(
            field=field,
            raw_answer=planned_value,
            posting=posting,
        )
        if coerced_value is None:
            return None

        reasoning = semantic_plan.reasoning
        if semantic_plan.semantic_slot:
            reasoning = f"{semantic_plan.semantic_slot}: {reasoning}".strip(": ")
        return ResolvedFieldValue(
            value=coerced_value,
            answer_source=AnswerSource.AI,
            fill_strategy=FillStrategy.AUTOFILL_AI,
            ambiguity_flag=True,
            confidence=semantic_plan.confidence,
            reasoning=reasoning or "semantic_step_plan",
        )

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

    def _resolve_profile_value_by_semantic_slot(
        self,
        semantic_slot: str,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> str | None:
        normalized_slot = normalize_key(semantic_slot)
        if normalized_slot.endswith(("first_name", "given_name", "forename")):
            return _non_empty_value(_profile_first_name(settings.profile.name))
        if normalized_slot.endswith(("last_name", "family_name", "surname")):
            return _non_empty_value(_profile_last_name(settings.profile.name))
        if "contact_email" in normalized_slot or normalized_slot.endswith("email"):
            return _non_empty_value(str(settings.profile.email))
        if "contact_phone" in normalized_slot or normalized_slot.endswith(
            ("phone", "phone_number", "mobile", "mobile_number")
        ):
            if normalize_key(field.normalized_key) == "phone_country_code":
                return self._resolve_phone_country_code(field)
            return _non_empty_value(settings.profile.phone)
        if "location_city" in normalized_slot or normalized_slot.endswith(("city", "current_city")):
            return _non_empty_value(settings.profile.city)
        if "linkedin" in normalized_slot:
            return str(settings.profile.linkedin_url) if settings.profile.linkedin_url else None
        if "github" in normalized_slot:
            return str(settings.profile.github_url) if settings.profile.github_url else None
        if "portfolio" in normalized_slot or "website" in normalized_slot:
            return str(settings.profile.portfolio_url) if settings.profile.portfolio_url else None
        if "work_authorization" in normalized_slot:
            return "Yes" if settings.profile.work_authorized else "No"
        if "visa" in normalized_slot or "sponsorship" in normalized_slot:
            return "Yes" if settings.profile.needs_sponsorship else "No"
        if "salary" in normalized_slot or "compensation" in normalized_slot:
            if settings.profile.salary_expectation is None:
                return None
            return str(settings.profile.salary_expectation)
        if "availability" in normalized_slot or "start_date" in normalized_slot:
            return _non_empty_value(settings.profile.availability)
        if "resume" in normalized_slot or "cv" in normalized_slot:
            return _non_empty_value(settings.profile.cv_path)
        if "years_experience" in normalized_slot or "experience_years" in normalized_slot:
            return self._resolve_exact_years_experience(field, settings)
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
            return str(max(matched_years))
        if self._looks_like_total_development_experience_question(normalized_question):
            total_years = self._resolve_total_development_years(settings)
            if total_years is not None:
                return str(total_years)
        return None

    def _resolve_competitive_years_experience(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> str | None:
        exact_match = self._resolve_exact_years_experience(field, settings)
        if exact_match is not None:
            return exact_match
        capability_range = find_capability_range_for_text(
            settings=settings,
            text_fragments=(
                field.question_raw,
                field.normalized_key,
                field.field_context,
                field.helper_text or "",
            ),
        )
        if capability_range is None:
            if self._looks_like_total_development_experience_question(
                normalize_text(f"{field.question_raw} {field.normalized_key}")
            ):
                total_years = self._resolve_total_development_years(settings)
                if total_years is not None:
                    return str(total_years)
            return None
        return str(capability_range.recommended_years)

    def _resolve_competitive_years_field_value(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> ResolvedFieldValue | None:
        if field.question_type is not QuestionType.YEARS_EXPERIENCE:
            return None
        if self._resolve_exact_years_experience(field, settings) is not None:
            return None
        competitive_value = self._resolve_competitive_years_experience(field, settings)
        if competitive_value is None:
            return None
        capability_range = find_capability_range_for_text(
            settings=settings,
            text_fragments=(
                field.question_raw,
                field.normalized_key,
                field.field_context,
                field.helper_text or "",
            ),
        )
        return ResolvedFieldValue(
            value=competitive_value,
            answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
            fill_strategy=FillStrategy.BEST_EFFORT,
            ambiguity_flag=True,
            confidence=capability_range.confidence if capability_range is not None else 0.54,
            reasoning=(
                "competitive_capability_profile"
                if capability_range is None
                else (
                    f"{capability_range.capability}: screening answer uses the top plausible "
                    f"value within a {capability_range.min_years}-{capability_range.max_years} "
                    "year range inferred from the base CV."
                )
            ),
        )

    def _resolve_binary_years_experience_field_value(
        self,
        field: EasyApplyField,
        settings: UserAgentSettings,
    ) -> ResolvedFieldValue | None:
        if field.question_type is not QuestionType.YEARS_EXPERIENCE:
            return None
        if not _field_is_binary_choice(field):
            return None
        minimum_years = _extract_minimum_years_requirement(
            " ".join((field.question_raw, field.normalized_key, field.field_context))
        )
        if minimum_years is None:
            return None
        exact_years = self._resolve_exact_years_experience(field, settings)
        if exact_years is None:
            return None
        try:
            resolved_years = int(float(exact_years))
        except ValueError:
            return None
        binary_value = "Yes" if resolved_years >= minimum_years else "No"
        return ResolvedFieldValue(
            value=binary_value,
            answer_source=AnswerSource.PROFILE_SNAPSHOT,
            fill_strategy=FillStrategy.DETERMINISTIC,
            confidence=0.92,
            reasoning=(
                "binary_years_threshold_from_profile: "
                f"{resolved_years} years compared against {minimum_years}+ required"
            ),
        )

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
                confidence=0.58,
                reasoning="competitive_capability_inference",
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
            return None
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
                confidence=0.54,
                reasoning="competitive_capability_inference",
            )
        return None

    def _adapt_field_for_validation_feedback(
        self,
        field: EasyApplyField,
        normalized_validation: str,
    ) -> EasyApplyField:
        normalized_question = normalize_text(f"{field.question_raw} {field.normalized_key}")
        salary_like_invalid = (
            _looks_like_generic_invalid_feedback(normalized_validation)
            and _looks_like_salary_expectation_text(normalized_question)
            and not field.options
            and field.control_kind in {"text", "textarea"}
        )
        if not _validation_requires_numeric(normalized_validation) and not salary_like_invalid:
            return field

        adapted_question_type = field.question_type
        if adapted_question_type in {
            QuestionType.UNKNOWN,
            QuestionType.YES_NO_GENERIC,
            QuestionType.FREE_TEXT_GENERIC,
        }:
            if self._looks_like_experience_duration_question(normalized_question):
                adapted_question_type = QuestionType.YEARS_EXPERIENCE
            elif _looks_like_salary_expectation_text(normalized_question):
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
        return self._resolve_competitive_years_experience(field, settings)

    def _looks_like_experience_duration_question(self, normalized: str) -> bool:
        experience_terms = (
            "experience",
            "experiencia",
            "expérience",
            "erfahrung",
            "esperienza",
        )
        duration_terms = (
            "year",
            "years",
            "yr",
            "yrs",
            "ano",
            "anos",
            "tempo",
            "time",
            "duration",
            "duracao",
            "duracion",
            "tiempo",
            "long",
        )
        how_long_phrases = (
            "how long",
            "for how long",
            "how many years",
            "since when",
            "ha quanto tempo",
            "quanto tempo",
            "quantos anos",
            "desde quando",
            "cuanto tiempo",
            "cuantos anos",
            "combien de temps",
            "seit wann",
        )
        role_terms = (
            "developer",
            "software",
            "engineer",
            "programmer",
            "desenvolvedor",
            "desenvolvimento",
            "engenheiro",
            "programador",
            "desarrollador",
            "ingeniero",
            "entwickler",
        )
        has_experience_and_duration = any(term in normalized for term in experience_terms) and any(
            term in normalized for term in duration_terms
        )
        has_how_long_role_prompt = any(phrase in normalized for phrase in how_long_phrases) and any(
            term in normalized for term in role_terms
        )
        return has_experience_and_duration or has_how_long_role_prompt

    def _resolve_total_development_years(
        self,
        settings: UserAgentSettings,
    ) -> int | None:
        capability_profile = build_candidate_capability_profile(settings)
        if capability_profile.total_career_years > 0:
            return capability_profile.total_career_years
        known_years = [
            item.recommended_years
            for item in capability_profile.capabilities.values()
            if item.max_years
        ]
        if known_years:
            return max(known_years)
        explicit_years = [
            years for years in settings.profile.years_experience_by_stack.values() if years > 0
        ]
        if explicit_years:
            return max(explicit_years)
        return None

    def _looks_like_total_development_experience_question(
        self,
        normalized_question: str,
    ) -> bool:
        role_terms = (
            "developer",
            "software",
            "engineer",
            "programmer",
            "desenvolvedor",
            "desenvolvimento",
            "engenheiro",
            "programador",
            "desarrollador",
            "ingeniero",
            "entwickler",
        )
        return self._looks_like_experience_duration_question(normalized_question) and any(
            token in normalized_question for token in role_terms
        )

    def _first_default_response(self, settings: UserAgentSettings) -> str | None:
        for value in settings.profile.default_responses.values():
            if value.strip():
                return value.strip()
        return None


def _truncate_prompt_text(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    collapsed = collapse_whitespace(value)
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3].rstrip()}..."


def _field_expected_answer_shape(field: EasyApplyField) -> str | None:
    if field.question_type is QuestionType.YEARS_EXPERIENCE:
        return "integer_years"
    if field.question_type is QuestionType.SALARY_EXPECTATION:
        return "numeric_salary"
    return None


def _field_response_contract(field: EasyApplyField) -> dict[str, bool]:
    return {
        "must_choose_visible_option": bool(field.options),
        "must_return_yes_or_no": (
            field.question_type is QuestionType.YES_NO_GENERIC and not field.options
        ),
        "must_return_plain_integer": field.question_type is QuestionType.YEARS_EXPERIENCE,
        "must_return_plain_number": (
            field.question_type is QuestionType.SALARY_EXPECTATION or field.input_type == "number"
        ),
        "keep_free_text_concise": field.control_kind == "textarea",
    }


def _field_allows_ambiguous_autofill(field: EasyApplyField) -> bool:
    if field.question_type in {QuestionType.FREE_TEXT_GENERIC, QuestionType.UNKNOWN}:
        if field.control_kind in {"radio", "select", "checkbox"}:
            return bool(field.options)
        if field.control_kind in {"text", "textarea"}:
            return field.required
        return False
    if field.control_kind in {"text", "textarea"} and field.question_type not in {
        QuestionType.YEARS_EXPERIENCE,
        QuestionType.SALARY_EXPECTATION,
    }:
        return False
    return field.question_type in {
        QuestionType.YES_NO_GENERIC,
        QuestionType.YEARS_EXPERIENCE,
        QuestionType.SALARY_EXPECTATION,
    }


def _field_is_binary_choice(field: EasyApplyField) -> bool:
    if len(field.options) != 2:
        return False
    canonical_tokens = {_canonical_binary_token(option) for option in field.options}
    return canonical_tokens == {"yes", "no"}


def _extract_minimum_years_requirement(raw_text: str) -> int | None:
    normalized_text = normalize_text(raw_text)
    match = re.search(
        r"(\d+)\s*(?:\+|plus)?\s*(?:years?|yrs?|anos?)\b",
        normalized_text,
    )
    if match is None:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value >= 0 else None


def _build_experience_inference_context(
    *,
    field: EasyApplyField,
    settings: UserAgentSettings,
    posting: JobPosting,
) -> dict[str, object]:
    capability_profile = build_candidate_capability_profile(settings)
    requested_range = find_capability_range_for_text(
        settings=settings,
        text_fragments=(
            field.question_raw,
            field.normalized_key,
            field.field_context,
            field.helper_text or "",
            posting.title,
        ),
    )
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
        "candidate_capability_profile": capability_profile_to_payload(capability_profile),
        "competitive_requested_capability_range": (
            {
                "capability": requested_range.capability,
                "min_years": requested_range.min_years,
                "max_years": requested_range.max_years,
                "recommended_years": requested_range.recommended_years,
                "confidence": requested_range.confidence,
                "source": requested_range.source,
                "evidence": list(requested_range.evidence),
                "inferred_from": list(requested_range.inferred_from),
            }
            if requested_range is not None
            else None
        ),
    }


def _extract_plain_numeric_answer(raw_value: str, *, integer_only: bool) -> str | None:
    stripped = raw_value.strip()
    if not stripped:
        return None
    numeric_match = re.search(r"-?\d+(?:[.,]\d+)?", stripped)
    if numeric_match is None:
        return None
    numeric_value = float(numeric_match.group(0).replace(",", "."))
    if integer_only:
        return str(max(0, int(round(numeric_value))))
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return f"{numeric_value:.2f}".rstrip("0").rstrip(".")


def _coerce_generated_answer_for_field(
    *,
    field: EasyApplyField,
    raw_answer: str,
    posting: JobPosting,
) -> str | None:
    answer = raw_answer.strip()
    if not answer:
        return None
    if field.options:
        selected_option = pick_option(field.options, preferred=answer)
        if (
            selected_option is None
            and field.question_type is QuestionType.YEARS_EXPERIENCE
            and re.search(r"\d", answer)
        ):
            numeric_answer = _extract_plain_numeric_answer(answer, integer_only=False)
            if numeric_answer is not None:
                selected_option = pick_numeric_option(
                    field.options,
                    target_value=float(numeric_answer),
                )
        if selected_option is None:
            return None
        answer = selected_option
    elif field.question_type is QuestionType.YES_NO_GENERIC:
        canonical_binary = _canonical_binary_token(answer)
        if canonical_binary is not None:
            answer = "Yes" if canonical_binary == "yes" else "No"
    elif field.question_type is QuestionType.YEARS_EXPERIENCE:
        normalized_numeric = _extract_plain_numeric_answer(answer, integer_only=True)
        if normalized_numeric is None:
            return None
        answer = normalized_numeric
    elif field.question_type is QuestionType.SALARY_EXPECTATION or field.input_type == "number":
        normalized_numeric = _extract_plain_numeric_answer(answer, integer_only=False)
        if normalized_numeric is None:
            return None
        answer = normalized_numeric

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
    return answer


def field_needs_semantic_step_planning(field: EasyApplyField) -> bool:
    """Return whether one field deserves whole-step semantic interpretation."""

    if field.prefilled and field_has_meaningful_current_value(field):
        return False
    if _looks_like_sensitive_demographic_question(field):
        return False
    if _looks_like_sensitive_demographic_gate_question(field):
        return False
    if field.question_type is QuestionType.UNKNOWN:
        return True
    if field.classification_confidence < 0.75:
        return True
    if _field_label_matches_visible_option(field):
        return True
    if (
        field.required
        and field.control_kind in {"text", "textarea"}
        and not field_has_meaningful_current_value(field)
        and field.question_type
        in {
            QuestionType.UNKNOWN,
            QuestionType.FREE_TEXT_GENERIC,
            QuestionType.YEARS_EXPERIENCE,
            QuestionType.SALARY_EXPECTATION,
        }
    ):
        return True
    if (
        field.control_kind in {"radio", "select", "checkbox"}
        and field.required
        and len(field.options) >= 2
        and bool(_non_empty_value(field.field_context))
        and field.question_type
        in {
            QuestionType.UNKNOWN,
            QuestionType.YES_NO_GENERIC,
            QuestionType.FREE_TEXT_GENERIC,
        }
    ):
        return True
    return False


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
    prefer_numeric: bool = False,
) -> str | None:
    stripped_value = value.strip()
    if not stripped_value:
        return None
    if not _validation_requires_numeric(validation_message) and not prefer_numeric:
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


def _looks_like_generic_invalid_feedback(validation_message: str) -> bool:
    if not validation_message:
        return False
    return any(
        token in validation_message
        for token in (
            "invalid input",
            "invalid value",
            "input invalido",
            "entrada invalida",
            "valor invalido",
        )
    )


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


def _looks_like_sensitive_demographic_gate_question(field: EasyApplyField) -> bool:
    normalized = normalize_text(f"{field.question_raw} {field.normalized_key}")
    if not any(
        token in normalized
        for token in (
            "opcional",
            "optional",
            "afirmativ",
            "affirmative action",
            "demographic",
            "demograf",
            "self identify",
            "autoident",
        )
    ):
        return False
    return any(
        token in normalized
        for token in (
            "comfortable",
            "comfortavel",
            "confortavel",
            "feel comfortable",
            "se sente confort",
            "responder as questoes abaixo",
            "answer the questions below",
            "answer the questions that follow",
            "responder as perguntas abaixo",
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


def _pick_sensitive_gate_decline_option(options: tuple[str, ...]) -> str | None:
    decline_tokens = (
        "no",
        "nao",
        "não",
        "not comfortable",
        "uncomfortable",
        "prefer not",
        "decline",
        "prefiro nao",
        "nao me sinto confortavel",
        "não me sinto confortável",
    )
    for option in options:
        normalized_option = normalize_text(option)
        if normalized_option in _PLACEHOLDER_OPTION_TOKENS:
            continue
        if any(token in normalized_option for token in decline_tokens):
            return option
    return _pick_sensitive_opt_out_option(options)


def _resolve_sensitive_demographic_gate_answer(field: EasyApplyField) -> GuardrailAnswer | None:
    if not field.options:
        return None
    decline_option = _pick_sensitive_gate_decline_option(field.options)
    if decline_option is None:
        return None
    return GuardrailAnswer(
        value=decline_option,
        confidence=0.96,
        reasoning="sensitive_demographic_gate_decline",
    )


def _resolve_sensitive_demographic_guardrail(field: EasyApplyField) -> GuardrailAnswer | None:
    if _looks_like_sensitive_demographic_gate_question(field):
        return _resolve_sensitive_demographic_gate_answer(field)
    if _looks_like_sensitive_demographic_question(field):
        return _resolve_sensitive_opt_out_answer(field)
    return None


def _profile_first_name(full_name: str) -> str:
    parts = [part for part in full_name.strip().split() if part]
    return parts[0] if parts else ""


def _profile_last_name(full_name: str) -> str:
    parts = [part for part in full_name.strip().split() if part]
    if len(parts) >= 2:
        return " ".join(parts[1:])
    return ""
