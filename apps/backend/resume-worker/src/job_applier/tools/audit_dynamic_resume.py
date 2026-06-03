"""Audit one generated dynamic resume against base-CV evidence and layout heuristics."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from job_applier.application.agent_execution import build_user_agent_settings
from job_applier.application.config import UserAgentSettings
from job_applier.domain.enums import SupportedLanguage
from job_applier.infrastructure.candidate_capabilities import extract_capabilities_from_text
from job_applier.infrastructure.language_support import (
    canonical_resume_section_title,
    detect_text_language,
)
from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore
from job_applier.infrastructure.resume_dynamic import (
    _parse_front_matter,
    _split_markdown_blocks,
    _split_markdown_body_sections,
)

Severity = Literal["info", "warning", "error"]

_GENERIC_SUMMARY_PHRASES = (
    "proven track record",
    "results driven",
    "dynamic professional",
    "highly motivated",
    "passionate about",
    "eager to deliver",
    "well prepared",
    "targeted for",
)
_GENERIC_HEADLINE_TOKENS = frozenset({"engineer", "developer", "specialist"})
_UNANCHORED_ALLOWED_TOKENS = frozenset(
    {
        "software",
        "full stack",
        "backend",
        "automation",
        "engineer",
        "developer",
        "specialist",
        "remote",
        "apis",
    }
)
_PORTUGUESE_SECTION_HEADINGS = frozenset(
    {"resumo", "experiência", "certificações", "educação", "competências"}
)
_ENGLISH_SECTION_HEADINGS = frozenset(
    {"summary", "experience", "certifications", "education", "skills"}
)
_PORTUGUESE_ENGLISH_LABEL_LEAKS = (
    "core languages:",
    "tools & platforms:",
    "full stack & backend:",
    "engineering practices:",
    "applied ai & automation:",
)
_PORTUGUESE_ENGLISH_META_LEAK_PATTERNS = (
    r"\bpresent\b",
    r"\bself-employed\b",
    r"\bbrazil\b",
)
_TECHNICAL_LANGUAGE_ALLOWLIST = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "java",
        "aws",
        "gcp",
        "azure",
        "rest apis",
        "api",
        "apis",
        "devops",
        "rag",
        "rabbitmq",
        "saas",
        "legal-tech",
        "legaltech",
        "microservices",
        "backend",
        "full stack",
        "full-stack",
        "mobile",
        "chatbot",
        "chatbots",
        "rust",
    }
)


@dataclass(frozen=True, slots=True)
class AuditFinding:
    severity: Severity
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class AuditResult:
    markdown_path: str
    pdf_path: str | None
    base_cv_path: str | None
    headline: str
    summary: str
    findings: tuple[AuditFinding, ...]
    page_count: int | None = None
    page_char_counts: tuple[int, ...] = ()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-dir", help="Directory containing dynamic-resume artifacts.")
    parser.add_argument("--markdown", help="Explicit path to the tailored markdown file.")
    parser.add_argument("--pdf", help="Explicit path to the tailored PDF file.")
    parser.add_argument("--base-cv", help="Explicit path to the base CV used for comparison.")
    parser.add_argument("--job-title", help="Optional target job title used for tailoring checks.")
    parser.add_argument(
        "--panel-dir",
        default="artifacts/runtime/panel",
        help="Panel storage directory used to infer the default base CV.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON.")
    return parser.parse_args()


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path | None, Path | None]:
    submission_dir = Path(args.submission_dir) if args.submission_dir else None
    markdown_path = Path(args.markdown) if args.markdown else None
    pdf_path = Path(args.pdf) if args.pdf else None
    if submission_dir is not None:
        dynamic_dir = submission_dir / "dynamic-resume"
        if markdown_path is None:
            matches = sorted(dynamic_dir.glob("*-oh-my-cv.md"))
            if matches:
                markdown_path = matches[0]
        if pdf_path is None:
            matches = sorted(dynamic_dir.glob("*-tailored.pdf"))
            if matches:
                pdf_path = matches[0]
    if markdown_path is None:
        raise SystemExit("Could not locate a markdown artifact to audit.")
    return markdown_path, pdf_path, submission_dir


def _load_base_settings(panel_dir: Path) -> UserAgentSettings:
    store = LocalPanelSettingsStore(root_dir=panel_dir)
    return build_user_agent_settings(store.load())


def _strip_inline_html(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", without_tags).strip()


def _extract_header_headline(metadata: dict[str, object]) -> str:
    header = metadata.get("header")
    if not isinstance(header, list):
        return ""
    for item in header:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        normalized = _strip_inline_html(text)
        lower_normalized = normalized.lower()
        if "|" in normalized or any(
            token in lower_normalized for token in _GENERIC_HEADLINE_TOKENS
        ):
            return normalized
    return ""


def _read_markdown(markdown_path: Path) -> tuple[str, str]:
    raw_markdown = markdown_path.read_text(encoding="utf-8")
    metadata, body_markdown = _parse_front_matter(raw_markdown)
    headline = ""
    summary = ""
    header_blocks = _split_markdown_blocks(tuple(body_markdown.splitlines()))
    current_heading = ""
    collected_summary: list[str] = []
    for block in header_blocks:
        if not block:
            continue
        first = block[0].strip()
        if first.startswith("# "):
            headline = first.removeprefix("# ").strip()
            continue
        if first.startswith("## "):
            current_heading = (
                canonical_resume_section_title(first.removeprefix("## ").strip())
                or first.removeprefix("## ").strip().lower()
            )
            continue
        if current_heading == "summary":
            collected_summary.append(" ".join(line.strip() for line in block if line.strip()))
            if collected_summary:
                break
    if not headline:
        headline = (
            _extract_header_headline(metadata)
            or str(metadata.get("role") or metadata.get("headline") or "").strip()
        )
    summary = " ".join(part for part in collected_summary if part).strip()
    return headline, summary


def _read_pdf_text(pdf_path: Path | None) -> tuple[str, int | None, tuple[int, ...]]:
    if pdf_path is None or shutil.which("pdftotext") is None:
        return "", None, ()
    try:
        completed = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError, FileNotFoundError:
        return "", None, ()
    text = completed.stdout
    pages = tuple(page.strip() for page in text.split("\f"))
    non_empty_pages = tuple(page for page in pages if page)
    page_char_counts = tuple(len(page) for page in non_empty_pages)
    page_count = len(non_empty_pages) if non_empty_pages else None
    return text, page_count, page_char_counts


def _extract_markdown_resume_body(markdown_path: Path) -> str:
    raw_markdown = markdown_path.read_text(encoding="utf-8")
    _, body_markdown = _parse_front_matter(raw_markdown)
    sections = _split_markdown_body_sections(body_markdown)
    collected = []
    for _, lines in sections:
        collected.append("\n".join(lines))
    return "\n".join(collected)


def _normalize_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9+\-#/ ]+", " ", text.lower())
    tokens = {token for token in cleaned.split() if len(token) >= 3}
    return tokens


def _strip_technical_allowlist(text: str) -> str:
    lowered = text.lower()
    for token in sorted(_TECHNICAL_LANGUAGE_ALLOWLIST, key=len, reverse=True):
        lowered = lowered.replace(token, " ")
    return lowered


def _collect_findings(
    *,
    headline: str,
    summary: str,
    markdown_body: str,
    pdf_text: str,
    base_cv_text: str,
    target_job_title: str | None,
) -> tuple[AuditFinding, ...]:
    findings: list[AuditFinding] = []
    combined_resume_text = "\n".join(part for part in (markdown_body, pdf_text) if part).strip()
    if not combined_resume_text:
        findings.append(
            AuditFinding(
                severity="error",
                code="empty_resume_output",
                message="The generated resume appears to be empty.",
            )
        )
        return tuple(findings)

    lowered_summary = summary.lower()
    if summary and any(phrase in lowered_summary for phrase in _GENERIC_SUMMARY_PHRASES):
        findings.append(
            AuditFinding(
                severity="warning",
                code="generic_summary_phrase",
                message="Summary contains generic marketing language.",
            )
        )

    if headline:
        lowered_headline = headline.lower()
        if not any(token in lowered_headline for token in _GENERIC_HEADLINE_TOKENS):
            findings.append(
                AuditFinding(
                    severity="warning",
                    code="headline_missing_role_shape",
                    message="Headline does not resemble a role-focused heading.",
                )
            )

    base_tokens = _normalize_tokens(base_cv_text)
    resume_tokens = _normalize_tokens(combined_resume_text)
    extra_resume_tokens = {
        token
        for token in resume_tokens - base_tokens
        if token not in _UNANCHORED_ALLOWED_TOKENS and len(token) >= 5
    }
    if len(extra_resume_tokens) >= 25:
        findings.append(
            AuditFinding(
                severity="warning",
                code="high_unanchored_term_count",
                message="Resume introduces many terms not evidenced in the base CV.",
            )
        )

    if target_job_title:
        normalized_job_title_tokens = _normalize_tokens(target_job_title)
        unsupported_title_tokens = {
            token
            for token in normalized_job_title_tokens
            if token not in base_tokens
            and token not in _UNANCHORED_ALLOWED_TOKENS
            and len(token) >= 4
        }
        if unsupported_title_tokens and unsupported_title_tokens.issubset(extra_resume_tokens):
            findings.append(
                AuditFinding(
                    severity="warning",
                    code="title_alignment_not_grounded",
                    message=(
                        "Target-title stack cues were added without clear evidence in the base CV."
                    ),
                )
            )

    detected_language = detect_text_language(combined_resume_text)
    if detected_language.language == SupportedLanguage.PORTUGUESE:
        lowered_resume = combined_resume_text.lower()
        leaked_labels = [
            label for label in _PORTUGUESE_ENGLISH_LABEL_LEAKS if label in lowered_resume
        ]
        if leaked_labels:
            findings.append(
                AuditFinding(
                    severity="error",
                    code="english_labels_in_portuguese_resume",
                    message=(
                        "Portuguese resume still contains English labels: "
                        f"{', '.join(leaked_labels)}."
                    ),
                )
            )
        leaked_meta = [
            pattern
            for pattern in _PORTUGUESE_ENGLISH_META_LEAK_PATTERNS
            if re.search(pattern, lowered_resume)
        ]
        if leaked_meta:
            findings.append(
                AuditFinding(
                    severity="error",
                    code="english_meta_in_portuguese_resume",
                    message="Portuguese resume still contains English metadata markers.",
                )
            )
        english_headings = [
            heading for heading in _ENGLISH_SECTION_HEADINGS if f"## {heading}" in lowered_resume
        ]
        if english_headings:
            findings.append(
                AuditFinding(
                    severity="warning",
                    code="english_heading_in_portuguese_resume",
                    message=(
                        "Portuguese resume contains English section headings: "
                        f"{', '.join(english_headings)}."
                    ),
                )
            )
    elif detected_language.language == SupportedLanguage.ENGLISH:
        lowered_resume = combined_resume_text.lower()
        portuguese_headings = [
            heading for heading in _PORTUGUESE_SECTION_HEADINGS if f"## {heading}" in lowered_resume
        ]
        if portuguese_headings:
            findings.append(
                AuditFinding(
                    severity="warning",
                    code="portuguese_heading_in_english_resume",
                    message=(
                        "English resume contains Portuguese headings: "
                        f"{', '.join(portuguese_headings)}."
                    ),
                )
            )

    stripped_language_probe = _strip_technical_allowlist(combined_resume_text)
    if (
        detect_text_language(stripped_language_probe).language == SupportedLanguage.PORTUGUESE
        and summary
    ):
        lowered_summary_probe = _strip_technical_allowlist(summary)
        if detect_text_language(lowered_summary_probe).language == SupportedLanguage.ENGLISH:
            findings.append(
                AuditFinding(
                    severity="warning",
                    code="english_summary_in_portuguese_resume",
                    message="Summary language appears misaligned with the rest of the resume.",
                )
            )

    return tuple(findings)


def _load_base_cv_text(base_cv_path: Path | None) -> str:
    if base_cv_path is None:
        return ""
    suffix = base_cv_path.suffix.lower()
    if suffix == ".md":
        return base_cv_path.read_text(encoding="utf-8")
    if suffix == ".pdf" and shutil.which("pdftotext") is not None:
        try:
            completed = subprocess.run(
                ["pdftotext", str(base_cv_path), "-"],
                check=True,
                capture_output=True,
                text=True,
            )
            return completed.stdout
        except subprocess.CalledProcessError, FileNotFoundError:
            return ""
    try:
        return base_cv_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def _resolve_base_cv_path(args: argparse.Namespace, submission_dir: Path | None) -> Path | None:
    if args.base_cv:
        return Path(args.base_cv)
    if submission_dir is not None:
        input_dir = submission_dir / "input"
        pdf_candidates = sorted(input_dir.glob("*.pdf"))
        if pdf_candidates:
            return pdf_candidates[0]
    settings = _load_base_settings(Path(args.panel_dir))
    cv_path = settings.profile.cv_path
    return Path(cv_path) if cv_path else None


def audit_resume(args: argparse.Namespace) -> AuditResult:
    markdown_path, pdf_path, submission_dir = _resolve_paths(args)
    headline, summary = _read_markdown(markdown_path)
    markdown_body = _extract_markdown_resume_body(markdown_path)
    pdf_text, page_count, page_char_counts = _read_pdf_text(pdf_path)
    base_cv_path = _resolve_base_cv_path(args, submission_dir)
    base_cv_text = _load_base_cv_text(base_cv_path)

    if not base_cv_text:
        base_cv_text = markdown_body

    grounded_capabilities = extract_capabilities_from_text(base_cv_text)
    target_job_title = args.job_title or None
    findings = list(
        _collect_findings(
            headline=headline,
            summary=summary,
            markdown_body=markdown_body,
            pdf_text=pdf_text,
            base_cv_text=base_cv_text,
            target_job_title=target_job_title,
        )
    )

    if page_count is not None and page_count > 2:
        findings.append(
            AuditFinding(
                severity="warning",
                code="resume_exceeds_two_pages",
                message=f"Rendered resume uses {page_count} pages.",
            )
        )

    if page_char_counts and len(page_char_counts) >= 2:
        first_page = page_char_counts[0]
        last_page = page_char_counts[-1]
        if first_page >= 1800 and last_page <= 300:
            findings.append(
                AuditFinding(
                    severity="warning",
                    code="underused_last_page",
                    message="Rendered resume underuses the final page.",
                )
            )

    if grounded_capabilities and not findings:
        findings.append(
            AuditFinding(
                severity="info",
                code="no_major_findings",
                message="Audit did not find major resume-quality issues.",
            )
        )

    return AuditResult(
        markdown_path=str(markdown_path),
        pdf_path=str(pdf_path) if pdf_path else None,
        base_cv_path=str(base_cv_path) if base_cv_path else None,
        headline=headline,
        summary=summary,
        findings=tuple(findings),
        page_count=page_count,
        page_char_counts=page_char_counts,
    )


def main() -> int:
    args = _parse_args()
    result = audit_resume(args)
    if args.json:
        print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    else:
        print(f"Markdown: {result.markdown_path}")
        if result.pdf_path:
            print(f"PDF: {result.pdf_path}")
        if result.base_cv_path:
            print(f"Base CV: {result.base_cv_path}")
        for finding in result.findings:
            print(f"[{finding.severity}] {finding.code}: {finding.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
