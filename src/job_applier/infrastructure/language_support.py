"""Language detection and localization helpers for the MVP."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import SupportedLanguage

_LANGUAGE_DISPLAY_NAMES: dict[SupportedLanguage, str] = {
    SupportedLanguage.ENGLISH: "English",
    SupportedLanguage.PORTUGUESE: "Portuguese",
}

_RESUME_SECTION_LABELS: dict[SupportedLanguage, dict[str, str]] = {
    SupportedLanguage.ENGLISH: {
        "summary": "Summary",
        "experience": "Experience",
        "certifications": "Certifications",
        "education": "Education",
        "skills": "Skills",
    },
    SupportedLanguage.PORTUGUESE: {
        "summary": "Resumo",
        "experience": "Experiência",
        "certifications": "Certificações",
        "education": "Educação",
        "skills": "Competências",
    },
}

_FIELD_LABELS: dict[SupportedLanguage, dict[str, str]] = {
    SupportedLanguage.ENGLISH: {
        "linkedin": "LinkedIn",
        "github": "GitHub",
        "current_location": "Current location",
        "current_file": "Current file",
    },
    SupportedLanguage.PORTUGUESE: {
        "linkedin": "LinkedIn",
        "github": "GitHub",
        "current_location": "Localização atual",
        "current_file": "Arquivo atual",
    },
}

_SKILL_CATEGORY_LABELS: dict[SupportedLanguage, dict[str, str]] = {
    SupportedLanguage.ENGLISH: {
        "core languages": "Core Languages",
        "full stack & backend": "Full Stack & Backend",
        "engineering practices": "Engineering Practices",
        "applied ai & automation": "Applied AI & Automation",
        "tools & platforms": "Tools & Platforms",
        "cloud & platforms": "Cloud & Platforms",
        "interests": "Interests",
    },
    SupportedLanguage.PORTUGUESE: {
        "core languages": "Linguagens Principais",
        "full stack & backend": "Full Stack e Backend",
        "engineering practices": "Práticas de Engenharia",
        "applied ai & automation": "IA Aplicada e Automação",
        "tools & platforms": "Ferramentas e Plataformas",
        "cloud & platforms": "Nuvem e Plataformas",
        "interests": "Interesses",
    },
}

_SECTION_TITLE_ALIASES: dict[str, str] = {
    "summary": "summary",
    "professional summary": "summary",
    "resumo": "summary",
    "perfil": "summary",
    "experience": "experience",
    "professional experience": "experience",
    "work experience": "experience",
    "experiencia": "experience",
    "experiencia profissional": "experience",
    "certifications": "certifications",
    "certification": "certifications",
    "certificacao": "certifications",
    "certificacoes": "certifications",
    "education": "education",
    "educacao": "education",
    "formacao": "education",
    "academic background": "education",
    "skills": "skills",
    "technical skills": "skills",
    "competencias": "skills",
    "habilidades": "skills",
}

_PORTUGUESE_HINT_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\bcom\b", 0.4),
    (r"\bpara\b", 0.4),
    (r"\buma\b", 0.4),
    (r"\bque\b", 0.35),
    (r"\bnao\b", 0.35),
    (r"\banos?\b", 0.4),
    (r"\bvaga\b", 0.9),
    (r"\bremoto\b", 0.7),
    (r"\bexperiencia\b", 0.9),
    (r"\bexperiencias\b", 0.8),
    (r"\bengenheir[oa]\b", 1.2),
    (r"\bdesenvolvedor(?:a)?\b", 1.2),
    (r"\btrabalho\b", 0.6),
    (r"\brequisitos\b", 0.8),
    (r"\bresponsabilidades\b", 0.8),
    (r"\bconhecimento\b", 0.6),
    (r"\bidioma\b", 0.7),
    (r"\bcurriculo\b", 0.8),
)

_ENGLISH_HINT_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"\bwith\b", 0.35),
    (r"\band\b", 0.35),
    (r"\bfor\b", 0.35),
    (r"\bthe\b", 0.35),
    (r"\byears?\b", 0.4),
    (r"\brole\b", 0.8),
    (r"\bremote\b", 0.7),
    (r"\bexperience\b", 0.9),
    (r"\bengineer\b", 1.2),
    (r"\bdeveloper\b", 1.2),
    (r"\bwork\b", 0.6),
    (r"\brequirements\b", 0.8),
    (r"\bresponsibilities\b", 0.8),
    (r"\blanguage\b", 0.7),
    (r"\bresume\b", 0.8),
)

_PORTUGUESE_DIACRITIC_PATTERN = re.compile(r"[ãõáéíóúâêôç]")


@dataclass(frozen=True, slots=True)
class LanguageDetectionResult:
    """Resolved language signal plus confidence and evidence."""

    language: SupportedLanguage
    confidence: float
    source: str
    evidence: tuple[str, ...] = ()


def display_name_for_language(language: SupportedLanguage) -> str:
    """Return the product-facing language name."""

    return _LANGUAGE_DISPLAY_NAMES.get(language, "English")


def localized_section_label(section_key: str, language: SupportedLanguage) -> str:
    """Return one canonical resume section label in the requested language."""

    labels = _RESUME_SECTION_LABELS.get(language, _RESUME_SECTION_LABELS[SupportedLanguage.ENGLISH])
    return labels.get(section_key, section_key.title())


def localized_field_label(label_key: str, language: SupportedLanguage) -> str:
    """Return one short UI label in the requested language."""

    labels = _FIELD_LABELS.get(language, _FIELD_LABELS[SupportedLanguage.ENGLISH])
    return labels.get(label_key, label_key.replace("_", " ").title())


def localized_skill_category_label(raw_label: str, language: SupportedLanguage) -> str:
    """Return one localized skill bucket label when the category is known."""

    normalized = _normalize_language_text(raw_label)
    labels = _SKILL_CATEGORY_LABELS.get(language, _SKILL_CATEGORY_LABELS[SupportedLanguage.ENGLISH])
    return labels.get(normalized, raw_label.strip())


def canonical_resume_section_title(raw_title: str) -> str | None:
    """Map visible section titles into canonical internal section keys."""

    normalized = _normalize_language_text(raw_title)
    return _SECTION_TITLE_ALIASES.get(normalized)


def detect_text_language(
    text: str,
    *,
    default_language: SupportedLanguage = SupportedLanguage.ENGLISH,
    source: str = "text",
) -> LanguageDetectionResult:
    """Infer whether free text is closer to English or Portuguese."""

    normalized = _normalize_language_text(text)
    if not normalized:
        return LanguageDetectionResult(
            language=default_language,
            confidence=0.0,
            source=source,
            evidence=("empty_text_default",),
        )

    english_score = _score_language_patterns(normalized, _ENGLISH_HINT_PATTERNS)
    portuguese_score = _score_language_patterns(normalized, _PORTUGUESE_HINT_PATTERNS)
    if _PORTUGUESE_DIACRITIC_PATTERN.search(text):
        portuguese_score += 0.9

    dominant_language = default_language
    dominant_score = (
        english_score if default_language is SupportedLanguage.ENGLISH else portuguese_score
    )
    alternate_score = (
        portuguese_score if dominant_language is SupportedLanguage.ENGLISH else english_score
    )
    evidence: list[str] = []

    if portuguese_score > english_score + 0.25:
        dominant_language = SupportedLanguage.PORTUGUESE
        dominant_score = portuguese_score
        alternate_score = english_score
        evidence.append("portuguese_pattern_bias")
    elif english_score > portuguese_score + 0.25:
        dominant_language = SupportedLanguage.ENGLISH
        dominant_score = english_score
        alternate_score = portuguese_score
        evidence.append("english_pattern_bias")
    else:
        evidence.append("default_language_fallback")

    confidence = 0.0
    if dominant_score > 0.0:
        confidence = min(1.0, max(0.15, (dominant_score - alternate_score + 0.5) / 4.0))
    if len(normalized.split()) >= 12:
        confidence = min(1.0, confidence + 0.1)

    return LanguageDetectionResult(
        language=dominant_language,
        confidence=round(confidence, 3),
        source=source,
        evidence=tuple(evidence),
    )


def detect_job_posting_language(
    posting: JobPosting,
    *,
    default_language: SupportedLanguage = SupportedLanguage.ENGLISH,
) -> LanguageDetectionResult:
    """Infer the target language from the job title and description."""

    description_signal = detect_text_language(
        posting.description_raw,
        default_language=default_language,
        source="job_description",
    )
    title_signal = detect_text_language(
        posting.title,
        default_language=default_language,
        source="job_title",
    )
    title_weight = 1.25
    description_weight = 1.0
    if posting.detail_description_score < 0.45:
        title_weight = 1.75
        description_weight = 0.2
    elif posting.detail_description_score < 0.7:
        title_weight = 1.5
        description_weight = 0.6
    return combine_language_signals(
        (
            (title_signal, title_weight),
            (description_signal, description_weight),
        ),
        default_language=default_language,
        source="job_posting",
    )


def combine_language_signals(
    weighted_signals: tuple[tuple[LanguageDetectionResult, float], ...],
    *,
    default_language: SupportedLanguage,
    source: str,
) -> LanguageDetectionResult:
    """Combine multiple language hints into one resolved target language."""

    english_score = 0.0
    portuguese_score = 0.0
    evidence: list[str] = []
    for signal, weight in weighted_signals:
        if signal.language is SupportedLanguage.PORTUGUESE:
            portuguese_score += max(0.1, signal.confidence or 0.1) * weight
        else:
            english_score += max(0.1, signal.confidence or 0.1) * weight
        evidence.extend(signal.evidence[:2])

    if portuguese_score > english_score + 0.15:
        language = SupportedLanguage.PORTUGUESE
        confidence = min(1.0, max(0.2, (portuguese_score - english_score + 0.25) / 2.0))
    elif english_score > portuguese_score + 0.15:
        language = SupportedLanguage.ENGLISH
        confidence = min(1.0, max(0.2, (english_score - portuguese_score + 0.25) / 2.0))
    else:
        language = default_language
        confidence = 0.2
        evidence.append("weighted_default_fallback")

    return LanguageDetectionResult(
        language=language,
        confidence=round(confidence, 3),
        source=source,
        evidence=tuple(dict.fromkeys(evidence)),
    )


def _score_language_patterns(
    normalized_text: str,
    patterns: tuple[tuple[str, float], ...],
) -> float:
    score = 0.0
    for pattern, weight in patterns:
        matches = re.findall(pattern, normalized_text)
        if matches:
            score += min(3.0, len(matches)) * weight
    return score


def _normalize_language_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    collapsed = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_text.lower())
    return re.sub(r"\s{2,}", " ", collapsed).strip()
