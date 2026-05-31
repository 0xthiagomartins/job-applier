"""Dynamic CV generation compatible with Oh-My-CV markdown notation."""

from __future__ import annotations

import html
import json
import logging
import re
import shlex
import shutil
import subprocess
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from uuid import UUID

import yaml  # type: ignore[import-untyped]

from job_applier.application.config import UserAgentSettings
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import ResumeMode, SupportedLanguage
from job_applier.infrastructure.candidate_capabilities import (
    build_candidate_capability_profile,
    capability_profile_to_payload,
)
from job_applier.infrastructure.language_support import (
    LanguageDetectionResult,
    canonical_resume_section_title,
    combine_language_signals,
    detect_job_posting_language,
    detect_text_language,
    display_name_for_language,
    localized_field_label,
    localized_section_label,
    localized_skill_category_label,
)
from job_applier.resume_theme import DEFAULT_OH_MY_CV_RESUME_CSS
from job_applier.settings import RuntimeSettings

logger = logging.getLogger(__name__)

_RESUME_ADAPTATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string", "minLength": 1},
        "focus_keywords": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 12,
        },
        "skill_focus": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 12,
        },
        "experience_focus": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entry_hint": {"type": "string", "minLength": 1},
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 8,
                    },
                },
                "required": ["entry_hint", "keywords"],
                "additionalProperties": False,
            },
            "maxItems": 12,
        },
        "adaptation_summary": {"type": "string"},
    },
    "required": [
        "headline",
        "summary",
        "focus_keywords",
        "skill_focus",
        "experience_focus",
        "adaptation_summary",
    ],
    "additionalProperties": False,
}

_RESUME_TRANSLATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "minLength": 1},
                    "text": {"type": "string", "minLength": 1},
                },
                "required": ["ref", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

_TARGET_KEYWORD_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("automation engineer", (r"\bautomation (engineer|developer|specialist)\b",)),
    ("automation developer", (r"\bautomation developer\b",)),
    ("rpa developer", (r"\brpa\b", r"\brobotic process automation\b")),
    ("backend developer", (r"\bbackend\b", r"\bserver[\s\-]?side\b")),
    ("full stack developer", (r"\bfull[\s\-]?stack\b", r"\bfullstack\b")),
    ("full stack", (r"\bfull[\s\-]?stack\b",)),
    ("backend", (r"\bbackend\b",)),
    ("frontend", (r"\bfrontend\b",)),
    ("rpa", (r"\brpa\b", r"\brobotic process automation\b")),
    ("uipath", (r"\buipath\b",)),
    ("react native", (r"\breact[\s\-]?native\b",)),
    ("typescript", (r"\btypescript\b",)),
    ("javascript", (r"\bjavascript\b", r"\bnode\.?js\b")),
    ("python", (r"\bpython\b",)),
    ("java", (r"\bjava\b",)),
    ("langchain", (r"\blangchain\b",)),
    ("llm", (r"\bllm\b",)),
    ("rag", (r"\brag\b", r"\bretrieval[\s\-]augmented generation\b")),
    ("applied ai", (r"\bapplied ai\b",)),
    ("ai", (r"\bartificial intelligence\b", r"\bapplied ai\b", r"\bai\b")),
    ("fastapi", (r"\bfastapi\b",)),
    ("react", (r"\breact\b",)),
    ("expo", (r"\bexpo\b",)),
    ("mobile", (r"\bmobile\b", r"\bios\b", r"\bandroid\b")),
    ("api", (r"\bapis?\b",)),
    ("automation", (r"\bautomation\b",)),
    ("workflow automation", (r"\bworkflow automation\b", r"\bworkflows?\b")),
    ("process orchestration", (r"\bprocess orchestration\b", r"\borchestration\b")),
    ("system integrations", (r"\bsystem integrations?\b", r"\bintegrations?\b")),
    ("microservices", (r"\bmicroservices?\b",)),
    ("observability", (r"\bobservability\b",)),
    ("database modeling", (r"\bdatabase modeling\b",)),
    ("internal tools", (r"\binternal tools?\b",)),
    ("ai-assisted workflows", (r"\bai[\s\-]?assisted workflows?\b", r"\bai[\s\-]?assisted\b")),
    ("chatbot systems", (r"\bchatbot systems?\b", r"\bchatbots?\b")),
    ("aws", (r"\baws\b",)),
    ("gcp", (r"\bgcp\b", r"\bgoogle cloud\b")),
    ("azure", (r"\bazure\b",)),
    ("docker", (r"\bdocker\b",)),
    ("kubernetes", (r"\bkubernetes\b", r"\bk8s\b")),
)

_ROLE_TARGET_PROFILE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "automation engineer": (
        "automation",
        "workflow automation",
        "system integrations",
        "process orchestration",
        "observability",
    ),
    "automation developer": (
        "automation",
        "workflow automation",
        "system integrations",
        "process orchestration",
        "observability",
    ),
    "rpa developer": (
        "automation",
        "workflow automation",
        "system integrations",
        "process orchestration",
        "rpa",
    ),
    "backend developer": (
        "backend",
        "api",
        "microservices",
        "database modeling",
        "observability",
    ),
    "full stack developer": (
        "full stack",
        "typescript",
        "javascript",
        "react",
        "api",
    ),
    "software engineer": (
        "backend",
        "api",
        "python",
        "javascript",
        "typescript",
        "full stack",
    ),
}

_ROLE_TARGET_ALIAS_PATTERNS: dict[str, tuple[str, ...]] = {
    "automation engineer": (
        r"\bautomation (engineer|developer|specialist)\b",
        r"\bintelligent automation\b",
    ),
    "automation developer": (
        r"\bautomation developer\b",
        r"\bautomation engineer\b",
        r"\bintelligent automation\b",
    ),
    "rpa developer": (
        r"\brpa\b",
        r"\brobotic process automation\b",
        r"\buipath developer\b",
        r"\buipath\b",
    ),
    "backend developer": (
        r"\bbackend\b",
        r"\bserver[\s\-]?side\b",
    ),
    "full stack developer": (
        r"\bfull[\s\-]?stack\b",
        r"\bfullstack\b",
    ),
    "software engineer": (
        r"\bsoftware (engineer|developer)\b",
        r"\bapplication(s)? (engineer|developer)\b",
        r"\bbackend (engineer|developer)\b",
        r"\bback[\s\-]?end (engineer|developer)\b",
        r"\bfull[\s\-]?stack\b",
        r"\bfullstack\b",
    ),
}

_SPECIALIZATION_FALLBACK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "uipath": ("automation", "workflow automation", "system integrations"),
    "langchain": ("applied ai", "rag", "ai-assisted workflows", "chatbot systems"),
    "typescript": ("typescript", "javascript", "api"),
    "javascript": ("javascript", "typescript", "api"),
    "java": ("java", "backend", "api", "microservices"),
}

_ROLE_TARGET_ALIGNMENT_SENTENCES: dict[str, str] = {
    "automation engineer": (
        "Recent work includes workflow design, integrations, and operational tooling "
        "for complex business systems."
    ),
    "automation developer": (
        "Recent work includes workflow design, integrations, and operational tooling "
        "for complex business systems."
    ),
    "rpa developer": (
        "Recent work includes process orchestration, integrations, and resilient workflow delivery "
        "for business-critical operations."
    ),
    "backend developer": (
        "Recent work includes backend services, APIs, and production support "
        "for operationally critical platforms."
    ),
    "full stack developer": (
        "Recent work spans product delivery across web applications, backend services, "
        "APIs, and operational workflows."
    ),
    "software engineer": (
        "Recent work spans software delivery across application services, APIs, "
        "product features, and operational support."
    ),
}

_ROLE_TARGET_SUMMARY_SCOPES: dict[str, str] = {
    "automation engineer": "workflow design, integrations, and operational tooling",
    "automation developer": "workflow design, integrations, and operational tooling",
    "rpa developer": "process orchestration, workflow reliability, and business operations",
    "backend developer": "backend services, internal tooling, and production support",
    "full stack developer": "product delivery, backend services, and operational workflows",
    "software engineer": "software delivery, application services, and operational support",
}

_ROLE_TARGET_HEADLINE_SCOPE_LABELS: dict[str, str] = {
    "automation engineer": "Automation Systems",
    "automation developer": "Automation Systems",
    "rpa developer": "Workflow Automation",
    "backend developer": "Backend Systems",
}

_GENERIC_EDITORIAL_ROLE_TARGETS = frozenset({"software engineer", "software developer"})

_EDITORIAL_BANNED_PHRASES: tuple[str, ...] = (
    "targeted for",
    "selected fit areas",
    "proven track record",
    "robust background",
    "well-prepared",
    "eager to deliver",
    "results-driven",
    "seasoned professional",
    "dynamic professional",
)

_CANONICAL_TEXT_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"\bapis\b", "APIs"),
    (r"\bapi\b", "API"),
    (r"\bai\b", "AI"),
    (r"\brag\b", "RAG"),
    (r"\bllms\b", "LLMs"),
    (r"\bllm\b", "LLM"),
    (r"\btypescript\b", "TypeScript"),
    (r"\bjavascript\b", "JavaScript"),
    (r"\bdevops\b", "DevOps"),
    (r"\baws\b", "AWS"),
    (r"\bgcp\b", "GCP"),
    (r"\bazure\b", "Azure"),
    (r"\buipath\b", "UiPath"),
    (r"\blangchain\b", "LangChain"),
)

_HEADLINE_SPECIALIZATION_KEYWORDS = frozenset(
    {
        "python",
        "java",
        "typescript",
        "javascript",
        "aws",
        "gcp",
        "azure",
        "react",
        "react native",
        "fastapi",
        "uipath",
        "langchain",
        "rag",
        "applied ai",
        "ai",
        "llm",
        "mobile",
    },
)

_SUMMARY_DETAIL_KEYWORDS = frozenset(
    {
        *tuple(_HEADLINE_SPECIALIZATION_KEYWORDS),
        "api",
        "microservices",
        "system integrations",
        "workflow automation",
        "observability",
        "database modeling",
        "automation",
    },
)


@dataclass(frozen=True, slots=True)
class DynamicResumeBuildResult:
    """Files produced while preparing a resume variant for one job."""

    source_cv_path: Path
    submission_cv_path: Path
    cv_version: str
    resume_mode: ResumeMode = ResumeMode.STATIC
    target_language: SupportedLanguage = SupportedLanguage.ENGLISH
    matched_role_target: str | None = None
    matched_specializations: tuple[str, ...] = ()
    markdown_path: Path | None = None
    css_path: Path | None = None
    used_dynamic_variant: bool = False
    notes: str | None = None
    source_resume_language: SupportedLanguage = SupportedLanguage.ENGLISH


@dataclass(frozen=True, slots=True)
class TailoredResumeMarkdownResult:
    """Rendered markdown plus language metadata for one tailored resume."""

    markdown: str
    target_language: SupportedLanguage
    source_resume_language: SupportedLanguage
    language_alignment_satisfied: bool = True


@dataclass(frozen=True, slots=True)
class ResumeExperienceEntry:
    """Normalized experience block extracted from the source resume."""

    title: str
    company_name: str | None = None
    date_range: str | None = None
    bullets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResumeCertificationEntry:
    """Certification entry preserved from the source resume."""

    name: str
    issuer: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeEducationEntry:
    """Education entry preserved from the source resume."""

    institution: str
    degree: str | None = None
    location: str | None = None
    date_range: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeSourceSnapshot:
    """High-fidelity snapshot extracted from the source resume text."""

    header_role: str | None = None
    summary: str | None = None
    experience_entries: tuple[ResumeExperienceEntry, ...] = ()
    certifications: tuple[ResumeCertificationEntry, ...] = ()
    education_entries: tuple[ResumeEducationEntry, ...] = ()
    skill_lines: tuple[str, ...] = ()
    additional_sections: tuple[tuple[str, tuple[str, ...]], ...] = ()
    word_count: int = 0
    phone: str | None = None
    email: str | None = None
    city: str | None = None
    portfolio_hint: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeHeaderItem:
    """Normalized header item parsed from Oh-My-CV front matter."""

    text: str
    link: str | None = None
    new_line: bool = False


@dataclass(frozen=True, slots=True)
class ResumeEntryBlock:
    """Structured resume entry used by the HTML renderer."""

    title: str
    meta_lines: tuple[str, ...] = ()
    paragraphs: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExperienceFocusPlan:
    """Keyword hints used to re-prioritize one experience block."""

    entry_hint: str
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResumeAdaptationPlan:
    """Structured adaptation plan used to render a tailored resume safely."""

    headline: str | None = None
    summary: str | None = None
    focus_keywords: tuple[str, ...] = ()
    skill_focus: tuple[str, ...] = ()
    experience_focus: tuple[ExperienceFocusPlan, ...] = ()
    adaptation_summary: str | None = None


class OhMyCvDynamicResumeBuilder:
    """Create a job-tailored resume variant and render it as PDF when enabled."""

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, runtime_settings: RuntimeSettings) -> None:
        self._runtime_settings = runtime_settings

    def build_for_job(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        matched_role_target: str | None = None,
        matched_specializations: tuple[str, ...] = (),
        run_dir: Path,
        submission_id: UUID,
    ) -> DynamicResumeBuildResult | None:
        """Return the resume file that should be uploaded for this specific job."""

        source_cv_path = _existing_path(settings.profile.cv_path)
        if source_cv_path is None:
            return None
        target_language_signal = self._detect_target_resume_language(
            settings=settings,
            posting=posting,
        )
        base_copy = self._copy_source_cv(
            source_cv_path=source_cv_path,
            run_dir=run_dir,
            submission_id=submission_id,
            filename=settings.profile.cv_filename,
        )
        requested_resume_mode = settings.profile.resume_mode
        if requested_resume_mode is ResumeMode.STATIC:
            return DynamicResumeBuildResult(
                source_cv_path=source_cv_path,
                submission_cv_path=base_copy,
                cv_version=base_copy.name,
                resume_mode=ResumeMode.STATIC,
                target_language=target_language_signal.language,
                matched_role_target=matched_role_target,
                matched_specializations=matched_specializations,
                notes="static_resume_mode_selected",
                source_resume_language=settings.profile.preferred_language,
            )
        if not self._runtime_settings.resume_dynamic_enabled:
            return DynamicResumeBuildResult(
                source_cv_path=source_cv_path,
                submission_cv_path=base_copy,
                cv_version=base_copy.name,
                resume_mode=requested_resume_mode,
                target_language=target_language_signal.language,
                matched_role_target=matched_role_target,
                matched_specializations=matched_specializations,
                notes="dynamic_resume_runtime_disabled",
                source_resume_language=settings.profile.preferred_language,
            )

        resume_text = self._extract_resume_text(source_cv_path)
        resume_snapshot = self._build_resume_source_snapshot(
            settings=settings,
            resume_text=resume_text,
        )
        source_language_signal = self._detect_resume_source_language(
            settings=settings,
            resume_text=resume_text,
            resume_snapshot=resume_snapshot,
        )
        resume_markdown_result = self._build_tailored_markdown(
            settings=settings,
            posting=posting,
            matched_role_target=matched_role_target,
            matched_specializations=matched_specializations,
            resume_text=resume_text,
            resume_snapshot=resume_snapshot,
            source_language=source_language_signal.language,
            target_language=target_language_signal.language,
        )
        if resume_markdown_result is None:
            return DynamicResumeBuildResult(
                source_cv_path=source_cv_path,
                submission_cv_path=base_copy,
                cv_version=base_copy.name,
                resume_mode=requested_resume_mode,
                target_language=target_language_signal.language,
                matched_role_target=matched_role_target,
                matched_specializations=matched_specializations,
                notes="dynamic_resume_markdown_generation_failed",
                source_resume_language=source_language_signal.language,
            )
        if not resume_markdown_result.language_alignment_satisfied:
            return DynamicResumeBuildResult(
                source_cv_path=source_cv_path,
                submission_cv_path=base_copy,
                cv_version=base_copy.name,
                resume_mode=requested_resume_mode,
                target_language=resume_markdown_result.target_language,
                matched_role_target=matched_role_target,
                matched_specializations=matched_specializations,
                notes="dynamic_resume_language_alignment_unavailable",
                source_resume_language=resume_markdown_result.source_resume_language,
            )

        variant_dir = run_dir / "dynamic-resume"
        variant_dir.mkdir(parents=True, exist_ok=True)
        slug = _slugify(posting.title) or "job"
        token = submission_id.hex[:8]
        markdown_path = variant_dir / f"{token}-{slug}-oh-my-cv.md"
        markdown_path.write_text(resume_markdown_result.markdown, encoding="utf-8")

        css_text = self._resolve_resume_css(settings=settings)
        css_path = variant_dir / f"{token}-resume.css"
        css_path.write_text(css_text, encoding="utf-8")

        pdf_path = variant_dir / f"{token}-{slug}-tailored.pdf"
        rendered, render_notes = self._render_markdown_to_pdf(
            markdown_path=markdown_path,
            css_path=css_path,
            output_pdf_path=pdf_path,
        )
        if not rendered:
            return DynamicResumeBuildResult(
                source_cv_path=source_cv_path,
                submission_cv_path=base_copy,
                cv_version=base_copy.name,
                resume_mode=requested_resume_mode,
                target_language=resume_markdown_result.target_language,
                matched_role_target=matched_role_target,
                matched_specializations=matched_specializations,
                markdown_path=markdown_path,
                css_path=css_path,
                notes=render_notes or "dynamic_resume_pdf_render_failed",
                source_resume_language=resume_markdown_result.source_resume_language,
            )

        return DynamicResumeBuildResult(
            source_cv_path=source_cv_path,
            submission_cv_path=pdf_path,
            cv_version=pdf_path.name,
            resume_mode=requested_resume_mode,
            target_language=resume_markdown_result.target_language,
            matched_role_target=matched_role_target,
            matched_specializations=matched_specializations,
            markdown_path=markdown_path,
            css_path=css_path,
            used_dynamic_variant=True,
            notes="dynamic_resume_ready",
            source_resume_language=resume_markdown_result.source_resume_language,
        )

    def _copy_source_cv(
        self,
        *,
        source_cv_path: Path,
        run_dir: Path,
        submission_id: UUID,
        filename: str | None,
    ) -> Path:
        input_dir = run_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        safe_filename = _sanitize_filename(filename or source_cv_path.name)
        copied_path = input_dir / f"{submission_id.hex[:8]}-{safe_filename}"
        if not copied_path.exists():
            shutil.copy2(source_cv_path, copied_path)
        return copied_path

    def _extract_resume_text(self, source_cv_path: Path) -> str | None:
        suffix = source_cv_path.suffix.lower()
        if suffix in {".txt", ".md"}:
            return self._truncate_resume_text(
                source_cv_path.read_text(encoding="utf-8", errors="ignore")
            )
        if suffix == ".docx":
            return self._truncate_resume_text(_extract_docx_text(source_cv_path))
        if suffix == ".pdf":
            return self._truncate_resume_text(_extract_pdf_text(source_cv_path))
        return None

    def _truncate_resume_text(self, text: str | None) -> str | None:
        if text is None:
            return None
        normalized = text.strip()
        if not normalized:
            return None
        limit = max(2_000, self._runtime_settings.resume_dynamic_max_cv_chars)
        return normalized[:limit]

    def _detect_target_resume_language(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> LanguageDetectionResult:
        return detect_job_posting_language(
            posting,
            default_language=settings.profile.preferred_language,
        )

    def _detect_resume_source_language(
        self,
        *,
        settings: UserAgentSettings,
        resume_text: str | None,
        resume_snapshot: ResumeSourceSnapshot,
    ) -> LanguageDetectionResult:
        snapshot_fragments = [
            resume_snapshot.header_role or "",
            resume_snapshot.summary or "",
            "\n".join(resume_snapshot.skill_lines),
        ]
        for entry in resume_snapshot.experience_entries:
            snapshot_fragments.append(
                "\n".join(
                    filter(
                        None,
                        (
                            entry.title,
                            entry.company_name or "",
                            " ".join(entry.bullets),
                        ),
                    )
                )
            )
        text_signal = detect_text_language(
            resume_text or "",
            default_language=settings.profile.preferred_language,
            source="resume_text",
        )
        snapshot_signal = detect_text_language(
            "\n".join(fragment for fragment in snapshot_fragments if fragment.strip()),
            default_language=settings.profile.preferred_language,
            source="resume_snapshot",
        )
        return combine_language_signals(
            (
                (snapshot_signal, 1.25),
                (text_signal, 1.0),
            ),
            default_language=settings.profile.preferred_language,
            source="resume_source",
        )

    def _build_tailored_markdown(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        matched_role_target: str | None,
        matched_specializations: tuple[str, ...],
        resume_text: str | None,
        resume_snapshot: ResumeSourceSnapshot,
        source_language: SupportedLanguage,
        target_language: SupportedLanguage,
    ) -> TailoredResumeMarkdownResult | None:
        fallback_markdown = self._build_fallback_markdown(
            settings=settings,
            posting=posting,
            matched_role_target=matched_role_target,
            matched_specializations=matched_specializations,
            resume_text=resume_text,
            resume_snapshot=resume_snapshot,
            render_language=target_language,
        )
        language_safe_fallback_markdown = fallback_markdown
        if not _snapshot_has_structured_content(resume_snapshot):
            return TailoredResumeMarkdownResult(
                markdown=fallback_markdown,
                target_language=target_language,
                source_resume_language=source_language,
            )

        heuristic_plan = self._build_heuristic_adaptation_plan(
            settings=settings,
            posting=posting,
            matched_role_target=matched_role_target,
            matched_specializations=matched_specializations,
            resume_snapshot=resume_snapshot,
        )
        adaptation_plan = heuristic_plan
        if settings.ai.api_key is not None:
            resume_evidence_keywords = self._build_resume_evidence_keywords(
                settings=settings,
                resume_snapshot=resume_snapshot,
            )
            prompt_payload = self._build_prompt_payload(
                settings=settings,
                posting=posting,
                matched_role_target=matched_role_target,
                matched_specializations=matched_specializations,
                resume_text=resume_text,
                resume_snapshot=resume_snapshot,
                fallback_markdown=fallback_markdown,
                heuristic_plan=heuristic_plan,
                resume_evidence_keywords=resume_evidence_keywords,
                source_language=source_language,
                target_language=target_language,
            )
            try:
                response_payload = self._create_response(
                    api_key=settings.ai.api_key.get_secret_value(),
                    model=settings.ai.model,
                    prompt_payload=prompt_payload,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "dynamic_resume_openai_generation_failed",
                    extra={
                        "job_posting_id": str(posting.id),
                        "title": posting.title,
                        "company_name": posting.company_name,
                    },
                )
            else:
                ai_plan = self._parse_adaptation_plan(response_payload)
                if ai_plan is not None:
                    ai_plan = self._sanitize_ai_adaptation_plan(
                        ai_plan=ai_plan,
                        heuristic_plan=heuristic_plan,
                        settings=settings,
                    )
                if ai_plan is not None and self._validate_adaptation_plan(
                    plan=ai_plan,
                    posting=posting,
                    resume_snapshot=resume_snapshot,
                ):
                    adaptation_plan = self._merge_adaptation_plans(heuristic_plan, ai_plan)

        localized_snapshot = resume_snapshot
        localized_plan = adaptation_plan
        language_alignment_satisfied = target_language is source_language
        should_localize_resume = (
            target_language is not SupportedLanguage.ENGLISH
            or source_language is not target_language
        )
        if should_localize_resume and settings.ai.api_key is not None:
            try:
                (
                    localized_snapshot,
                    localized_plan,
                    language_alignment_satisfied,
                ) = self._localize_resume_snapshot(
                    settings=settings,
                    resume_snapshot=resume_snapshot,
                    adaptation_plan=adaptation_plan,
                    source_language=source_language,
                    target_language=target_language,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "dynamic_resume_localization_failed",
                    extra={
                        "job_posting_id": str(posting.id),
                        "title": posting.title,
                        "company_name": posting.company_name,
                        "source_language": source_language.value,
                        "target_language": target_language.value,
                    },
                )
        elif should_localize_resume:
            language_alignment_satisfied = False

        if language_alignment_satisfied:
            language_safe_fallback_markdown = self._build_preserved_resume_markdown(
                settings=settings,
                posting=posting,
                resume_snapshot=localized_snapshot,
                adaptation_plan=localized_plan,
                render_language=target_language,
            )

        markdown = self._build_preserved_resume_markdown(
            settings=settings,
            posting=posting,
            resume_snapshot=localized_snapshot,
            adaptation_plan=localized_plan,
            render_language=target_language,
        )
        markdown_is_valid = self._validate_generated_markdown(
            markdown=markdown,
            fallback_markdown=fallback_markdown,
            resume_snapshot=resume_snapshot,
            render_language=target_language,
        )
        markdown_is_aligned = self._markdown_aligns_with_target_language(
            markdown=markdown,
            target_language=target_language,
        )
        if markdown_is_valid and markdown_is_aligned:
            return TailoredResumeMarkdownResult(
                markdown=markdown,
                target_language=target_language,
                source_resume_language=source_language,
                language_alignment_satisfied=language_alignment_satisfied,
            )

        safe_fallback_is_valid = self._validate_generated_markdown(
            markdown=language_safe_fallback_markdown,
            fallback_markdown=fallback_markdown,
            resume_snapshot=resume_snapshot,
            render_language=target_language,
        )
        safe_fallback_is_aligned = self._markdown_aligns_with_target_language(
            markdown=language_safe_fallback_markdown,
            target_language=target_language,
        )
        if (
            language_safe_fallback_markdown != fallback_markdown
            and safe_fallback_is_valid
            and safe_fallback_is_aligned
        ):
            return TailoredResumeMarkdownResult(
                markdown=language_safe_fallback_markdown,
                target_language=target_language,
                source_resume_language=source_language,
                language_alignment_satisfied=True,
            )

        return TailoredResumeMarkdownResult(
            markdown=fallback_markdown,
            target_language=target_language,
            source_resume_language=source_language,
            language_alignment_satisfied=False,
        )

    def _localize_resume_snapshot(
        self,
        *,
        settings: UserAgentSettings,
        resume_snapshot: ResumeSourceSnapshot,
        adaptation_plan: ResumeAdaptationPlan,
        source_language: SupportedLanguage,
        target_language: SupportedLanguage,
    ) -> tuple[ResumeSourceSnapshot, ResumeAdaptationPlan, bool]:
        translation_items = self._build_resume_translation_items(
            resume_snapshot=resume_snapshot,
            adaptation_plan=adaptation_plan,
        )
        if not translation_items:
            return resume_snapshot, adaptation_plan, False
        translated_texts = self._translate_resume_items(
            settings=settings,
            translation_items=translation_items,
            source_language=source_language,
            target_language=target_language,
        )
        if translated_texts and not self._translated_resume_items_align(
            translated_texts=translated_texts,
            target_language=target_language,
        ):
            translated_texts = self._translate_resume_items(
                settings=settings,
                translation_items=translation_items,
                source_language=source_language,
                target_language=target_language,
                strict_target_language=True,
            )
        if not translated_texts:
            return resume_snapshot, adaptation_plan, False
        if not self._translated_resume_items_align(
            translated_texts=translated_texts,
            target_language=target_language,
        ):
            return resume_snapshot, adaptation_plan, False
        localized_snapshot, localized_plan = self._apply_translated_resume_items(
            resume_snapshot=resume_snapshot,
            adaptation_plan=adaptation_plan,
            translated_texts=translated_texts,
            target_language=target_language,
        )
        localized_snapshot, localized_plan = self._repair_residual_localization(
            settings=settings,
            resume_snapshot=localized_snapshot,
            adaptation_plan=localized_plan,
            target_language=target_language,
        )
        return localized_snapshot, localized_plan, True

    def _build_resume_translation_items(
        self,
        *,
        resume_snapshot: ResumeSourceSnapshot,
        adaptation_plan: ResumeAdaptationPlan,
    ) -> tuple[tuple[str, str], ...]:
        items: list[tuple[str, str]] = []

        def add_item(ref: str, text: str | None) -> None:
            candidate = _normalize_resume_copy(text or "")
            if not candidate:
                return
            if not re.search(r"[A-Za-zÀ-ÿ]{3,}", candidate):
                return
            items.append((ref, candidate))

        add_item("headline", adaptation_plan.headline or resume_snapshot.header_role)
        add_item("summary", adaptation_plan.summary or resume_snapshot.summary)
        add_item("city", resume_snapshot.city)

        for index, entry in enumerate(resume_snapshot.experience_entries):
            add_item(f"experience_title_{index}", entry.title)
            add_item(f"experience_company_{index}", entry.company_name)
            add_item(f"experience_date_{index}", entry.date_range)
            for bullet_index, bullet in enumerate(entry.bullets):
                add_item(f"experience_bullet_{index}_{bullet_index}", bullet)

        for index, certification in enumerate(resume_snapshot.certifications):
            add_item(f"certification_name_{index}", certification.name)
            add_item(f"certification_issuer_{index}", certification.issuer)

        for index, education in enumerate(resume_snapshot.education_entries):
            add_item(f"education_institution_{index}", education.institution)
            add_item(f"education_degree_{index}", education.degree)
            add_item(f"education_location_{index}", education.location)

        for index, skill_line in enumerate(resume_snapshot.skill_lines):
            add_item(f"skill_line_{index}", skill_line)

        for section_index, (title, lines) in enumerate(resume_snapshot.additional_sections):
            add_item(f"additional_title_{section_index}", title)
            for line_index, line in enumerate(lines):
                add_item(f"additional_line_{section_index}_{line_index}", line)

        return tuple(items)

    def _translate_resume_items(
        self,
        *,
        settings: UserAgentSettings,
        translation_items: tuple[tuple[str, str], ...],
        source_language: SupportedLanguage,
        target_language: SupportedLanguage,
        strict_target_language: bool = False,
    ) -> dict[str, str] | None:
        if settings.ai.api_key is None:
            return None
        batched_items = self._chunk_resume_translation_items(translation_items)
        translated: dict[str, str] = {}
        for batch in batched_items:
            batch_result = self._translate_resume_items_batch(
                settings=settings,
                translation_items=batch,
                source_language=source_language,
                target_language=target_language,
                strict_target_language=strict_target_language,
            )
            if batch_result is None:
                return None
            translated.update(batch_result)
        expected_refs = {ref for ref, _ in translation_items}
        if expected_refs - set(translated):
            return None
        return translated

    def _translate_resume_items_batch(
        self,
        *,
        settings: UserAgentSettings,
        translation_items: tuple[tuple[str, str], ...],
        source_language: SupportedLanguage,
        target_language: SupportedLanguage,
        strict_target_language: bool = False,
    ) -> dict[str, str] | None:
        if settings.ai.api_key is None:
            return None
        payload: dict[str, object] = {
            "source_language": source_language.value,
            "source_language_name": display_name_for_language(source_language),
            "target_language": target_language.value,
            "target_language_name": display_name_for_language(target_language),
            "translation_mode": "strict_target_language" if strict_target_language else "standard",
            "items": [{"ref": ref, "text": text} for ref, text in translation_items],
        }
        developer_text = (
            "You translate structured resume text from one language to another while "
            "preserving facts exactly. Keep employers, institutions, URLs, technology names, "
            "acronyms, dates, and certifications faithful to the source. Translate natural "
            "language into the target_language_name only. Translate mixed label/value lines "
            "too, such as skill category labels before a colon, while preserving the actual "
            "technology tokens after the colon. Translate degree names when they are natural "
            "language phrases, but do not invent equivalencies or new credentials. Do not add "
            "or remove items. Return only valid JSON matching the schema."
        )
        if strict_target_language:
            developer_text += (
                " Every returned text must read naturally in the target_language_name, even "
                "when the source already looks acceptable in another language. Do not leave "
                "English prose unchanged when target_language_name is Portuguese. Proper names "
                "and technology tokens may stay as-is, but the surrounding narrative must be "
                "fully localized."
            )
        response_payload = self._create_structured_response(
            api_key=settings.ai.api_key.get_secret_value(),
            model=settings.ai.model,
            developer_text=developer_text,
            prompt_payload=payload,
            schema_name="resume_translation",
            schema=_RESUME_TRANSLATION_SCHEMA,
        )
        return self._parse_translation_response(
            response_payload=response_payload,
            expected_refs=tuple(ref for ref, _ in translation_items),
        )

    def _chunk_resume_translation_items(
        self,
        translation_items: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[tuple[str, str], ...], ...]:
        batches: list[tuple[tuple[str, str], ...]] = []
        current_batch: list[tuple[str, str]] = []
        current_chars = 0
        max_items_per_batch = 10
        max_chars_per_batch = 1800
        for item in translation_items:
            ref, text = item
            item_chars = len(ref) + len(text)
            if current_batch and (
                len(current_batch) >= max_items_per_batch
                or current_chars + item_chars > max_chars_per_batch
            ):
                batches.append(tuple(current_batch))
                current_batch = []
                current_chars = 0
            current_batch.append(item)
            current_chars += item_chars
        if current_batch:
            batches.append(tuple(current_batch))
        return tuple(batches)

    def _parse_translation_response(
        self,
        *,
        response_payload: dict[str, object],
        expected_refs: tuple[str, ...],
    ) -> dict[str, str] | None:
        output_text = self._extract_output_text(response_payload)
        if not output_text:
            return None
        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError:
            logger.warning("dynamic_resume_translation_invalid_json", extra={"output": output_text})
            return None
        if not isinstance(payload, dict):
            return None
        items = payload.get("items")
        if not isinstance(items, list):
            return None
        expected_ref_set = set(expected_refs)
        translated: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref") or "").strip()
            text = _normalize_resume_copy(str(item.get("text") or "").strip())
            if not ref or not text or ref not in expected_ref_set:
                continue
            translated[ref] = text
        if expected_ref_set - set(translated):
            return None
        return translated

    def _translated_resume_items_align(
        self,
        *,
        translated_texts: dict[str, str],
        target_language: SupportedLanguage,
    ) -> bool:
        if target_language is SupportedLanguage.ENGLISH:
            return True
        sampled_segments = [
            translated_texts.get("headline", ""),
            translated_texts.get("summary", ""),
        ]
        sampled_segments.extend(
            text for ref, text in translated_texts.items() if ref.startswith("experience_bullet_")
        )
        sample_text = "\n".join(segment for segment in sampled_segments if segment.strip())
        if not sample_text:
            return False
        detection = detect_text_language(
            sample_text,
            default_language=target_language,
            source="translated_resume_items",
        )
        return detection.language is target_language and detection.confidence >= 0.35

    def _apply_translated_resume_items(
        self,
        *,
        resume_snapshot: ResumeSourceSnapshot,
        adaptation_plan: ResumeAdaptationPlan,
        translated_texts: dict[str, str],
        target_language: SupportedLanguage,
    ) -> tuple[ResumeSourceSnapshot, ResumeAdaptationPlan]:
        localized_plan = replace(
            adaptation_plan,
            headline=translated_texts.get("headline", adaptation_plan.headline),
            summary=translated_texts.get("summary", adaptation_plan.summary),
        )

        localized_experience_entries = tuple(
            replace(
                entry,
                title=translated_texts.get(f"experience_title_{index}", entry.title),
                company_name=translated_texts.get(
                    f"experience_company_{index}",
                    entry.company_name,
                ),
                date_range=translated_texts.get(
                    f"experience_date_{index}",
                    entry.date_range,
                ),
                bullets=tuple(
                    translated_texts.get(
                        f"experience_bullet_{index}_{bullet_index}",
                        bullet,
                    )
                    for bullet_index, bullet in enumerate(entry.bullets)
                ),
            )
            for index, entry in enumerate(resume_snapshot.experience_entries)
        )
        localized_certifications = tuple(
            replace(
                certification,
                name=translated_texts.get(f"certification_name_{index}", certification.name),
                issuer=translated_texts.get(
                    f"certification_issuer_{index}",
                    certification.issuer,
                ),
            )
            for index, certification in enumerate(resume_snapshot.certifications)
        )
        localized_education_entries = tuple(
            replace(
                education,
                institution=translated_texts.get(
                    f"education_institution_{index}",
                    education.institution,
                ),
                degree=translated_texts.get(f"education_degree_{index}", education.degree),
                location=translated_texts.get(f"education_location_{index}", education.location),
            )
            for index, education in enumerate(resume_snapshot.education_entries)
        )
        localized_skill_lines = tuple(
            self._localize_skill_line(
                translated_texts.get(f"skill_line_{index}", skill_line),
                target_language=target_language,
            )
            for index, skill_line in enumerate(resume_snapshot.skill_lines)
        )
        localized_additional_sections = tuple(
            (
                translated_texts.get(f"additional_title_{section_index}", title),
                tuple(
                    translated_texts.get(
                        f"additional_line_{section_index}_{line_index}",
                        line,
                    )
                    for line_index, line in enumerate(lines)
                ),
            )
            for section_index, (title, lines) in enumerate(resume_snapshot.additional_sections)
        )
        localized_snapshot = replace(
            resume_snapshot,
            header_role=translated_texts.get("headline", resume_snapshot.header_role),
            summary=translated_texts.get("summary", resume_snapshot.summary),
            city=translated_texts.get("city", resume_snapshot.city),
            experience_entries=localized_experience_entries,
            certifications=localized_certifications,
            education_entries=localized_education_entries,
            skill_lines=localized_skill_lines,
            additional_sections=localized_additional_sections,
        )
        return localized_snapshot, localized_plan

    def _repair_residual_localization(
        self,
        *,
        settings: UserAgentSettings,
        resume_snapshot: ResumeSourceSnapshot,
        adaptation_plan: ResumeAdaptationPlan,
        target_language: SupportedLanguage,
    ) -> tuple[ResumeSourceSnapshot, ResumeAdaptationPlan]:
        if settings.ai.api_key is None or target_language is SupportedLanguage.ENGLISH:
            return resume_snapshot, adaptation_plan
        residual_items = tuple(
            (ref, text)
            for ref, text in self._build_resume_translation_items(
                resume_snapshot=resume_snapshot,
                adaptation_plan=adaptation_plan,
            )
            if self._resume_item_needs_localization(
                ref=ref,
                text=text,
                target_language=target_language,
            )
        )
        if not residual_items:
            return resume_snapshot, adaptation_plan
        translated_residuals: dict[str, str] = {}
        grouped_items: dict[SupportedLanguage, list[tuple[str, str]]] = {}
        for ref, text in residual_items:
            detection = detect_text_language(
                text,
                default_language=target_language,
                source="resume_localization_repair",
            )
            if detection.language is target_language:
                continue
            grouped_items.setdefault(detection.language, []).append((ref, text))
        for index, skill_line in enumerate(resume_snapshot.skill_lines):
            prefix, separator, suffix = skill_line.partition(":")
            normalized_suffix = _normalize_resume_copy(suffix)
            if not separator or not normalized_suffix:
                continue
            if len(re.findall(r"[A-Za-zÀ-ÿ]{3,}", normalized_suffix)) < 2:
                continue
            suffix_detection = detect_text_language(
                normalized_suffix,
                default_language=target_language,
                source="resume_skill_suffix_localization_check",
            )
            grouped_items.setdefault(suffix_detection.language, []).append(
                (f"skill_suffix_{index}", normalized_suffix)
            )
        for source_language, items in grouped_items.items():
            batch_result = self._translate_resume_items(
                settings=settings,
                translation_items=tuple(items),
                source_language=source_language,
                target_language=target_language,
                strict_target_language=True,
            )
            if batch_result:
                translated_residuals.update(batch_result)
        if not translated_residuals:
            return resume_snapshot, adaptation_plan
        localized_snapshot, localized_plan = self._apply_translated_resume_items(
            resume_snapshot=resume_snapshot,
            adaptation_plan=adaptation_plan,
            translated_texts=translated_residuals,
            target_language=target_language,
        )
        if any(ref.startswith("skill_suffix_") for ref in translated_residuals):
            localized_skill_lines = list(localized_snapshot.skill_lines)
            for index, skill_line in enumerate(localized_skill_lines):
                translated_suffix = translated_residuals.get(f"skill_suffix_{index}")
                if translated_suffix is None:
                    continue
                prefix, separator, _suffix = skill_line.partition(":")
                if not separator:
                    continue
                localized_skill_lines[index] = (
                    f"{localized_skill_category_label(prefix, target_language)}: "
                    f"{_normalize_resume_copy(translated_suffix)}"
                )
            localized_snapshot = replace(
                localized_snapshot,
                skill_lines=tuple(localized_skill_lines),
            )
        return localized_snapshot, localized_plan

    def _resume_item_needs_localization(
        self,
        *,
        ref: str,
        text: str,
        target_language: SupportedLanguage,
    ) -> bool:
        normalized_text = _normalize_resume_copy(text)
        if not normalized_text:
            return False
        alpha_tokens = re.findall(r"[A-Za-zÀ-ÿ]{2,}", normalized_text)
        if not alpha_tokens:
            return False
        always_repair_meta_prefixes = (
            "experience_company_",
            "experience_date_",
            "certification_name_",
            "certification_issuer_",
            "education_degree_",
            "education_location_",
        )
        minimum_token_count = 3
        if ref.startswith(always_repair_meta_prefixes):
            minimum_token_count = 1
        elif ref.startswith(("experience_title_", "additional_title_", "additional_line_")):
            minimum_token_count = 2
        if len(alpha_tokens) < minimum_token_count:
            return False
        detection = detect_text_language(
            normalized_text,
            default_language=target_language,
            source="resume_item_localization_check",
        )
        if ref.startswith(always_repair_meta_prefixes):
            return not (detection.language is target_language and detection.confidence >= 0.6)
        return detection.language is not target_language and detection.confidence >= 0.45

    def _localize_skill_line(
        self,
        skill_line: str,
        *,
        target_language: SupportedLanguage,
    ) -> str:
        prefix, separator, suffix = skill_line.partition(":")
        if not separator:
            return skill_line
        localized_prefix = localized_skill_category_label(prefix, target_language)
        normalized_suffix = suffix.strip()
        if not normalized_suffix:
            return localized_prefix
        return f"{localized_prefix}: {normalized_suffix}"

    def _build_prompt_payload(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        matched_role_target: str | None,
        matched_specializations: tuple[str, ...],
        resume_text: str | None,
        resume_snapshot: ResumeSourceSnapshot,
        fallback_markdown: str,
        heuristic_plan: ResumeAdaptationPlan,
        resume_evidence_keywords: tuple[str, ...],
        source_language: SupportedLanguage,
        target_language: SupportedLanguage,
    ) -> dict[str, object]:
        return {
            "language_context": {
                "default_product_language": settings.profile.preferred_language.value,
                "source_resume_language": source_language.value,
                "source_resume_language_name": display_name_for_language(source_language),
                "target_resume_language": target_language.value,
                "target_resume_language_name": display_name_for_language(target_language),
            },
            "candidate_profile": {
                "name": settings.profile.name,
                "email": settings.profile.email,
                "phone": settings.profile.phone,
                "city": settings.profile.city,
                "linkedin_url": str(settings.profile.linkedin_url or ""),
                "github_url": str(settings.profile.github_url or ""),
                "portfolio_url": str(settings.profile.portfolio_url or ""),
                "years_experience_by_stack": settings.profile.years_experience_by_stack,
                "work_authorized": settings.profile.work_authorized,
                "needs_sponsorship": settings.profile.needs_sponsorship,
                "availability": settings.profile.availability,
                "salary_expectation": settings.profile.salary_expectation,
                "default_responses": settings.profile.default_responses,
                "resume_mode": settings.profile.resume_mode.value,
                "preferred_language": settings.profile.preferred_language.value,
                "capability_profile": capability_profile_to_payload(
                    build_candidate_capability_profile(settings)
                ),
            },
            "job_target": {
                "title": posting.title,
                "company_name": posting.company_name,
                "location": posting.location or settings.search.location,
                "description_raw": posting.description_raw[:20_000]
                if _posting_has_usable_detail_context(posting)
                else "",
                "detail_quality_score": posting.detail_quality_score,
                "detail_description_score": posting.detail_description_score,
                "detail_quality_source": posting.detail_quality_source,
                "detail_quality_signals": list(posting.detail_quality_signals),
                "description_context_available": _posting_has_usable_detail_context(posting),
                "positive_filters": list(settings.profile.positive_filters),
                "role_targets": list(settings.search.keywords),
                "matched_role_target": matched_role_target,
                "matched_specializations": list(matched_specializations),
                "target_keywords": list(_extract_posting_keywords(posting)),
            },
            "allowed_focus_keywords": list(heuristic_plan.focus_keywords),
            "allowed_skill_focus_keywords": list(heuristic_plan.skill_focus),
            "resume_evidence_keywords": list(resume_evidence_keywords),
            "source_resume_text": resume_text or "",
            "source_resume_snapshot": _resume_snapshot_to_payload(resume_snapshot),
            "fallback_markdown": fallback_markdown,
        }

    def _build_heuristic_adaptation_plan(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        matched_role_target: str | None,
        matched_specializations: tuple[str, ...],
        resume_snapshot: ResumeSourceSnapshot,
    ) -> ResumeAdaptationPlan:
        role_target = matched_role_target or self._select_primary_role_target(
            settings=settings,
            posting=posting,
        )
        broad_focus_keywords = self._build_target_stack_hints(
            settings=settings,
            posting=posting,
            resume_snapshot=resume_snapshot,
            role_target=role_target,
            matched_specializations=matched_specializations,
        )
        focus_keywords = self._build_editorial_focus_keywords(
            posting=posting,
            broad_focus_keywords=broad_focus_keywords,
            matched_specializations=matched_specializations,
        )
        primary_focus_keywords = self._build_primary_focus_keywords(
            role_target=role_target,
            focus_keywords=broad_focus_keywords,
        )
        role_resolution_keywords = focus_keywords or primary_focus_keywords or broad_focus_keywords
        summary_focus_keywords = focus_keywords or role_resolution_keywords[:3]
        headline = self._build_targeted_headline(
            posting=posting,
            resume_snapshot=resume_snapshot,
            role_target=role_target,
            focus_keywords=focus_keywords,
            role_resolution_keywords=role_resolution_keywords,
        )
        summary = self._build_targeted_summary(
            resume_snapshot=resume_snapshot,
            posting=posting,
            role_target=role_target,
            focus_keywords=summary_focus_keywords,
            role_resolution_keywords=role_resolution_keywords,
        )
        experience_focus = tuple(
            ExperienceFocusPlan(
                entry_hint=entry.title,
                keywords=tuple(
                    self._keywords_for_experience_entry(
                        entry=entry,
                        focus_keywords=(
                            primary_focus_keywords or broad_focus_keywords or focus_keywords
                        ),
                    ),
                ),
            )
            for entry in resume_snapshot.experience_entries
        )
        adaptation_focus = focus_keywords or primary_focus_keywords or broad_focus_keywords
        return ResumeAdaptationPlan(
            headline=headline,
            summary=summary,
            focus_keywords=summary_focus_keywords,
            skill_focus=primary_focus_keywords or broad_focus_keywords or focus_keywords,
            experience_focus=experience_focus,
            adaptation_summary=(
                f"Emphasize {', '.join(adaptation_focus[:4])} for {posting.title} "
                f"while preserving the source resume facts."
            ),
        )

    def _build_targeted_summary(
        self,
        *,
        resume_snapshot: ResumeSourceSnapshot,
        posting: JobPosting,
        role_target: str | None,
        focus_keywords: tuple[str, ...],
        role_resolution_keywords: tuple[str, ...],
    ) -> str:
        base_summary = _normalize_resume_copy(
            resume_snapshot.summary
            or (
                "Full Stack Software Engineer with strong experience across software delivery, "
                "automation, and applied AI."
            )
        ).strip()
        base_sentences = _split_summary_sentences(base_summary)
        if not base_sentences:
            return _trim_summary_text(base_summary)
        lead_sentence = base_sentences[0]
        editorial_role_target = _resolve_editorial_role_target(
            role_target=role_target,
            posting_title=posting.title,
            focus_keywords=role_resolution_keywords,
        )
        focus_sentence = _build_summary_focus_sentence(
            role_target=editorial_role_target,
            focus_keywords=focus_keywords,
            posting_keywords=_extract_posting_keywords(posting),
            lead_sentence=lead_sentence,
        )
        closing_sentence = _select_summary_closing_sentence(
            base_sentences=base_sentences,
            existing_sentences=(lead_sentence, focus_sentence),
        )
        summary_sentences = [lead_sentence]
        if focus_sentence:
            summary_sentences.append(focus_sentence)
        if closing_sentence:
            summary_sentences.append(closing_sentence)
        assembled_summary = _assemble_summary_sentences(tuple(summary_sentences))
        if not assembled_summary:
            return _trim_summary_text(base_summary)
        return _trim_summary_text(assembled_summary)

    def _build_targeted_headline(
        self,
        *,
        posting: JobPosting,
        resume_snapshot: ResumeSourceSnapshot,
        role_target: str | None,
        focus_keywords: tuple[str, ...],
        role_resolution_keywords: tuple[str, ...],
    ) -> str:
        headline_role = _extract_base_role_identity(
            resume_snapshot.header_role or "Full Stack Software Engineer",
        )
        editorial_role_target = _resolve_editorial_role_target(
            role_target=role_target,
            posting_title=posting.title,
            focus_keywords=role_resolution_keywords,
        )
        headline_keywords = _select_headline_specializations(
            headline_role=headline_role,
            focus_keywords=focus_keywords,
        )
        if not headline_keywords:
            role_scope_label = _resolve_headline_role_scope_label(
                headline_role=headline_role,
                editorial_role_target=editorial_role_target,
                focus_keywords=role_resolution_keywords,
            )
            if role_scope_label:
                return f"{headline_role} | {role_scope_label}"
            return headline_role
        suffix = _format_keyword_phrase(headline_keywords[:2])
        return f"{headline_role} | {suffix}"

    def _keywords_for_experience_entry(
        self,
        *,
        entry: ResumeExperienceEntry,
        focus_keywords: tuple[str, ...],
    ) -> list[str]:
        haystack = _normalize_comparison_text(
            " ".join(
                filter(
                    None,
                    (
                        entry.title,
                        entry.company_name,
                        " ".join(entry.bullets),
                    ),
                ),
            ),
        )
        matched_keywords = [
            keyword for keyword in focus_keywords if _normalize_comparison_text(keyword) in haystack
        ]
        if matched_keywords:
            return matched_keywords[:6]
        return list(focus_keywords[:3])

    def _parse_adaptation_plan(
        self,
        response_data: dict[str, object],
    ) -> ResumeAdaptationPlan | None:
        output_text = self._extract_output_text(response_data)
        if not output_text:
            return None
        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError:
            logger.warning("dynamic_resume_invalid_json_response", extra={"output": output_text})
            return None
        if not isinstance(payload, dict):
            return None

        experience_focus_items: list[ExperienceFocusPlan] = []
        for item in payload.get("experience_focus") or ():
            if not isinstance(item, dict):
                continue
            entry_hint = str(item.get("entry_hint") or "").strip()
            if not entry_hint:
                continue
            keywords = tuple(
                str(keyword).strip()
                for keyword in (item.get("keywords") or ())
                if str(keyword).strip()
            )
            experience_focus_items.append(
                ExperienceFocusPlan(entry_hint=entry_hint, keywords=keywords),
            )

        return ResumeAdaptationPlan(
            headline=str(payload.get("headline") or "").strip() or None,
            summary=str(payload.get("summary") or "").strip() or None,
            focus_keywords=tuple(
                str(keyword).strip()
                for keyword in (payload.get("focus_keywords") or ())
                if str(keyword).strip()
            ),
            skill_focus=tuple(
                str(keyword).strip()
                for keyword in (payload.get("skill_focus") or ())
                if str(keyword).strip()
            ),
            experience_focus=tuple(experience_focus_items),
            adaptation_summary=str(payload.get("adaptation_summary") or "").strip() or None,
        )

    def _validate_adaptation_plan(
        self,
        *,
        plan: ResumeAdaptationPlan,
        posting: JobPosting,
        resume_snapshot: ResumeSourceSnapshot,
    ) -> bool:
        if plan.summary is None or len(plan.summary.split()) < 12:
            return False
        if not _summary_passes_editorial_checks(plan.summary):
            return False
        if not plan.focus_keywords:
            return False
        normalized_summary = _normalize_comparison_text(plan.summary)
        if not any(
            _normalize_comparison_text(keyword) in normalized_summary
            for keyword in plan.focus_keywords
        ):
            return False
        if (
            _normalize_comparison_text(posting.title) not in normalized_summary
            and len(plan.focus_keywords) < 2
        ):
            return False
        return True

    def _merge_adaptation_plans(
        self,
        baseline: ResumeAdaptationPlan,
        override: ResumeAdaptationPlan,
    ) -> ResumeAdaptationPlan:
        return ResumeAdaptationPlan(
            headline=baseline.headline,
            summary=override.summary or baseline.summary,
            focus_keywords=_merge_keyword_sequences(
                baseline.focus_keywords,
                override.focus_keywords,
            ),
            skill_focus=_merge_keyword_sequences(
                baseline.skill_focus,
                override.skill_focus,
            ),
            experience_focus=override.experience_focus or baseline.experience_focus,
            adaptation_summary=override.adaptation_summary or baseline.adaptation_summary,
        )

    def _sanitize_ai_adaptation_plan(
        self,
        *,
        ai_plan: ResumeAdaptationPlan,
        heuristic_plan: ResumeAdaptationPlan,
        settings: UserAgentSettings,
    ) -> ResumeAdaptationPlan | None:
        allowed_summary_keywords = {
            _normalize_keyword(keyword)
            for keyword in heuristic_plan.focus_keywords
            if keyword.strip()
        }
        allowed_skill_keywords = {
            _normalize_keyword(keyword) for keyword in heuristic_plan.skill_focus if keyword.strip()
        }
        allowed_keywords = allowed_summary_keywords | allowed_skill_keywords
        allowed_role_targets = {
            _normalize_keyword(keyword) for keyword in settings.search.keywords if keyword.strip()
        }
        recognized_summary_keywords = {
            _normalize_keyword(keyword)
            for keyword in _extract_keyword_labels(ai_plan.summary or "")
        }
        if any(
            keyword not in allowed_summary_keywords and keyword not in allowed_role_targets
            for keyword in recognized_summary_keywords
        ) or not _summary_passes_editorial_checks(ai_plan.summary):
            sanitized_summary = None
        else:
            sanitized_summary = _normalize_resume_copy(ai_plan.summary or "")

        experience_focus_items: list[ExperienceFocusPlan] = []
        for item in ai_plan.experience_focus:
            filtered_keywords = tuple(
                keyword
                for keyword in item.keywords
                if _normalize_keyword(keyword) in allowed_keywords
            )
            if filtered_keywords:
                experience_focus_items.append(
                    ExperienceFocusPlan(entry_hint=item.entry_hint, keywords=filtered_keywords),
                )

        sanitized_plan = ResumeAdaptationPlan(
            headline=None,
            summary=sanitized_summary,
            focus_keywords=tuple(
                keyword
                for keyword in ai_plan.focus_keywords
                if _normalize_keyword(keyword) in allowed_summary_keywords
            ),
            skill_focus=tuple(
                keyword
                for keyword in ai_plan.skill_focus
                if _normalize_keyword(keyword) in allowed_keywords
            ),
            experience_focus=tuple(experience_focus_items),
            adaptation_summary=ai_plan.adaptation_summary,
        )
        if (
            not sanitized_plan.summary
            and not sanitized_plan.focus_keywords
            and not sanitized_plan.skill_focus
        ):
            return None
        return sanitized_plan

    def _build_fallback_markdown(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        matched_role_target: str | None,
        matched_specializations: tuple[str, ...],
        resume_text: str | None,
        resume_snapshot: ResumeSourceSnapshot,
        render_language: SupportedLanguage,
    ) -> str:
        if _snapshot_has_structured_content(resume_snapshot):
            return self._build_preserved_resume_markdown(
                settings=settings,
                posting=posting,
                resume_snapshot=resume_snapshot,
                render_language=render_language,
            )

        location_line = _escape_yaml_scalar(settings.profile.city)
        if render_language is SupportedLanguage.PORTUGUESE:
            summary = (
                f"Curriculo adaptado para {posting.title} na {posting.company_name}. "
                "Esta versao preserva o historico do candidato e destaca as evidencias tecnicas "
                "mais relevantes disponiveis no curriculo de origem."
            )
        else:
            summary = (
                f"Resume adapted for {posting.title} at {posting.company_name}. "
                "This version preserves the candidate background and highlights the most relevant "
                "technical evidence available in the source resume."
            )
        known_years = ", ".join(
            f"{stack} ({years}y)" for stack, years in _screening_capability_years(settings)[:8]
        )
        role_target = matched_role_target or self._select_primary_role_target(
            settings=settings,
            posting=posting,
        )
        stacks = ", ".join(
            self._build_target_stack_hints(
                settings=settings,
                posting=posting,
                resume_snapshot=resume_snapshot,
                role_target=role_target,
                matched_specializations=matched_specializations,
            )
        )
        sanitized_resume_excerpt = self._resume_excerpt(resume_text)
        known_years_line = f"- Known years by stack: {known_years}.\n" if known_years else ""
        if render_language is SupportedLanguage.PORTUGUESE:
            known_years_line = (
                f"- Anos conhecidos por stack: {known_years}.\n" if known_years else ""
            )
            relevant_target_label = "Alvo relevante"
            highlighted_stacks_label = "Stacks destacadas"
            location_target_label = "Localizacao alvo"
            availability_label = "Disponibilidade"
            notes_label = "Notas"
        else:
            relevant_target_label = "Relevant target"
            highlighted_stacks_label = "Highlighted stacks"
            location_target_label = "Location target"
            availability_label = "Availability"
            notes_label = "Notes"
        return (
            "---\n"
            f"name: {settings.profile.name}\n"
            "header:\n"
            f'  - text: "{location_line}"\n'
            f'  - text: "{settings.profile.phone}"\n'
            f'  - text: "{settings.profile.email}"\n'
            f"    link: mailto:{settings.profile.email}\n"
            "---\n\n"
            f"## {localized_section_label('summary', render_language)}\n\n"
            f"{_normalize_resume_copy(summary)}\n\n"
            f"## {localized_section_label('experience', render_language)}\n\n"
            f"**{relevant_target_label}:** {posting.title} — {posting.company_name}\n\n"
            f"- {highlighted_stacks_label}: {stacks}.\n"
            f"{known_years_line}"
            f"- {location_target_label}: {posting.location or settings.search.location}.\n"
            f"- {availability_label}: {settings.profile.availability}.\n\n"
            f"## {localized_section_label('skills', render_language)}\n\n"
            f"{stacks}\n\n"
            f"## {notes_label}\n\n"
            f"{sanitized_resume_excerpt}\n"
        )

    def _build_target_stack_hints(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        resume_snapshot: ResumeSourceSnapshot,
        role_target: str | None,
        matched_specializations: tuple[str, ...],
    ) -> tuple[str, ...]:
        resume_evidence_keywords = self._build_resume_evidence_keywords(
            settings=settings,
            resume_snapshot=resume_snapshot,
        )
        evidence_tokens = {_normalize_keyword(keyword) for keyword in resume_evidence_keywords}
        merged: list[str] = []
        seen: set[str] = set()

        profile_keywords = _ROLE_TARGET_PROFILE_KEYWORDS.get(
            _normalize_keyword(role_target or ""),
            (),
        )
        posting_keywords = _extract_posting_keywords(posting)
        title_keywords = _extract_keyword_labels(posting.title)
        desired_keywords = _merge_keyword_sequences(
            tuple(
                keyword
                for keyword in title_keywords
                if _normalize_keyword(keyword) in _SUMMARY_DETAIL_KEYWORDS
            ),
            matched_specializations,
            tuple(
                specialization
                for posting_keyword in posting_keywords
                for specialization in _SPECIALIZATION_FALLBACK_KEYWORDS.get(
                    _normalize_keyword(posting_keyword),
                    (posting_keyword,),
                )
            ),
            tuple(profile_keywords),
            posting_keywords,
            _screening_capability_terms(settings),
        )

        for keyword in desired_keywords:
            token = _normalize_keyword(keyword)
            if not token or token in seen:
                continue
            if token in evidence_tokens:
                seen.add(token)
                merged.append(keyword)

        if not merged:
            fallback_keywords = tuple(
                keyword
                for keyword in (
                    "automation",
                    "backend",
                    "full stack",
                    "system integrations",
                    "typescript",
                    "javascript",
                    "python",
                    "rag",
                    "applied ai",
                )
                if _normalize_keyword(keyword) in evidence_tokens
            )
            if fallback_keywords:
                return fallback_keywords[:5]
            return ("automation", "system integrations", "Python")
        return tuple(merged[:12])

    def _build_editorial_focus_keywords(
        self,
        *,
        posting: JobPosting,
        broad_focus_keywords: tuple[str, ...],
        matched_specializations: tuple[str, ...],
    ) -> tuple[str, ...]:
        allowed_tokens = {
            *(_normalize_keyword(keyword) for keyword in _HEADLINE_SPECIALIZATION_KEYWORDS),
            *(_normalize_keyword(keyword) for keyword in _SUMMARY_DETAIL_KEYWORDS),
        }
        normalized_broad_focus = {
            _normalize_keyword(keyword): keyword
            for keyword in broad_focus_keywords
            if _normalize_keyword(keyword)
        }
        title_keywords = _extract_keyword_labels(posting.title)
        title_exact_focus = tuple(
            keyword for keyword in title_keywords if _normalize_keyword(keyword) in allowed_tokens
        )
        title_specializations = tuple(
            specialization
            for title_keyword in title_keywords
            for specialization in _SPECIALIZATION_FALLBACK_KEYWORDS.get(
                _normalize_keyword(title_keyword),
                (title_keyword,),
            )
        )
        desired_keywords = _merge_keyword_sequences(
            title_exact_focus,
            matched_specializations,
            title_specializations,
            title_keywords,
        )
        editorial_focus: list[str] = []
        seen: set[str] = set()
        for keyword in desired_keywords:
            token = _normalize_keyword(keyword)
            if not token or token in seen or token not in allowed_tokens:
                continue
            resolved_keyword = normalized_broad_focus.get(token)
            if resolved_keyword is None:
                continue
            seen.add(token)
            editorial_focus.append(resolved_keyword)
        return tuple(editorial_focus[:3])

    def _build_resume_evidence_keywords(
        self,
        *,
        settings: UserAgentSettings,
        resume_snapshot: ResumeSourceSnapshot,
    ) -> tuple[str, ...]:
        observed: list[str] = []
        observed.extend(
            _extract_keyword_labels(
                "\n".join(
                    filter(
                        None,
                        (
                            resume_snapshot.header_role or "",
                            resume_snapshot.summary or "",
                            "\n".join(resume_snapshot.skill_lines),
                            "\n".join(
                                entry.title
                                for entry in resume_snapshot.experience_entries
                                if entry.title
                            ),
                            "\n".join(
                                entry.company_name or ""
                                for entry in resume_snapshot.experience_entries
                            ),
                            "\n".join(
                                bullet
                                for entry in resume_snapshot.experience_entries
                                for bullet in entry.bullets
                            ),
                            "\n".join(
                                certification.name
                                for certification in resume_snapshot.certifications
                            ),
                        ),
                    ),
                ),
            )
        )
        observed.extend(_screening_capability_terms(settings))
        return _merge_keyword_sequences(tuple(observed))

    def _select_primary_role_target(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> str | None:
        role_targets = tuple(target for target in settings.search.keywords if target.strip())
        if not role_targets:
            return None
        title_text = _normalize_comparison_text(posting.title)
        title_tokens = set(title_text.split())
        best_target = role_targets[0]
        best_score = -1.0
        for role_target in role_targets:
            normalized_target = _normalize_keyword(role_target)
            alias_patterns = _ROLE_TARGET_ALIAS_PATTERNS.get(normalized_target, ())
            if normalized_target in title_text:
                score = 1.0
            elif alias_patterns and any(
                re.search(pattern, title_text) for pattern in alias_patterns
            ):
                score = 1.0
            else:
                target_tokens = [token for token in normalized_target.split() if token]
                title_overlap = 0.0
                if target_tokens:
                    title_overlap = sum(token in title_tokens for token in target_tokens) / len(
                        target_tokens
                    )
                score = title_overlap
            if score > best_score:
                best_score = score
                best_target = role_target
        return best_target

    def _build_resume_source_snapshot(
        self,
        *,
        settings: UserAgentSettings,
        resume_text: str | None,
    ) -> ResumeSourceSnapshot:
        if resume_text is None:
            return ResumeSourceSnapshot()
        normalized_text = _normalize_extracted_resume_text(resume_text)
        if not normalized_text:
            return ResumeSourceSnapshot()

        header_lines, section_map = _split_resume_sections(normalized_text)
        role_line = _first_non_empty_line(header_lines[1:]) if len(header_lines) > 1 else None
        summary = " ".join(section_map.get("Summary", ())).strip() or None
        phone = _extract_phone(normalized_text) or _normalize_optional_text(settings.profile.phone)
        email = _extract_email(normalized_text) or _normalize_optional_text(settings.profile.email)
        city = _extract_city_hint(header_lines) or _normalize_optional_text(settings.profile.city)
        portfolio_hint = _extract_portfolio_hint(normalized_text)
        skill_lines = _coalesce_wrapped_skill_lines(section_map.get("Skills", ()))

        return ResumeSourceSnapshot(
            header_role=role_line,
            summary=summary,
            experience_entries=_parse_experience_entries(section_map.get("Experience", ())),
            certifications=_parse_certification_entries(section_map.get("Certifications", ())),
            education_entries=_parse_education_entries(section_map.get("Education", ())),
            skill_lines=skill_lines,
            additional_sections=tuple(
                (title, _filter_non_empty_lines(lines))
                for title, lines in section_map.items()
                if title not in {"Summary", "Experience", "Certifications", "Education", "Skills"}
            ),
            word_count=len(normalized_text.split()),
            phone=phone,
            email=email,
            city=city,
            portfolio_hint=portfolio_hint,
        )

    def _build_preserved_resume_markdown(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        resume_snapshot: ResumeSourceSnapshot,
        adaptation_plan: ResumeAdaptationPlan | None = None,
        render_language: SupportedLanguage = SupportedLanguage.ENGLISH,
    ) -> str:
        adaptation_plan = adaptation_plan or ResumeAdaptationPlan()
        location = _escape_yaml_scalar(
            _localize_resume_meta_text(
                resume_snapshot.city or settings.profile.city,
                render_language,
            ),
        )
        phone = _escape_yaml_scalar(resume_snapshot.phone or settings.profile.phone)
        email = _escape_yaml_scalar(resume_snapshot.email or settings.profile.email)
        header_role = _escape_yaml_scalar(
            _normalize_resume_copy(
                adaptation_plan.headline
                or resume_snapshot.header_role
                or "Full Stack Software Engineer",
            ),
        )
        prioritized_experience_entries = self._prioritize_experience_entries(
            resume_snapshot=resume_snapshot,
            adaptation_plan=adaptation_plan,
        )
        prioritized_skill_lines = self._prioritize_skill_lines(
            skill_lines=resume_snapshot.skill_lines,
            adaptation_plan=adaptation_plan,
        )
        summary_text = _normalize_resume_copy(
            adaptation_plan.summary
            or resume_snapshot.summary
            or (
                "Full Stack Software Engineer with strong experience across software "
                "delivery, automation, and applied AI."
            ),
        )
        summary_text = _localize_resume_phrase_overrides(summary_text, render_language)
        header_items: list[tuple[str, str | None, str | None]] = [
            (
                "  - text: |",
                f'      <span style="font-size: 1.15em; font-weight: bold;">{header_role}</span>',
                None,
            ),
            (
                f'  - text: "{location}"',
                "    newLine: true",
                None,
            ),
        ]
        if phone:
            header_items.append(
                (
                    f'  - text: "{phone}"',
                    None,
                    None,
                ),
            )
        if email:
            header_items.append(
                (
                    f'  - text: "{email}"',
                    f"    link: mailto:{email}",
                    None,
                ),
            )
        if settings.profile.linkedin_url:
            linkedin_url = str(settings.profile.linkedin_url)
            linkedin_label = _escape_yaml_scalar(_display_label_for_url(linkedin_url))
            header_items.append(
                (
                    (
                        f'  - text: "{localized_field_label("linkedin", render_language)}: '
                        f'{linkedin_label}"'
                    ),
                    f"    link: {linkedin_url}",
                    "    newLine: true",
                ),
            )
        if settings.profile.github_url:
            github_url = str(settings.profile.github_url)
            github_label = _escape_yaml_scalar(_display_label_for_url(github_url))
            header_items.append(
                (
                    (
                        f'  - text: "{localized_field_label("github", render_language)}: '
                        f'{github_label}"'
                    ),
                    f"    link: {github_url}",
                    None,
                ),
            )
        elif resume_snapshot.portfolio_hint:
            header_items.append(
                (
                    f'  - text: "{_escape_yaml_scalar(resume_snapshot.portfolio_hint)}"',
                    None,
                    None,
                ),
            )
        if settings.profile.portfolio_url:
            portfolio_url = str(settings.profile.portfolio_url)
            header_items.append(
                (
                    f'  - text: "{_escape_yaml_scalar(_display_label_for_url(portfolio_url))}"',
                    f"    link: {portfolio_url}",
                    None,
                ),
            )

        markdown_lines = ["---", f"name: {settings.profile.name}", "header:"]
        for item_lines in header_items:
            primary, *secondary_lines = item_lines
            markdown_lines.append(primary)
            for secondary in secondary_lines:
                if secondary:
                    markdown_lines.append(secondary)
        markdown_lines.extend(
            [
                "---",
                "",
                f"## {localized_section_label('summary', render_language)}",
                "",
                summary_text,
                "",
                f"## {localized_section_label('experience', render_language)}",
                "",
            ],
        )

        for entry in prioritized_experience_entries:
            markdown_lines.append(
                f"**{_localize_resume_phrase_overrides(entry.title, render_language)}**"
            )
            if entry.company_name:
                markdown_lines.append(
                    "  ~ "
                    + _normalize_resume_copy(
                        _localize_resume_meta_text(entry.company_name, render_language),
                    )
                )
            if entry.date_range:
                markdown_lines.append(
                    "  ~ " + _localize_resume_meta_text(entry.date_range, render_language)
                )
            markdown_lines.append("")
            for bullet in entry.bullets:
                markdown_lines.append(
                    f"- {_localize_resume_phrase_overrides(bullet, render_language)}"
                )
            markdown_lines.append("")

        if resume_snapshot.certifications:
            markdown_lines.extend(
                [f"## {localized_section_label('certifications', render_language)}", ""]
            )
            for certification in resume_snapshot.certifications:
                markdown_lines.append(
                    f"**{_localize_resume_phrase_overrides(certification.name, render_language)}**"
                )
                if certification.issuer:
                    markdown_lines.append(
                        "  ~ "
                        + _localize_resume_phrase_overrides(
                            certification.issuer,
                            render_language,
                        )
                    )
                markdown_lines.append("")

        if resume_snapshot.education_entries:
            markdown_lines.extend(
                [f"## {localized_section_label('education', render_language)}", ""]
            )
            for education in resume_snapshot.education_entries:
                markdown_lines.append(
                    "**"
                    + _localize_resume_phrase_overrides(
                        education.institution,
                        render_language,
                    )
                    + "**"
                )
                if education.location:
                    markdown_lines.append(
                        "  ~ "
                        + _normalize_resume_copy(
                            _localize_resume_meta_text(education.location, render_language),
                        )
                    )
                markdown_lines.append("")
                if education.degree:
                    markdown_lines.append(
                        _localize_resume_phrase_overrides(education.degree, render_language)
                    )
                if education.date_range:
                    markdown_lines.append(
                        "  ~ " + _localize_resume_meta_text(education.date_range, render_language)
                    )
                markdown_lines.append("")

        if prioritized_skill_lines:
            markdown_lines.extend([f"## {localized_section_label('skills', render_language)}", ""])
            markdown_lines.extend(
                _localize_resume_phrase_overrides(line, render_language)
                for line in prioritized_skill_lines
            )
            markdown_lines.append("")

        for title, lines in resume_snapshot.additional_sections:
            if not lines:
                continue
            markdown_lines.extend(
                [_localize_resume_phrase_overrides(f"## {title}", render_language), ""]
            )
            markdown_lines.extend(
                _localize_resume_phrase_overrides(line, render_language) for line in lines
            )
            markdown_lines.append("")

        return (
            "\n".join(line.rstrip() for line in markdown_lines if line is not None).strip() + "\n"
        )

    def _prioritize_experience_entries(
        self,
        *,
        resume_snapshot: ResumeSourceSnapshot,
        adaptation_plan: ResumeAdaptationPlan,
    ) -> tuple[ResumeExperienceEntry, ...]:
        focus_keywords = adaptation_plan.focus_keywords or adaptation_plan.skill_focus
        if not focus_keywords:
            return resume_snapshot.experience_entries
        experience_focus_map = {
            _normalize_comparison_text(item.entry_hint): item.keywords
            for item in adaptation_plan.experience_focus
        }
        prioritized_entries: list[ResumeExperienceEntry] = []
        for entry in resume_snapshot.experience_entries:
            entry_haystack = _normalize_comparison_text(
                " ".join(filter(None, (entry.title, entry.company_name))),
            )
            entry_specific_keywords = next(
                (
                    keywords
                    for entry_hint, keywords in experience_focus_map.items()
                    if entry_hint and entry_hint in entry_haystack
                ),
                (),
            )
            prioritized_entries.append(
                ResumeExperienceEntry(
                    title=entry.title,
                    company_name=entry.company_name,
                    date_range=entry.date_range,
                    bullets=_sort_lines_by_keywords(
                        entry.bullets,
                        focus_keywords=(*entry_specific_keywords, *focus_keywords),
                    ),
                ),
            )
        return tuple(prioritized_entries)

    def _build_primary_focus_keywords(
        self,
        *,
        role_target: str | None,
        focus_keywords: tuple[str, ...],
    ) -> tuple[str, ...]:
        profile_keywords = _ROLE_TARGET_PROFILE_KEYWORDS.get(
            _normalize_keyword(role_target or ""),
            (),
        )
        normalized_profile_keywords = {
            _normalize_keyword(item) for item in profile_keywords if _normalize_keyword(item)
        }
        specialization_keywords = tuple(
            keyword
            for keyword in focus_keywords
            if _normalize_keyword(keyword) not in normalized_profile_keywords
            and _normalize_keyword(keyword)
            not in {
                "automation engineer",
                "automation developer",
                "rpa developer",
                "backend developer",
                "full stack developer",
            }
        )
        if not profile_keywords:
            return specialization_keywords[:5] or focus_keywords[:5]
        focus_tokens = {_normalize_keyword(keyword) for keyword in focus_keywords}
        matched_profile_keywords = tuple(
            keyword for keyword in profile_keywords if _normalize_keyword(keyword) in focus_tokens
        )
        return _merge_keyword_sequences(
            specialization_keywords[:5],
            matched_profile_keywords[:3],
            focus_keywords[:5],
        )[:5]

    def _prioritize_skill_lines(
        self,
        *,
        skill_lines: tuple[str, ...],
        adaptation_plan: ResumeAdaptationPlan,
    ) -> tuple[str, ...]:
        focus_keywords = adaptation_plan.skill_focus or adaptation_plan.focus_keywords
        if not focus_keywords:
            return skill_lines
        return _sort_lines_by_keywords(skill_lines, focus_keywords=focus_keywords)

    def _validate_generated_markdown(
        self,
        *,
        markdown: str,
        fallback_markdown: str,
        resume_snapshot: ResumeSourceSnapshot,
        render_language: SupportedLanguage,
    ) -> bool:
        if not _snapshot_has_structured_content(resume_snapshot):
            return True

        normalized_output = _normalize_comparison_text(markdown)
        _metadata, body_markdown = _parse_front_matter(markdown)
        generated_sections = {
            (canonical_resume_section_title(heading) or _normalize_comparison_text(heading)): lines
            for heading, lines in _split_markdown_body_sections(body_markdown)
        }
        required_headings = ["summary", "experience"]
        if resume_snapshot.certifications:
            required_headings.append("certifications")
        if resume_snapshot.education_entries:
            required_headings.append("education")
        if resume_snapshot.skill_lines:
            required_headings.append("skills")
        for heading in required_headings:
            if heading not in generated_sections:
                return False

        if any(phrase in normalized_output for phrase in _EDITORIAL_BANNED_PHRASES):
            return False

        summary_lines = generated_sections.get("summary", ())
        summary_text = " ".join(line.strip() for line in summary_lines if line.strip())
        if summary_text:
            if not _summary_passes_editorial_checks(summary_text):
                return False

        if resume_snapshot.experience_entries and _count_resume_entry_blocks(
            generated_sections.get("experience", ())
        ) < len(resume_snapshot.experience_entries):
            return False
        if resume_snapshot.certifications and _count_resume_entry_blocks(
            generated_sections.get("certifications", ())
        ) < len(resume_snapshot.certifications):
            return False
        if resume_snapshot.education_entries and _count_resume_entry_blocks(
            generated_sections.get("education", ())
        ) < len(resume_snapshot.education_entries):
            return False

        source_word_count = resume_snapshot.word_count
        output_word_count = len(markdown.split())
        if source_word_count >= 180 and output_word_count < int(source_word_count * 0.72):
            return False

        required_tokens = _required_resume_identity_tokens(resume_snapshot)
        for token in required_tokens:
            if token not in normalized_output:
                return False

        fallback_word_count = len(fallback_markdown.split())
        if fallback_word_count >= 180 and output_word_count < int(fallback_word_count * 0.72):
            return False
        if not self._markdown_aligns_with_target_language(
            markdown=markdown,
            target_language=render_language,
        ):
            return False
        return True

    def _markdown_aligns_with_target_language(
        self,
        *,
        markdown: str,
        target_language: SupportedLanguage,
    ) -> bool:
        if target_language is SupportedLanguage.ENGLISH:
            return True
        _metadata, body_markdown = _parse_front_matter(markdown)
        sampled_sections: list[str] = []
        for heading, lines in _split_markdown_body_sections(body_markdown):
            canonical_heading = canonical_resume_section_title(
                heading
            ) or _normalize_comparison_text(heading)
            if canonical_heading not in {"summary", "experience", "skills"}:
                continue
            sampled_sections.extend(line.strip() for line in lines if line.strip())
        sample_text = "\n".join(sampled_sections[:18]).strip()
        if not sample_text:
            return False
        detection = detect_text_language(
            sample_text,
            default_language=target_language,
            source="generated_resume_markdown",
        )
        return detection.language is target_language and detection.confidence >= 0.35

    def _resolve_resume_css(self, *, settings: UserAgentSettings) -> str:
        if settings.profile.resume_css is not None and settings.profile.resume_css.strip():
            if _looks_like_legacy_resume_css(settings.profile.resume_css):
                return _default_resume_css()
            return settings.profile.resume_css
        return _default_resume_css()

    def _resume_excerpt(self, resume_text: str | None) -> str:
        if resume_text is None:
            return "Resume content extraction unavailable; using profile snapshot data only."
        cleaned = " ".join(resume_text.split())
        if not cleaned:
            return "Resume text was empty after extraction; using profile snapshot data only."
        return cleaned[:900]

    def _create_response(
        self,
        *,
        api_key: str,
        model: str,
        prompt_payload: dict[str, object],
    ) -> dict[str, object]:
        return self._create_structured_response(
            api_key=api_key,
            model=model,
            developer_text=(
                "You produce a structured adaptation plan for a dynamic resume. "
                "The system will render the final Oh-My-CV markdown itself. "
                "Preserve the candidate's factual history exactly: same employers, "
                "same education, same certifications, same projects, and no "
                "invented claims. Optimize only by adjusting the headline, "
                "rewriting the professional summary, and highlighting truthful "
                "target-relevant keywords, skills, and experience emphasis. "
                "Write the headline and summary in the target_resume_language "
                "provided in language_context, even when the source resume uses "
                "a different language. "
                "Do not replace the candidate's base professional identity with "
                "a different role family unless the source resume itself already "
                "uses that identity. Treat role family and stack emphasis as "
                "separate decisions. "
                "Keep the headline concise: preserve the base role identity and "
                "append at most two concrete stack specializations when they are "
                "truthfully supported. Keep the summary to two or three concise "
                "sentences. "
                "Prefer minimal edits to the summary when the source version is "
                "already strong. Avoid recruiter cliches, keyword stuffing, and "
                "phrases like 'Targeted for' or 'Selected fit areas'. "
                "Never introduce frameworks, stacks, seniority claims, or tools "
                "outside the allowed_focus_keywords, "
                "allowed_skill_focus_keywords, and "
                "resume_evidence_keywords provided in the payload. "
                "Use allowed_focus_keywords only for the headline and summary. "
                "Use allowed_skill_focus_keywords only to prioritize skills and "
                "experience emphasis. "
                "Focus on improving match for the target job without changing the "
                "candidate's story. Return only valid JSON matching the schema."
            ),
            prompt_payload=prompt_payload,
            schema_name="dynamic_resume_plan",
            schema=_RESUME_ADAPTATION_SCHEMA,
        )

    def _create_structured_response(
        self,
        *,
        api_key: str,
        model: str,
        developer_text: str,
        prompt_payload: dict[str, object],
        schema_name: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        body = {
            "model": model,
            "input": [
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": developer_text,
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
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                },
            },
        }
        raw_body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=raw_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "dynamic_resume_openai_http_error",
                extra={"status": exc.code, "body": error_body[:1_000]},
            )
            raise RuntimeError(error_body) from exc
        parsed_payload = json.loads(payload)
        if not isinstance(parsed_payload, dict):
            msg = "OpenAI response payload was not a JSON object."
            raise RuntimeError(msg)
        return cast(dict[str, object], parsed_payload)

    def _extract_output_text(self, response_data: dict[str, object]) -> str:
        output = response_data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for chunk in content:
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("type") != "output_text":
                        continue
                    text = chunk.get("text")
                    if isinstance(text, str):
                        return text
        return ""

    def _looks_like_oh_my_cv_markdown(self, markdown: str) -> bool:
        normalized = markdown.strip()
        return normalized.startswith("---") and "## " in normalized

    def _render_markdown_to_pdf(
        self,
        *,
        markdown_path: Path,
        css_path: Path | None,
        output_pdf_path: Path,
    ) -> tuple[bool, str | None]:
        commands = self._build_render_commands(
            markdown_path=markdown_path,
            css_path=css_path,
            output_pdf_path=output_pdf_path,
        )
        last_error: str | None = None
        for command in commands:
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=self._runtime_settings.resume_dynamic_render_timeout_seconds,
                )
            except FileNotFoundError as exc:
                last_error = str(exc)
                continue
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                stdout = (exc.stdout or "").strip()
                last_error = stderr or stdout or str(exc)
                continue
            except subprocess.TimeoutExpired as exc:
                last_error = str(exc)
                continue
            if output_pdf_path.exists() and output_pdf_path.stat().st_size > 0:
                return True, None
        rendered_with_playwright, playwright_error = self._render_markdown_to_pdf_with_playwright(
            markdown_path=markdown_path,
            css_path=css_path,
            output_pdf_path=output_pdf_path,
        )
        if rendered_with_playwright:
            return True, None
        if playwright_error:
            if last_error:
                return False, f"{last_error}; {playwright_error}"
            return False, playwright_error
        return False, last_error

    def _render_markdown_to_pdf_with_playwright(
        self,
        *,
        markdown_path: Path,
        css_path: Path | None,
        output_pdf_path: Path,
    ) -> tuple[bool, str | None]:
        try:
            markdown_text = markdown_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return False, str(exc)
        css_text: str | None = None
        if css_path is not None:
            try:
                css_text = css_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                css_text = None

        document_html = _build_resume_html_document(markdown_text=markdown_text, css_text=css_text)
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.set_content(document_html, wait_until="load")
                    page.emulate_media(media="print")
                    page.pdf(
                        path=str(output_pdf_path),
                        format="A4",
                        print_background=True,
                    )
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        if output_pdf_path.exists() and output_pdf_path.stat().st_size > 0:
            return True, None
        return False, "playwright_pdf_output_not_created"

    def _build_render_commands(
        self,
        *,
        markdown_path: Path,
        css_path: Path | None,
        output_pdf_path: Path,
    ) -> list[list[str]]:
        commands: list[list[str]] = []
        if self._runtime_settings.resume_dynamic_render_command:
            template = self._runtime_settings.resume_dynamic_render_command
            formatted = template.format(
                markdown=str(markdown_path),
                pdf=str(output_pdf_path),
                css=str(css_path or ""),
            )
            commands.append(shlex.split(formatted))

        oh_my_cv_binary = shutil.which("oh-my-cv")
        default_commands: list[list[str]] = []
        if oh_my_cv_binary:
            default_commands.extend(
                [
                    [oh_my_cv_binary, "render", str(markdown_path), "-o", str(output_pdf_path)],
                    [
                        oh_my_cv_binary,
                        "render",
                        str(markdown_path),
                        "--output",
                        str(output_pdf_path),
                    ],
                ],
            )
        if css_path is not None:
            css_variants: list[list[str]] = []
            for command in default_commands:
                css_variants.append([*command, "--css", str(css_path)])
                css_variants.append([*command, "-c", str(css_path)])
            default_commands = [*css_variants, *default_commands]
        commands.extend(default_commands)
        return commands


def _extract_posting_keywords(posting: JobPosting) -> tuple[str, ...]:
    return _extract_keyword_labels(_resume_job_context_text(posting))


def _posting_has_usable_detail_context(posting: JobPosting) -> bool:
    return posting.detail_quality_score >= 0.55 and posting.detail_description_score >= 0.45


def _resume_job_context_text(posting: JobPosting) -> str:
    if _posting_has_usable_detail_context(posting):
        return f"{posting.title}\n{posting.description_raw}"
    return posting.title


def _extract_keyword_labels(text: str) -> tuple[str, ...]:
    haystack = text.lower()
    matches: list[tuple[int, int, str]] = []
    for pattern_index, (label, patterns) in enumerate(_TARGET_KEYWORD_PATTERNS):
        earliest_position: int | None = None
        for pattern in patterns:
            match = re.search(pattern, haystack)
            if match is None:
                continue
            position = match.start()
            if earliest_position is None or position < earliest_position:
                earliest_position = position
        if earliest_position is None:
            continue
        matches.append((earliest_position, pattern_index, label))
    matches.sort()
    return tuple(label for _, _, label in matches)


def _normalize_keyword(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _merge_keyword_sequences(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            candidate = item.strip()
            token = _normalize_keyword(candidate)
            if not token or token in seen:
                continue
            seen.add(token)
            merged.append(candidate)
    return tuple(merged)


def _format_keyword_phrase(keywords: tuple[str, ...]) -> str:
    cleaned = [_display_keyword_label(keyword) for keyword in keywords if keyword.strip()]
    if not cleaned:
        return "the most relevant experience from the source resume"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"


def _normalize_resume_copy(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized:
        return normalized
    for pattern, replacement in _CANONICAL_TEXT_REPLACEMENTS:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+([,.;:])", r"\1", normalized)
    normalized = re.sub(r"\s{2,}", " ", normalized)
    return normalized.strip()


def _extract_base_role_identity(header_role: str) -> str:
    normalized = _normalize_resume_copy(header_role)
    primary_segment = normalized.split("|", maxsplit=1)[0].strip()
    return primary_segment or normalized


def _select_headline_specializations(
    *,
    headline_role: str,
    focus_keywords: tuple[str, ...],
) -> tuple[str, ...]:
    normalized_role = _normalize_keyword(headline_role)
    selected: list[str] = []
    seen: set[str] = set()
    for keyword in focus_keywords:
        normalized_keyword = _normalize_keyword(keyword)
        if not normalized_keyword or normalized_keyword in seen:
            continue
        if normalized_keyword in normalized_role:
            continue
        if normalized_keyword not in _HEADLINE_SPECIALIZATION_KEYWORDS:
            continue
        seen.add(normalized_keyword)
        selected.append(keyword)
    return tuple(selected[:2])


def _resolve_editorial_role_target(
    *,
    role_target: str | None,
    posting_title: str,
    focus_keywords: tuple[str, ...],
) -> str | None:
    normalized_role_target = _normalize_keyword(role_target or "")
    if normalized_role_target and normalized_role_target not in _GENERIC_EDITORIAL_ROLE_TARGETS:
        return normalized_role_target

    normalized_title = _normalize_comparison_text(posting_title)
    focus_tokens = {
        _normalize_keyword(keyword) for keyword in focus_keywords if _normalize_keyword(keyword)
    }
    candidate_roles = (
        "backend developer",
        "full stack developer",
        "automation engineer",
        "automation developer",
        "rpa developer",
    )
    for candidate_role in candidate_roles:
        alias_patterns = _ROLE_TARGET_ALIAS_PATTERNS.get(candidate_role, ())
        if not alias_patterns or not any(
            re.search(pattern, normalized_title) for pattern in alias_patterns
        ):
            continue
        profile_tokens = {
            _normalize_keyword(keyword)
            for keyword in _ROLE_TARGET_PROFILE_KEYWORDS.get(candidate_role, ())
            if _normalize_keyword(keyword)
        }
        if focus_tokens & profile_tokens:
            return candidate_role

    return normalized_role_target or None


def _resolve_headline_role_scope_label(
    *,
    headline_role: str,
    editorial_role_target: str | None,
    focus_keywords: tuple[str, ...],
) -> str | None:
    normalized_role_target = _normalize_keyword(editorial_role_target or "")
    if not normalized_role_target:
        return None
    role_scope_label = _ROLE_TARGET_HEADLINE_SCOPE_LABELS.get(normalized_role_target)
    if role_scope_label is None:
        return None
    normalized_headline = _normalize_comparison_text(headline_role)
    alias_patterns = _ROLE_TARGET_ALIAS_PATTERNS.get(normalized_role_target, ())
    if normalized_role_target in normalized_headline or any(
        re.search(pattern, normalized_headline) for pattern in alias_patterns
    ):
        return None
    profile_tokens = {
        _normalize_keyword(keyword)
        for keyword in _ROLE_TARGET_PROFILE_KEYWORDS.get(normalized_role_target, ())
        if _normalize_keyword(keyword)
    }
    focus_tokens = {
        _normalize_keyword(keyword) for keyword in focus_keywords if _normalize_keyword(keyword)
    }
    if not focus_tokens & profile_tokens:
        return None
    return role_scope_label


def _split_summary_sentences(summary: str) -> tuple[str, ...]:
    normalized = _normalize_resume_copy(summary)
    if not normalized:
        return ()
    sentence_matches = re.findall(r"[^.!?]+[.!?]?", normalized)
    sentences: list[str] = []
    for sentence in sentence_matches:
        candidate = sentence.strip()
        if not candidate:
            continue
        if candidate[-1] not in ".!?":
            candidate += "."
        sentences.append(_normalize_resume_copy(candidate))
    return tuple(sentences)


def _sentence_signature(sentence: str) -> str:
    return _normalize_comparison_text(sentence)


def _trim_summary_text(summary: str, *, max_words: int = 52, max_sentences: int = 3) -> str:
    normalized = _normalize_resume_copy(summary)
    if not normalized:
        return normalized
    sentences = list(_split_summary_sentences(normalized))
    if sentences:
        limited_sentences = sentences[:max_sentences]
        candidate = " ".join(limited_sentences).strip()
    else:
        candidate = normalized
    words = candidate.split()
    if len(words) > max_words:
        candidate = " ".join(words[:max_words]).rstrip(",;:")
        if candidate and candidate[-1] not in ".!?":
            candidate += "."
    return _normalize_resume_copy(candidate)


def _assemble_summary_sentences(
    sentences: tuple[str, ...],
    *,
    max_words: int = 52,
    max_sentences: int = 3,
) -> str:
    selected: list[str] = []
    seen: set[str] = set()
    word_count = 0
    for sentence in sentences:
        candidate = _normalize_resume_copy(sentence)
        if not candidate:
            continue
        signature = _sentence_signature(candidate)
        if not signature or signature in seen:
            continue
        sentence_words = len(candidate.split())
        if selected and word_count + sentence_words > max_words:
            continue
        if not selected and sentence_words > max_words:
            return _trim_summary_text(candidate, max_words=max_words, max_sentences=max_sentences)
        selected.append(candidate)
        seen.add(signature)
        word_count += sentence_words
        if len(selected) >= max_sentences:
            break
    return _normalize_resume_copy(" ".join(selected))


def _summary_focus_coverage(summary: str, focus_keywords: tuple[str, ...]) -> int:
    normalized_summary = _normalize_comparison_text(summary)
    matches = 0
    for keyword in focus_keywords:
        token = _normalize_keyword(keyword)
        if token and token in normalized_summary:
            matches += 1
    return matches


def _select_summary_focus_keywords(
    *,
    focus_keywords: tuple[str, ...],
    posting_keywords: tuple[str, ...],
    existing_summary_text: str = "",
) -> tuple[str, ...]:
    posting_tokens = {
        _normalize_keyword(keyword) for keyword in posting_keywords if keyword.strip()
    }
    normalized_existing_summary = _normalize_comparison_text(existing_summary_text)
    selected: list[str] = []
    seen: set[str] = set()

    def add_keyword(candidate: str) -> None:
        normalized_candidate = _normalize_keyword(candidate)
        if not normalized_candidate or normalized_candidate in seen:
            return
        seen.add(normalized_candidate)
        selected.append(candidate)

    def is_fresh_keyword(candidate: str) -> bool:
        normalized_candidate = _normalize_keyword(candidate)
        return bool(normalized_candidate) and (
            normalized_candidate not in normalized_existing_summary
        )

    for keyword in focus_keywords:
        normalized_keyword = _normalize_keyword(keyword)
        if (
            normalized_keyword
            and normalized_keyword in posting_tokens
            and normalized_keyword in _HEADLINE_SPECIALIZATION_KEYWORDS
            and is_fresh_keyword(keyword)
        ):
            add_keyword(keyword)

    for keyword in focus_keywords:
        normalized_keyword = _normalize_keyword(keyword)
        if (
            normalized_keyword
            and normalized_keyword in posting_tokens
            and normalized_keyword in _SUMMARY_DETAIL_KEYWORDS
            and is_fresh_keyword(keyword)
        ):
            add_keyword(keyword)

    if selected:
        return tuple(selected[:3])

    for keyword in focus_keywords:
        normalized_keyword = _normalize_keyword(keyword)
        if (
            normalized_keyword
            and normalized_keyword in posting_tokens
            and normalized_keyword in _HEADLINE_SPECIALIZATION_KEYWORDS
        ):
            add_keyword(keyword)

    for keyword in focus_keywords:
        normalized_keyword = _normalize_keyword(keyword)
        if (
            normalized_keyword
            and normalized_keyword in posting_tokens
            and normalized_keyword in _SUMMARY_DETAIL_KEYWORDS
        ):
            add_keyword(keyword)

    if selected:
        return tuple(selected[:3])

    for keyword in focus_keywords:
        normalized_keyword = _normalize_keyword(keyword)
        if (
            normalized_keyword
            and normalized_keyword in _HEADLINE_SPECIALIZATION_KEYWORDS
            and is_fresh_keyword(keyword)
        ):
            add_keyword(keyword)

    for keyword in focus_keywords:
        normalized_keyword = _normalize_keyword(keyword)
        if (
            normalized_keyword
            and normalized_keyword in _SUMMARY_DETAIL_KEYWORDS
            and is_fresh_keyword(keyword)
        ):
            add_keyword(keyword)

    if selected:
        return tuple(selected[:3])

    for keyword in focus_keywords:
        normalized_keyword = _normalize_keyword(keyword)
        if normalized_keyword and normalized_keyword in _HEADLINE_SPECIALIZATION_KEYWORDS:
            add_keyword(keyword)

    for keyword in focus_keywords:
        normalized_keyword = _normalize_keyword(keyword)
        if normalized_keyword and normalized_keyword in _SUMMARY_DETAIL_KEYWORDS:
            add_keyword(keyword)

    return tuple(selected[:3])


def _build_summary_focus_sentence(
    *,
    role_target: str | None,
    focus_keywords: tuple[str, ...],
    posting_keywords: tuple[str, ...],
    lead_sentence: str = "",
) -> str:
    selected_focus = _select_summary_focus_keywords(
        focus_keywords=focus_keywords,
        posting_keywords=posting_keywords,
        existing_summary_text=lead_sentence,
    )
    normalized_role_target = _normalize_keyword(role_target or "")
    scope = _ROLE_TARGET_SUMMARY_SCOPES.get(normalized_role_target)
    if selected_focus:
        focus_phrase = _format_keyword_phrase(selected_focus)
        if scope:
            return _normalize_resume_copy(
                f"Recent work emphasizes {focus_phrase} across {scope}.",
            )
        return _normalize_resume_copy(
            f"Recent work emphasizes {focus_phrase} across production software delivery.",
        )
    return _build_role_alignment_sentence(
        role_target=role_target,
        focus_keywords=focus_keywords,
    )


def _select_summary_closing_sentence(
    *,
    base_sentences: tuple[str, ...],
    existing_sentences: tuple[str, ...],
) -> str:
    existing_signatures = {
        _sentence_signature(sentence)
        for sentence in existing_sentences
        if _sentence_signature(sentence)
    }
    candidates: list[tuple[int, int, str]] = []
    for index, sentence in enumerate(base_sentences[1:], start=1):
        signature = _sentence_signature(sentence)
        if not signature or signature in existing_signatures:
            continue
        score = _summary_sentence_quality_score(sentence)
        if score <= 0:
            continue
        candidates.append((score, -index, sentence))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][2]


def _summary_sentence_quality_score(sentence: str) -> int:
    normalized_sentence = _normalize_comparison_text(sentence)
    if not normalized_sentence:
        return 0
    score = 0
    if any(
        token in normalized_sentence
        for token in (
            "impact",
            "delivery",
            "reliable",
            "products",
            "platforms",
            "systems",
            "integrations",
            "automation",
            "production",
            "business",
        )
    ):
        score += 6
    if any(
        token in normalized_sentence
        for token in (
            "enthusiast",
            "interest",
            "interested",
            "passion",
            "curiosity",
        )
    ):
        score -= 8
    word_count = len(sentence.split())
    if 8 <= word_count <= 20:
        score += 2
    return score


def _build_role_alignment_sentence(
    *,
    role_target: str | None,
    focus_keywords: tuple[str, ...],
) -> str:
    normalized_role_target = _normalize_keyword(role_target or "")
    mapped_sentence = _ROLE_TARGET_ALIGNMENT_SENTENCES.get(normalized_role_target)
    if mapped_sentence is not None:
        return _normalize_resume_copy(mapped_sentence)
    if not focus_keywords:
        return ""
    focus_phrase = _format_keyword_phrase(focus_keywords[:3])
    return _normalize_resume_copy(
        f"Recent work is especially relevant to teams focused on {focus_phrase}.",
    )


def _summary_passes_editorial_checks(summary: str | None) -> bool:
    if summary is None:
        return False
    normalized_summary = _normalize_comparison_text(summary)
    if not normalized_summary:
        return False
    if any(phrase in normalized_summary for phrase in _EDITORIAL_BANNED_PHRASES):
        return False
    if len(summary.split()) > 70:
        return False
    sentence_count = len(re.findall(r"[.!?]+", summary))
    if sentence_count > 3:
        return False
    return True


def _sort_lines_by_keywords(
    lines: tuple[str, ...],
    *,
    focus_keywords: tuple[str, ...],
) -> tuple[str, ...]:
    normalized_keywords = tuple(
        token for token in (_normalize_keyword(keyword) for keyword in focus_keywords) if token
    )
    if not normalized_keywords:
        return lines
    scored_lines = [
        (_line_keyword_score(line, normalized_keywords), index, line)
        for index, line in enumerate(lines)
    ]
    if all(score == 0 for score, _index, _line in scored_lines):
        return lines
    scored_lines.sort(key=lambda item: (-item[0], item[1]))
    return tuple(line for _score, _index, line in scored_lines)


def _line_keyword_score(line: str, focus_keywords: tuple[str, ...]) -> int:
    normalized_line = _normalize_comparison_text(line)
    if not normalized_line:
        return 0
    score = 0
    for keyword in focus_keywords:
        if keyword in normalized_line:
            score += 12
            if normalized_line.startswith(keyword):
                score += 4
    return score


def _display_keyword_label(keyword: str) -> str:
    mapping = {
        "ai": "AI",
        "ai-assisted workflows": "AI-assisted workflows",
        "api": "APIs",
        "applied ai": "Applied AI",
        "aws": "AWS",
        "azure": "Azure",
        "automation engineer": "Automation Engineer",
        "automation developer": "Automation Developer",
        "docker": "Docker",
        "database modeling": "database modeling",
        "expo": "Expo",
        "fastapi": "FastAPI",
        "frontend": "frontend",
        "full stack": "full stack",
        "full stack developer": "Full Stack Developer",
        "software engineer": "Software Engineer",
        "gcp": "GCP",
        "internal tools": "internal tools",
        "java": "Java",
        "javascript": "JavaScript",
        "kubernetes": "Kubernetes",
        "langchain": "LangChain",
        "llm": "LLMs",
        "mobile": "mobile",
        "microservices": "microservices",
        "observability": "observability",
        "process orchestration": "process orchestration",
        "python": "Python",
        "rag": "RAG",
        "react": "React",
        "react native": "React Native",
        "rpa": "RPA",
        "rpa developer": "RPA Developer",
        "system integrations": "system integrations",
        "typescript": "TypeScript",
        "uipath": "UiPath",
        "workflow automation": "workflow automation",
        "backend developer": "Backend Developer",
        "backend": "backend",
        "automation": "automation",
        "chatbot systems": "chatbot systems",
    }
    normalized = _normalize_keyword(keyword)
    return mapping.get(normalized, keyword.strip())


def _display_role_target_label(role_target: str | None) -> str:
    if role_target is None or not role_target.strip():
        return "Full Stack Software Engineer"
    return _display_keyword_label(role_target.strip())


def _resume_snapshot_to_payload(snapshot: ResumeSourceSnapshot) -> dict[str, object]:
    return {
        "header_role": snapshot.header_role,
        "summary": snapshot.summary,
        "experience_entries": [
            {
                "title": entry.title,
                "company_name": entry.company_name,
                "date_range": entry.date_range,
                "bullets": list(entry.bullets),
            }
            for entry in snapshot.experience_entries
        ],
        "certifications": [
            {"name": entry.name, "issuer": entry.issuer} for entry in snapshot.certifications
        ],
        "education_entries": [
            {
                "institution": entry.institution,
                "degree": entry.degree,
                "location": entry.location,
                "date_range": entry.date_range,
            }
            for entry in snapshot.education_entries
        ],
        "skill_lines": list(snapshot.skill_lines),
        "additional_sections": [
            {"title": title, "lines": list(lines)} for title, lines in snapshot.additional_sections
        ],
        "word_count": snapshot.word_count,
        "phone": snapshot.phone,
        "email": snapshot.email,
        "city": snapshot.city,
        "portfolio_hint": snapshot.portfolio_hint,
    }


def _snapshot_has_structured_content(snapshot: ResumeSourceSnapshot) -> bool:
    return bool(
        snapshot.summary
        or snapshot.experience_entries
        or snapshot.certifications
        or snapshot.education_entries
        or snapshot.skill_lines
    )


def _normalize_extracted_resume_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text.replace("\f", "\n"))
    replacements = {
        "iago": "Thiago",
        "Soware": "Software",
        "soware": "software",
        "Certi�cations": "Certifications",
        "Certi�cation": "Certification",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized.strip()


def _split_resume_sections(text: str) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    header_lines: list[str] = []
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        canonical_title = canonical_resume_section_title(line)
        if canonical_title is not None:
            current_section = localized_section_label(canonical_title, SupportedLanguage.ENGLISH)
            sections.setdefault(current_section, [])
            continue
        if current_section is None:
            header_lines.append(line)
            continue
        sections.setdefault(current_section, []).append(line)
    return tuple(header_lines), {key: tuple(value) for key, value in sections.items()}


def _first_non_empty_line(lines: list[str] | tuple[str, ...]) -> str | None:
    for line in lines:
        if line.strip():
            return line.strip()
    return None


def _filter_non_empty_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(line.strip() for line in lines if line.strip())


def _coalesce_wrapped_skill_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line or not merged:
            merged.append(line)
            continue
        merged[-1] = f"{merged[-1]} {line}".strip()
    return tuple(merged)


def _extract_phone(text: str) -> str | None:
    match = re.search(r"(\+\d{1,3}\s?\d{2}\s?\d{4,5}-?\d{4})", text)
    if match:
        return match.group(1).strip()
    return None


def _extract_email(text: str) -> str | None:
    match = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _extract_portfolio_hint(text: str) -> str | None:
    for match in re.findall(r"\b(?:https?://)?([A-Z0-9.-]+\.[A-Z]{2,})\b", text, re.IGNORECASE):
        match_text = str(match)
        lowered = match_text.lower()
        if lowered.endswith("outlook.com"):
            continue
        return match_text
    return None


def _extract_city_hint(header_lines: tuple[str, ...]) -> str | None:
    for line in header_lines:
        if "brazil" in line.lower():
            return line.split("|")[0].strip()
    return None


def _parse_experience_entries(lines: tuple[str, ...]) -> tuple[ResumeExperienceEntry, ...]:
    items = [line for line in lines if line.strip()]
    entries: list[ResumeExperienceEntry] = []
    index = 0
    while index < len(items):
        current_line = items[index]
        parsed_heading = _parse_resume_heading_line(current_line)
        title = current_line
        if _is_resume_bullet(title):
            index += 1
            continue
        company_name: str | None = None
        date_range: str | None = None
        bullets: list[str] = []

        if parsed_heading is not None:
            title, company_name, date_range = parsed_heading
            index += 1
        else:
            index += 1
            if (
                index < len(items)
                and not _is_resume_bullet(items[index])
                and not _looks_like_date_range(items[index])
            ):
                company_name = items[index]
                index += 1
            if index < len(items) and _looks_like_date_range(items[index]):
                date_range = items[index]
                index += 1

        while index < len(items):
            current = items[index]
            if _looks_like_next_experience_entry(items, index):
                break
            if _is_resume_bullet(current):
                bullet = _strip_resume_bullet(current)
                index += 1
                while index < len(items):
                    continuation = items[index]
                    if _is_resume_bullet(continuation) or _looks_like_next_experience_entry(
                        items,
                        index,
                    ):
                        break
                    bullet = f"{bullet} {continuation}".strip()
                    index += 1
                bullets.append(bullet)
                continue
            if bullets:
                bullets[-1] = f"{bullets[-1]} {current}".strip()
            index += 1

        entries.append(
            ResumeExperienceEntry(
                title=title,
                company_name=company_name,
                date_range=date_range,
                bullets=tuple(bullets),
            ),
        )
    return tuple(entry for entry in entries if entry.title)


def _parse_certification_entries(lines: tuple[str, ...]) -> tuple[ResumeCertificationEntry, ...]:
    items = [line for line in lines if line.strip()]
    if not items:
        return ()
    layout_entries: list[ResumeCertificationEntry] = []
    for item in items:
        columns = _split_layout_columns(item)
        if len(columns) >= 2:
            layout_entries.append(
                ResumeCertificationEntry(
                    name=columns[0],
                    issuer=" ".join(columns[1:]).strip(),
                ),
            )
    if layout_entries and len(layout_entries) == len(items):
        return tuple(layout_entries)
    if len(items) % 2 == 0:
        midpoint = len(items) // 2
        names = items[:midpoint]
        issuers = items[midpoint:]
        return tuple(
            ResumeCertificationEntry(name=name, issuer=issuer)
            for name, issuer in zip(names, issuers, strict=True)
        )
    return tuple(ResumeCertificationEntry(name=item) for item in items)


def _parse_education_entries(lines: tuple[str, ...]) -> tuple[ResumeEducationEntry, ...]:
    items = [line for line in lines if line.strip()]
    if not items:
        return ()
    institution = items[0]
    degree = items[1] if len(items) > 1 else None
    location = items[2] if len(items) > 2 else None
    date_range = items[3] if len(items) > 3 else None

    first_columns = _split_layout_columns(items[0])
    if len(first_columns) >= 2:
        institution = first_columns[0]
        location = " ".join(first_columns[1:]).strip() or location
    if len(items) > 1:
        second_columns = _split_layout_columns(items[1])
        if len(second_columns) >= 2 and _looks_like_date_range(second_columns[-1]):
            degree = " ".join(second_columns[:-1]).strip()
            date_range = second_columns[-1]
    return (
        ResumeEducationEntry(
            institution=institution,
            degree=degree,
            location=location,
            date_range=date_range,
        ),
    )


def _looks_like_date_range(value: str) -> bool:
    return bool(re.fullmatch(r"\d{2}/\d{4}\s*-\s*(?:Present|\d{2}/\d{4})", value.strip()))


def _localize_resume_meta_text(
    text: str | None,
    target_language: SupportedLanguage,
) -> str:
    normalized = _normalize_resume_copy(text or "")
    if not normalized:
        return ""
    localized = normalized
    if target_language is SupportedLanguage.PORTUGUESE:
        localized = re.sub(r"\bSelf[\s\-]?Employed\b", "Autônomo", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bPresent\b", "Presente", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bCurrent\b", "Atual", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bBrazil\b", "Brasil", localized, flags=re.IGNORECASE)
    elif target_language is SupportedLanguage.ENGLISH:
        localized = re.sub(r"\bAutônomo\b", "Self-Employed", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bPresente\b", "Present", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bAtual\b", "Current", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bBrasil\b", "Brazil", localized, flags=re.IGNORECASE)
    return _normalize_resume_copy(localized)


def _localize_resume_phrase_overrides(
    text: str | None,
    target_language: SupportedLanguage,
) -> str:
    normalized = _normalize_resume_copy(text or "")
    if not normalized:
        return ""
    localized = normalized
    if target_language is SupportedLanguage.PORTUGUESE:
        replacements = (
            (r"\bMicroservices\b", "Microserviços"),
            (r"\bSystem Integrations\b", "Integrações de Sistemas"),
            (r"\bBackend Architecture\b", "Arquitetura de Backend"),
            (r"\bDatabase Modeling\b", "Modelagem de Banco de Dados"),
            (r"\bInternal Tools\b", "Ferramentas Internas"),
            (r"\bMachine Learning\b", "Aprendizado de Máquina"),
            (r"\bCryptography\b", "Criptografia"),
            (r"\bScalable Systems\b", "Sistemas Escaláveis"),
        )
    else:
        replacements = (
            (r"\bMicroserviços\b", "Microservices"),
            (r"\bIntegrações de Sistemas\b", "System Integrations"),
            (r"\bArquitetura de Backend\b", "Backend Architecture"),
            (r"\bModelagem de Banco de Dados\b", "Database Modeling"),
            (r"\bFerramentas Internas\b", "Internal Tools"),
            (r"\bAprendizado de Máquina\b", "Machine Learning"),
            (r"\bCriptografia\b", "Cryptography"),
            (r"\bSistemas Escaláveis\b", "Scalable Systems"),
        )
    for pattern, replacement in replacements:
        localized = re.sub(pattern, replacement, localized, flags=re.IGNORECASE)
    return _normalize_resume_copy(localized)


def _is_resume_bullet(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(("◦", "-", "*"))


def _strip_resume_bullet(value: str) -> str:
    stripped = value.strip()
    return re.sub(r"^[◦*-]\s*", "", stripped).strip()


def _looks_like_next_experience_entry(items: list[str], index: int) -> bool:
    if _parse_resume_heading_line(items[index]) is not None:
        return True
    if index + 2 >= len(items):
        return False
    return (
        not _is_resume_bullet(items[index])
        and not _is_resume_bullet(items[index + 1])
        and _looks_like_date_range(items[index + 2])
    )


def _split_layout_columns(value: str) -> tuple[str, ...]:
    columns = [part.strip() for part in re.split(r"\s{2,}", value.strip()) if part.strip()]
    return tuple(columns)


def _parse_resume_heading_line(value: str) -> tuple[str, str | None, str | None] | None:
    columns = _split_layout_columns(value)
    if len(columns) >= 3 and _looks_like_date_range(columns[-1]):
        title = " ".join(columns[:-2]).strip()
        company_name = columns[-2].strip()
        date_range = columns[-1].strip()
        if title:
            return title, company_name or None, date_range or None
    return None


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_comparison_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _required_resume_identity_tokens(snapshot: ResumeSourceSnapshot) -> tuple[str, ...]:
    tokens: list[str] = []
    for entry in snapshot.experience_entries:
        if entry.company_name:
            token = _resume_identity_company_token(entry.company_name)
            if token:
                tokens.append(token)
        if entry.date_range:
            token = _resume_identity_date_token(entry.date_range)
            if token:
                tokens.append(token)
    for education in snapshot.education_entries:
        if education.institution:
            token = _normalize_comparison_text(education.institution)
            if token:
                tokens.append(token)
        if education.date_range:
            token = _resume_identity_date_token(education.date_range)
            if token:
                tokens.append(token)
    return tuple(dict.fromkeys(tokens))


def _resume_identity_company_token(raw_value: str) -> str:
    token = _normalize_comparison_text(raw_value)
    if token in {"self employed", "autonomo"}:
        return ""
    return token


def _resume_identity_date_token(raw_value: str) -> str:
    matches = re.findall(r"\d{2}/\d{4}", raw_value)
    if matches:
        token = _normalize_comparison_text(" ".join(matches))
        if token:
            return token
    token = _normalize_comparison_text(raw_value)
    if token:
        return token
    return ""


def _format_target_keyword_sentence(posting: JobPosting) -> str:
    keywords = _extract_posting_keywords(posting)
    if not keywords:
        return f"the requirements described for {posting.title}"
    return ", ".join(keywords[:8])


def _default_resume_css() -> str:
    return DEFAULT_OH_MY_CV_RESUME_CSS


def _looks_like_legacy_resume_css(css_text: str) -> bool:
    normalized = css_text.strip()
    if not normalized:
        return True
    legacy_markers = (
        "Backbone CSS for Resume Template 1",
        ".resume-header-item:not(.no-separator)::after",
        '[data-scope="vue-smart-pages"][data-part="page"]',
    )
    return all(marker in normalized for marker in legacy_markers)


def _display_label_for_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    hostname = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    if "linkedin.com" in hostname and path_parts:
        return path_parts[-1]
    if "github.com" in hostname and path_parts:
        return path_parts[-1]
    if hostname:
        return hostname.removeprefix("www.")
    return raw_url


def _screening_capability_terms(settings: UserAgentSettings) -> tuple[str, ...]:
    profile = build_candidate_capability_profile(settings)
    return tuple(
        item.capability
        for item in sorted(
            profile.capabilities.values(),
            key=lambda item: (item.recommended_years, item.confidence, item.capability),
            reverse=True,
        )
        if item.source
        in {"profile_years", "user_reviewed_override", "user_reviewed_resume_inference"}
    )


def _screening_capability_years(settings: UserAgentSettings) -> list[tuple[str, int]]:
    profile = build_candidate_capability_profile(settings)
    return [
        (item.capability, item.recommended_years)
        for item in sorted(
            profile.capabilities.values(),
            key=lambda item: (item.recommended_years, item.confidence, item.capability),
            reverse=True,
        )
        if item.source
        in {"profile_years", "user_reviewed_override", "user_reviewed_resume_inference"}
    ]


def _build_resume_html_document(*, markdown_text: str, css_text: str | None) -> str:
    metadata, body_markdown = _parse_front_matter(markdown_text)
    name = _safe_html(metadata.get("name"))
    header_items = _normalize_header_items(metadata.get("header"))
    header_html = ""
    if name or header_items:
        pieces = ['<header class="resume-header">']
        if name:
            pieces.append(f"<h1>{name}</h1>")
        if header_items:
            grouped_rows = _group_header_rows(header_items)
            for row_index, row in enumerate(grouped_rows):
                row_class = (
                    "resume-header-row resume-header-row-primary"
                    if row_index == 0
                    else "resume-header-row resume-header-row-secondary"
                )
                pieces.append(f'<p class="{row_class}">')
                for index, item in enumerate(row):
                    classes = ["resume-header-item"]
                    if index == len(row) - 1:
                        classes.append("no-separator")
                    label = _format_inline_markdown(item.text)
                    if item.link:
                        pieces.append(
                            '<span class="'
                            f'{" ".join(classes)}">'
                            f'<a href="{_safe_attr(item.link)}">{label}</a></span>',
                        )
                    else:
                        pieces.append(f'<span class="{" ".join(classes)}">{label}</span>')
                pieces.append("</p>")
        pieces.append("</header>")
        header_html = "".join(pieces)

    body_html = _render_resume_body_html(body_markdown)
    base_css = """
@page { size: A4; margin: 0; }
body {
  margin: 0;
  background: #fff;
  color: #171717;
}
#resume-preview [data-scope="vue-smart-pages"][data-part="page"] {
  box-sizing: border-box;
  background: #fff;
  color: #171717;
  min-height: 100vh;
  padding: 15mm 13mm 14mm;
  line-height: 1.38;
}
#resume-preview p {
  margin: 0 0 7px 0;
}
#resume-preview h1 {
  margin: 0 0 6px 0;
  font-size: 31px;
}
#resume-preview h2 {
  margin: 14px 0 6px 0;
  font-size: 18px;
}
#resume-preview h3 {
  margin: 11px 0 5px 0;
  font-size: 15px;
}
#resume-preview ul,
#resume-preview ol {
  margin: 4px 0 7px 20px;
}
#resume-preview li {
  margin: 0;
}
#resume-preview .resume-section { break-inside: avoid; }
#resume-preview .resume-entry { margin: 0 0 8px 0; break-inside: avoid; }
#resume-preview .resume-entry-title { margin-bottom: 2px; }
#resume-preview .resume-entry-meta,
#resume-preview .resume-entry-detail { margin-left: 0.95em; }
#resume-preview dl.resume-entry-definition {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: baseline;
  column-gap: 10px;
  row-gap: 2px;
  margin: 0;
}
#resume-preview .resume-entry-definition dt,
#resume-preview .resume-entry-definition dd {
  margin: 0;
}
#resume-preview .resume-entry-definition dd:last-child { text-align: right; }
#resume-preview .resume-entry-definition-secondary {
  margin-top: 1px;
  grid-template-columns: minmax(0, 1fr) auto;
}
#resume-preview .resume-entry-definition-secondary dt { font-weight: 400; }
#resume-preview .resume-entry ul { margin-top: 3px; margin-bottom: 7px; }
#resume-preview .resume-entry li + li { margin-top: 2px; }
#resume-preview .resume-skill-line { margin-bottom: 2px; }
#resume-preview .resume-skill-line:last-child { margin-bottom: 0; }
"""
    user_css = css_text or ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{base_css}\n{user_css}</style></head><body>"
        "<div id='resume-preview'>"
        "<section data-scope='vue-smart-pages' data-part='page'>"
        f"{header_html}{body_html}"
        "</section></div></body></html>"
    )


def _parse_front_matter(markdown_text: str) -> tuple[dict[str, object], str]:
    if not markdown_text.startswith("---"):
        return {}, markdown_text
    lines = markdown_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, markdown_text
    closing_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        return {}, markdown_text
    yaml_block = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1 :]).lstrip("\n")
    try:
        parsed = yaml.safe_load(yaml_block)
    except Exception:  # noqa: BLE001
        parsed = None
    if not isinstance(parsed, dict):
        return {}, body
    normalized: dict[str, object] = {}
    for key, value in parsed.items():
        if isinstance(key, str):
            normalized[key] = value
    return normalized, body


def _normalize_header_items(raw_header: object) -> tuple[ResumeHeaderItem, ...]:
    if not isinstance(raw_header, list):
        return ()
    items: list[ResumeHeaderItem] = []
    for item in raw_header:
        if not isinstance(item, dict):
            continue
        text = _safe_html(item.get("text"))
        link_value = item.get("link")
        link = str(link_value).strip() if link_value is not None else None
        new_line = bool(item.get("newLine"))
        if not text:
            continue
        items.append(ResumeHeaderItem(text=text, link=link, new_line=new_line))
    return tuple(items)


def _group_header_rows(
    items: tuple[ResumeHeaderItem, ...],
) -> tuple[tuple[ResumeHeaderItem, ...], ...]:
    rows: list[tuple[ResumeHeaderItem, ...]] = []
    current: list[ResumeHeaderItem] = []
    for item in items:
        if item.new_line and current:
            rows.append(tuple(current))
            current = [item]
            continue
        current.append(item)
    if current:
        rows.append(tuple(current))
    return tuple(rows)


def _render_resume_body_html(markdown_body: str) -> str:
    sections = _split_markdown_body_sections(markdown_body)
    html_parts: list[str] = []
    for heading, section_lines in sections:
        html_parts.append('<section class="resume-section">')
        html_parts.append(f"<h2>{_format_inline_markdown(heading)}</h2>")
        html_parts.append(_render_resume_section_content(heading, section_lines))
        html_parts.append("</section>")
    return "".join(html_parts)


def _split_markdown_body_sections(markdown_body: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    sections: list[tuple[str, tuple[str, ...]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    for raw_line in markdown_body.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            if current_heading is not None:
                sections.append((current_heading, tuple(current_lines)))
            current_heading = stripped[3:].strip()
            current_lines = []
            continue
        if current_heading is not None:
            current_lines.append(raw_line.rstrip())
    if current_heading is not None:
        sections.append((current_heading, tuple(current_lines)))
    return tuple(sections)


def _render_resume_section_content(heading: str, lines: tuple[str, ...]) -> str:
    normalized_heading = heading.strip().lower()
    if normalized_heading == "skills":
        return "".join(
            f'<p class="resume-skill-line">{_format_inline_markdown(line.strip())}</p>'
            for line in lines
            if line.strip()
        )

    blocks = _split_markdown_blocks(lines)
    html_parts: list[str] = []
    for block in blocks:
        entry = _parse_resume_entry_block(block)
        if entry is not None:
            html_parts.append(_render_resume_entry_block(entry, section=normalized_heading))
            continue
        html_parts.append(_render_generic_markdown_block(block))
    return "".join(html_parts)


def _split_markdown_blocks(lines: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    blocks: list[tuple[str, ...]] = []
    current: list[str] = []
    for raw_line in lines:
        if not raw_line.strip():
            if current:
                blocks.append(tuple(current))
                current = []
            continue
        current.append(raw_line.rstrip())
    if current:
        blocks.append(tuple(current))
    return tuple(blocks)


def _count_resume_entry_blocks(lines: tuple[str, ...]) -> int:
    return sum(
        1 for block in _split_markdown_blocks(lines) if _parse_resume_entry_block(block) is not None
    )


def _parse_resume_entry_block(lines: tuple[str, ...]) -> ResumeEntryBlock | None:
    if not lines:
        return None
    title_match = re.fullmatch(r"\*\*(.+?)\*\*", lines[0].strip())
    if title_match is None:
        return None
    title = title_match.group(1).strip()
    meta_lines: list[str] = []
    paragraphs: list[str] = []
    bullets: list[str] = []
    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("~ "):
            meta_lines.append(stripped[2:].strip())
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            bullets.append(stripped[2:].strip())
            continue
        paragraphs.append(stripped)
    return ResumeEntryBlock(
        title=title,
        meta_lines=tuple(meta_lines),
        paragraphs=tuple(paragraphs),
        bullets=tuple(bullets),
    )


def _render_resume_entry_block(entry: ResumeEntryBlock, *, section: str) -> str:
    html_parts = ['<article class="resume-entry">']
    if section in {"experience", "certifications"} and entry.meta_lines:
        html_parts.append(_render_resume_entry_definition(entry))
    elif section == "education":
        html_parts.append(_render_resume_education_block(entry))
    else:
        html_parts.append(
            (
                '<p class="resume-entry-title"><strong>'
                f"{_format_inline_markdown(entry.title)}</strong></p>"
            ),
        )
        for meta_line in entry.meta_lines:
            html_parts.append(
                f'<p class="resume-entry-meta">~ {_format_inline_markdown(meta_line)}</p>',
            )
        for paragraph in entry.paragraphs:
            html_parts.append(
                f'<p class="resume-entry-detail">{_format_inline_markdown(paragraph)}</p>',
            )
    if entry.bullets:
        html_parts.append("<ul>")
        for bullet in entry.bullets:
            html_parts.append(f"<li>{_format_inline_markdown(bullet)}</li>")
        html_parts.append("</ul>")
    html_parts.append("</article>")
    return "".join(html_parts)


def _render_resume_entry_definition(entry: ResumeEntryBlock) -> str:
    html_parts = ['<dl class="resume-entry-definition">']
    html_parts.append(
        f"<dt><strong>{_format_inline_markdown(entry.title)}</strong></dt>",
    )
    for meta_line in entry.meta_lines:
        html_parts.append(f"<dd>{_format_inline_markdown(meta_line)}</dd>")
    html_parts.append("</dl>")
    for paragraph in entry.paragraphs:
        html_parts.append(
            f'<p class="resume-entry-detail">{_format_inline_markdown(paragraph)}</p>',
        )
    return "".join(html_parts)


def _render_resume_education_block(entry: ResumeEntryBlock) -> str:
    html_parts = ['<div class="resume-entry-education">']
    meta_lines = list(entry.meta_lines)
    location = meta_lines[0] if meta_lines else None
    date_line = meta_lines[1] if len(meta_lines) > 1 else None
    html_parts.append('<dl class="resume-entry-definition">')
    html_parts.append(
        f"<dt><strong>{_format_inline_markdown(entry.title)}</strong></dt>",
    )
    if location:
        html_parts.append(f"<dd>{_format_inline_markdown(location)}</dd>")
    html_parts.append("</dl>")
    for paragraph in entry.paragraphs:
        if date_line:
            html_parts.append(
                '<dl class="resume-entry-definition resume-entry-definition-secondary">'
                f"<dt>{_format_inline_markdown(paragraph)}</dt>"
                f"<dd>{_format_inline_markdown(date_line)}</dd>"
                "</dl>",
            )
            date_line = None
        else:
            html_parts.append(
                f'<p class="resume-entry-detail">{_format_inline_markdown(paragraph)}</p>',
            )
    if date_line:
        html_parts.append(
            f'<p class="resume-entry-detail">{_format_inline_markdown(date_line)}</p>',
        )
    html_parts.append("</div>")
    return "".join(html_parts)


def _render_generic_markdown_block(lines: tuple[str, ...]) -> str:
    if all(line.strip().startswith(("- ", "* ")) for line in lines):
        items = [line.strip()[2:].strip() for line in lines]
        return (
            "<ul>"
            + "".join(f"<li>{_format_inline_markdown(item)}</li>" for item in items)
            + "</ul>"
        )
    joined = " ".join(line.strip() for line in lines if line.strip()).strip()
    if not joined:
        return ""
    return f"<p>{_format_inline_markdown(joined)}</p>"


def _format_inline_markdown(text: str) -> str:
    escaped = _replace_iconify_spans(_safe_html(text))
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: f'<a href="{_safe_attr(match.group(2))}">{_safe_html(match.group(1))}</a>',
        escaped,
    )
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def _replace_iconify_spans(value: str) -> str:
    icon_map = {
        "tabler:map-pin": "&#128205;",
        "tabler:phone": "&#9742;",
        "tabler:mail": "&#9993;",
        "tabler:brand-linkedin": "in",
        "tabler:brand-github": "GH",
        "tabler:world": "&#127760;",
    }
    pattern = re.compile(
        r'<span[^>]*class=["\'][^"\']*iconify[^"\']*["\'][^>]*data-icon=["\']([^"\']+)["\'][^>]*></span>',
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: icon_map.get(match.group(1).lower(), ""), value)


def _safe_html(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _safe_attr(value: object) -> str:
    return html.escape(str(value), quote=True)


def _extract_pdf_text(path: Path) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        pdf_reader_cls = PdfReader
    except Exception:  # noqa: BLE001
        pdf_reader_cls = None
    if pdf_reader_cls is not None:
        try:
            pdf_reader = pdf_reader_cls(str(path))
        except Exception:  # noqa: BLE001
            pdf_reader = None
        if pdf_reader is not None:
            pieces: list[str] = []
            for page in pdf_reader.pages:
                try:
                    text = page.extract_text() or ""
                except Exception:  # noqa: BLE001
                    text = ""
                if text.strip():
                    pieces.append(text.strip())
            joined = "\n\n".join(pieces).strip()
            if joined:
                return joined

    for command in (
        ["pdftotext", "-layout", str(path), "-"],
        ["pdftotext", str(path), "-"],
    ):
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception:  # noqa: BLE001
            continue
        output = result.stdout.strip()
        if output:
            return output
    return None


def _extract_docx_text(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            raw_xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return None
    normalized = raw_xml.replace("</w:p>", "\n")
    without_tags = re.sub(r"<[^>]+>", "", normalized)
    text = html.unescape(without_tags).strip()
    return text or None


def _sanitize_filename(raw_name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw_name).strip()
    return sanitized or "resume.pdf"


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:60]


def _existing_path(raw_path: str | None) -> Path | None:
    if raw_path is None:
        return None
    path = Path(raw_path)
    if not path.exists() or not path.is_file():
        return None
    return path


def _escape_yaml_scalar(value: str) -> str:
    sanitized = value.replace('"', "'")
    return sanitized.strip()
