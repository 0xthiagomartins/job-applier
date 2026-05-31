#!/usr/bin/env python3
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
from job_applier.domain.enums import SupportedLanguage
from job_applier.infrastructure.candidate_capabilities import (
    build_candidate_capability_profile,
    extract_capabilities_from_text,
)
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


def _load_base_settings(panel_dir: Path):
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


def _read_markdown_sections(markdown_path: Path) -> dict[str, tuple[str, ...]]:
    raw_markdown = markdown_path.read_text(encoding="utf-8")
    _metadata, body_markdown = _parse_front_matter(raw_markdown)
    sections: dict[str, tuple[str, ...]] = {}
    for heading, lines in _split_markdown_body_sections(body_markdown):
        sections[heading.strip()] = tuple(line.strip() for line in lines if line.strip())
    return sections


def _infer_expected_resume_language(sections: dict[str, tuple[str, ...]]) -> SupportedLanguage:
    portuguese_score = 0
    english_score = 0
    for heading in sections:
        normalized = heading.strip().lower()
        if normalized in _PORTUGUESE_SECTION_HEADINGS:
            portuguese_score += 1
        if normalized in _ENGLISH_SECTION_HEADINGS:
            english_score += 1
    if portuguese_score > english_score:
        return SupportedLanguage.PORTUGUESE
    return SupportedLanguage.ENGLISH


def _extract_pdf_page_metrics(pdf_path: Path | None) -> tuple[int | None, tuple[int, ...]]:
    if pdf_path is None or not pdf_path.exists():
        return None, ()
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None, ()
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:  # noqa: BLE001
        return None, ()
    counts: list[int] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        counts.append(len(text.strip()))
    return len(reader.pages), tuple(counts)


def _extract_pdf_page_metrics_via_cli(pdf_path: Path | None) -> tuple[int | None, tuple[int, ...]]:
    if pdf_path is None or not pdf_path.exists():
        return None, ()
    if shutil.which("pdfinfo") is None or shutil.which("pdftotext") is None:
        return None, ()
    try:
        pdfinfo_output = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return None, ()
    page_count: int | None = None
    for line in pdfinfo_output.splitlines():
        if not line.startswith("Pages:"):
            continue
        raw_value = line.partition(":")[2].strip()
        if raw_value.isdigit():
            page_count = int(raw_value)
        break
    if page_count is None:
        return None, ()
    counts: list[int] = []
    for page_index in range(1, page_count + 1):
        try:
            page_text = subprocess.run(
                ["pdftotext", "-f", str(page_index), "-l", str(page_index), str(pdf_path), "-"],
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            ).stdout
        except OSError, subprocess.SubprocessError:
            return page_count, tuple(counts)
        counts.append(len(page_text.strip()))
    return page_count, tuple(counts)


def _audit_resume(
    *,
    markdown_path: Path,
    pdf_path: Path | None,
    base_cv_path: Path | None,
    job_title: str | None,
    panel_dir: Path,
) -> AuditResult:
    settings = _load_base_settings(panel_dir)
    if base_cv_path is not None:
        settings = settings.model_copy(
            update={
                "profile": settings.profile.model_copy(update={"cv_path": str(base_cv_path)}),
            }
        )
    capability_profile = build_candidate_capability_profile(settings)
    anchored_capabilities = set(capability_profile.capabilities)
    headline, summary = _read_markdown(markdown_path)
    sections = _read_markdown_sections(markdown_path)
    expected_language = _infer_expected_resume_language(sections)
    page_count, page_char_counts = _extract_pdf_page_metrics(pdf_path)
    if page_count is None:
        page_count, page_char_counts = _extract_pdf_page_metrics_via_cli(pdf_path)
    findings: list[AuditFinding] = []

    if len(headline) > 72:
        findings.append(
            AuditFinding(
                "warning",
                "headline_too_long",
                f"Headline is {len(headline)} characters.",
            ),
        )
    headline_tokens = set(re.findall(r"[a-z]+", headline.lower()))
    if headline_tokens and headline_tokens.issubset(_GENERIC_HEADLINE_TOKENS):
        findings.append(
            AuditFinding(
                "warning",
                "headline_too_generic",
                "Headline reads as a generic role without stack or domain emphasis.",
            )
        )
    summary_word_count = len(summary.split())
    if summary_word_count > 60:
        findings.append(
            AuditFinding(
                "warning",
                "summary_too_long",
                f"Summary has {summary_word_count} words and may be too dense for the top fold.",
            )
        )
    normalized_summary = summary.lower()
    for phrase in _GENERIC_SUMMARY_PHRASES:
        if phrase in normalized_summary:
            findings.append(
                AuditFinding(
                    "warning",
                    "generic_summary_language",
                    f'Summary contains generic recruiter language: "{phrase}".',
                )
            )
            break

    generated_capabilities = set(extract_capabilities_from_text(" ".join((headline, summary))))
    unanchored = sorted(
        capability
        for capability in generated_capabilities
        if capability not in anchored_capabilities and capability not in _UNANCHORED_ALLOWED_TOKENS
    )
    if unanchored:
        unanchored_phrase = ", ".join(unanchored)
        findings.append(
            AuditFinding(
                "error",
                "unanchored_terms",
                "Generated top-of-resume content mentions terms not anchored in the base CV: "
                f"{unanchored_phrase}.",
            )
        )

    repeated_focus_terms = [
        capability
        for capability in generated_capabilities
        if normalized_summary.count(capability.lower()) >= 3
    ]
    if repeated_focus_terms:
        repeated_phrase = ", ".join(sorted(repeated_focus_terms))
        findings.append(
            AuditFinding(
                "warning",
                "keyword_stuffing",
                f"Summary repeats focus terms too aggressively: {repeated_phrase}.",
            )
        )

    sampled_language_lines: list[str] = []
    for heading, lines in sections.items():
        normalized_heading = canonical_resume_section_title(heading) or heading.strip().lower()
        if normalized_heading not in {"summary", "experience", "skills"}:
            continue
        sampled_language_lines.extend(lines[:6])
    sampled_language_text = "\n".join(line for line in sampled_language_lines if line.strip())
    if sampled_language_text:
        detection = detect_text_language(
            sampled_language_text,
            default_language=expected_language,
            source=str(markdown_path),
        )
        if detection.language is not expected_language and detection.confidence >= 0.4:
            findings.append(
                AuditFinding(
                    "error",
                    "mixed_language_content",
                    "Resume body language does not match the localized section headings.",
                )
            )

    if expected_language is SupportedLanguage.PORTUGUESE:
        lower_markdown = markdown_path.read_text(encoding="utf-8").lower()
        leaked_labels = [
            label for label in _PORTUGUESE_ENGLISH_LABEL_LEAKS if label in lower_markdown
        ]
        if leaked_labels:
            findings.append(
                AuditFinding(
                    "error",
                    "english_labels_in_portuguese_resume",
                    "Portuguese resume still contains English section/category labels: "
                    + ", ".join(leaked_labels),
                )
            )

    if job_title:
        title_capabilities = set(extract_capabilities_from_text(job_title))
        if title_capabilities and not (generated_capabilities & title_capabilities):
            findings.append(
                AuditFinding(
                    "warning",
                    "weak_title_alignment",
                    "Top-of-resume content does not reflect explicit stack cues "
                    "from the target job title.",
                )
            )

    if page_count is not None and page_count > 2:
        findings.append(
            AuditFinding(
                "warning",
                "page_count_high",
                f"Tailored PDF uses {page_count} pages, which may be too long for the MVP target.",
            )
        )
    if page_char_counts and len(page_char_counts) >= 2 and page_char_counts[-1] < 180:
        findings.append(
            AuditFinding(
                "warning",
                "last_page_underutilized",
                "The final PDF page contains very little text and may feel visually underused.",
            )
        )

    if not findings:
        findings.append(
            AuditFinding(
                "info",
                "no_major_findings",
                "No major quality findings were detected by the local auditor.",
            )
        )

    return AuditResult(
        markdown_path=str(markdown_path),
        pdf_path=str(pdf_path) if pdf_path else None,
        base_cv_path=str(base_cv_path) if base_cv_path else settings.profile.cv_path,
        headline=headline,
        summary=summary,
        findings=tuple(findings),
        page_count=page_count,
        page_char_counts=page_char_counts,
    )


def main() -> int:
    args = _parse_args()
    markdown_path, pdf_path, submission_dir = _resolve_paths(args)
    panel_dir = Path(args.panel_dir)
    base_cv_path = Path(args.base_cv) if args.base_cv else None
    if base_cv_path is None and submission_dir is not None:
        candidate_input_dir = submission_dir / "input"
        input_candidates = sorted(candidate_input_dir.glob("*.pdf"))
        if input_candidates:
            base_cv_path = input_candidates[0]

    result = _audit_resume(
        markdown_path=markdown_path,
        pdf_path=pdf_path,
        base_cv_path=base_cv_path,
        job_title=args.job_title,
        panel_dir=panel_dir,
    )

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(f"Markdown: {result.markdown_path}")
        print(f"PDF: {result.pdf_path or 'n/a'}")
        print(f"Base CV: {result.base_cv_path or 'n/a'}")
        print(f"Headline: {result.headline}")
        print(f"Summary: {result.summary}")
        if result.page_count is not None:
            print(
                f"PDF pages: {result.page_count} "
                f"(chars per page: {', '.join(str(count) for count in result.page_char_counts)})"
            )
        print("Findings:")
        for finding in result.findings:
            print(f"- [{finding.severity}] {finding.code}: {finding.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
