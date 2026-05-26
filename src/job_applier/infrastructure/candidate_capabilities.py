"""Infer candidate capability ranges from the base CV and saved profile data."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from job_applier.application.config import UserAgentSettings

if TYPE_CHECKING:
    from job_applier.infrastructure.resume_dynamic import (
        ResumeExperienceEntry,
        ResumeSourceSnapshot,
    )

_CAPABILITY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", (r"\bpython\b",)),
    ("javascript", (r"\bjavascript\b", r"\bnode\.?js\b")),
    ("typescript", (r"\btypescript\b",)),
    ("java", (r"\bjava\b",)),
    ("aws", (r"\baws\b", r"\bamazon web services\b")),
    ("gcp", (r"\bgcp\b", r"\bgoogle cloud\b")),
    ("azure", (r"\bazure\b",)),
    ("linux", (r"\blinux\b",)),
    ("sql", (r"\bsql\b",)),
    ("react", (r"\breact\b",)),
    ("react native", (r"\breact[\s\-]?native\b",)),
    ("fastapi", (r"\bfastapi\b",)),
    ("uipath", (r"\buipath\b",)),
    ("langchain", (r"\blangchain\b",)),
    ("rag", (r"\brag\b", r"\bretrieval[\s\-]augmented generation\b")),
    (
        "automation",
        (
            r"\bautomation\b",
            r"\bworkflow automation\b",
            r"\bautomated workflows?\b",
            r"\bprocess automation\b",
        ),
    ),
    ("backend", (r"\bbackend\b", r"\bapi(s)?\b", r"\bmicroservices?\b")),
    ("full stack", (r"\bfull[\s\-]?stack\b", r"\bfullstack\b")),
)

_CAPABILITY_FAMILIES: dict[str, str] = {
    "python": "language",
    "javascript": "language",
    "typescript": "language",
    "java": "language",
    "aws": "cloud",
    "gcp": "cloud",
    "azure": "cloud",
    "linux": "platform",
    "sql": "data",
    "react": "frontend",
    "react native": "frontend",
    "fastapi": "backend_framework",
    "uipath": "automation_tool",
    "langchain": "ai_framework",
    "rag": "ai_framework",
    "automation": "automation_domain",
    "backend": "backend_domain",
    "full stack": "full_stack_domain",
}

_FAMILY_NEIGHBORS: dict[str, tuple[str, ...]] = {
    "language": ("backend_framework", "frontend", "automation_domain", "backend_domain", "data"),
    "cloud": ("backend_domain", "automation_domain", "platform"),
    "platform": ("backend_domain", "automation_domain", "cloud"),
    "data": ("backend_domain", "language", "automation_domain"),
    "frontend": ("language", "full_stack_domain"),
    "backend_framework": ("language", "backend_domain", "cloud"),
    "automation_tool": ("automation_domain", "ai_framework", "language"),
    "ai_framework": ("automation_domain", "language", "backend_domain"),
    "automation_domain": ("automation_tool", "ai_framework", "language", "cloud"),
    "backend_domain": ("language", "backend_framework", "cloud", "platform", "data"),
    "full_stack_domain": ("frontend", "backend_domain", "language"),
}

_DATE_RANGE_PATTERN = re.compile(r"(?P<start>\d{2}/\d{4})\s*-\s*(?P<end>Present|\d{2}/\d{4})")


@dataclass(frozen=True, slots=True)
class CapabilityExperienceRange:
    """Plausible experience interval for one candidate capability."""

    capability: str
    min_years: int
    max_years: int
    confidence: float
    source: str
    recommended_years_override: int | None = None
    evidence: tuple[str, ...] = ()
    inferred_from: tuple[str, ...] = ()

    @property
    def recommended_years(self) -> int:
        """Return the screening-optimized but plausible answer."""

        return self.recommended_years_override or self.max_years


@dataclass(frozen=True, slots=True)
class CandidateCapabilityProfile:
    """Structured capability profile inferred from profile data and the base CV."""

    total_career_years: int
    capabilities: dict[str, CapabilityExperienceRange]
    evidence_sources: tuple[str, ...] = ()


def build_candidate_capability_profile(settings: UserAgentSettings) -> CandidateCapabilityProfile:
    """Build a reusable candidate capability profile for autofill and tailoring."""

    snapshot = _load_resume_snapshot(settings.profile.cv_path)
    total_career_years = _estimate_total_career_years(snapshot, settings)
    capability_ranges: dict[str, CapabilityExperienceRange] = {}

    for stack_name, years in settings.profile.years_experience_by_stack.items():
        canonical = canonicalize_capability_name(stack_name)
        if canonical is None or years <= 0:
            continue
        capability_ranges[canonical] = CapabilityExperienceRange(
            capability=canonical,
            min_years=years,
            max_years=years,
            confidence=0.99,
            source="profile_years",
            evidence=(stack_name,),
        )

    months_by_capability = _collect_capability_months_from_resume(snapshot)
    for capability, months in months_by_capability.items():
        if capability in capability_ranges or months <= 0:
            continue
        max_years = _months_to_competitive_years(months, cap_years=total_career_years)
        min_years = max(1, min(max_years, math.floor(months / 12) or 1))
        capability_ranges[capability] = CapabilityExperienceRange(
            capability=capability,
            min_years=min_years,
            max_years=max_years,
            confidence=0.84,
            source="resume_experience",
            evidence=("experience_entries",),
        )

    weak_resume_mentions = _collect_capability_mentions(snapshot)
    for capability in weak_resume_mentions:
        if capability in capability_ranges:
            continue
        max_years = min(total_career_years, 4 if total_career_years >= 4 else total_career_years)
        if max_years <= 0:
            continue
        min_years = max(1, max_years - 2)
        capability_ranges[capability] = CapabilityExperienceRange(
            capability=capability,
            min_years=min_years,
            max_years=max_years,
            confidence=0.56,
            source="resume_mentions",
            evidence=("summary_or_skills",),
        )

    capability_ranges = _apply_reviewed_capability_overrides(
        base_ranges=capability_ranges,
        settings=settings,
    )

    return CandidateCapabilityProfile(
        total_career_years=total_career_years,
        capabilities=capability_ranges,
        evidence_sources=tuple(
            source
            for source in (
                settings.profile.cv_path,
                "years_experience_by_stack",
                "capability_overrides",
            )
            if source
        ),
    )


def find_capability_range_for_text(
    *,
    settings: UserAgentSettings,
    text_fragments: tuple[str, ...],
) -> CapabilityExperienceRange | None:
    """Return the best plausible capability range requested by one field/question."""

    profile = build_candidate_capability_profile(settings)
    requested_capabilities = extract_capabilities_from_text(" ".join(text_fragments))
    if not requested_capabilities:
        return None

    best_match: CapabilityExperienceRange | None = None
    for capability in requested_capabilities:
        candidate = profile.capabilities.get(capability)
        if candidate is None:
            candidate = _infer_related_capability_range(
                capability=capability,
                profile=profile,
            )
        if candidate is None:
            continue
        if best_match is None:
            best_match = candidate
            continue
        best_key = (best_match.confidence, best_match.max_years, best_match.min_years)
        candidate_key = (candidate.confidence, candidate.max_years, candidate.min_years)
        if candidate_key > best_key:
            best_match = candidate
    return best_match


def capability_profile_to_payload(profile: CandidateCapabilityProfile) -> dict[str, object]:
    """Return a compact JSON-friendly payload for prompts and logs."""

    ordered = sorted(
        profile.capabilities.values(),
        key=lambda item: (item.max_years, item.confidence, item.capability),
        reverse=True,
    )
    return {
        "total_career_years": profile.total_career_years,
        "capabilities": [
            {
                "capability": item.capability,
                "min_years": item.min_years,
                "max_years": item.max_years,
                "recommended_years": item.recommended_years,
                "confidence": item.confidence,
                "source": item.source,
                "reviewed": item.source.startswith("user_reviewed"),
                "evidence": list(item.evidence),
                "inferred_from": list(item.inferred_from),
            }
            for item in ordered
        ],
    }


def extract_capabilities_from_text(text: str) -> tuple[str, ...]:
    """Extract canonical capabilities from free-form text in text order."""

    normalized = _normalize_lookup_text(text)
    if not normalized:
        return ()
    positioned_matches: list[tuple[int, int, str]] = []
    for pattern_index, (label, patterns) in enumerate(_CAPABILITY_PATTERNS):
        earliest_position: int | None = None
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match is None:
                continue
            position = match.start()
            if earliest_position is None or position < earliest_position:
                earliest_position = position
        if earliest_position is None:
            continue
        positioned_matches.append((earliest_position, pattern_index, label))
    positioned_matches.sort()
    return tuple(label for _, _, label in positioned_matches)


def canonicalize_capability_name(raw_value: str) -> str | None:
    """Convert a raw skill/stack label into a canonical capability name."""

    capabilities = extract_capabilities_from_text(raw_value)
    if capabilities:
        return capabilities[0]
    normalized = _normalize_lookup_text(raw_value)
    return normalized or None


@lru_cache(maxsize=12)
def _load_resume_snapshot(cv_path: str | None) -> ResumeSourceSnapshot:
    from job_applier.infrastructure.resume_dynamic import (
        ResumeSourceSnapshot,
        _coalesce_wrapped_skill_lines,
        _extract_docx_text,
        _extract_pdf_text,
        _normalize_extracted_resume_text,
        _parse_experience_entries,
        _split_resume_sections,
    )

    if cv_path is None:
        return ResumeSourceSnapshot()
    path = Path(cv_path)
    if not path.exists():
        return ResumeSourceSnapshot()

    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        raw_text = path.read_text(encoding="utf-8", errors="ignore")
    elif suffix == ".docx":
        raw_text = _extract_docx_text(path) or ""
    elif suffix == ".pdf":
        raw_text = _extract_pdf_text(path) or ""
    else:
        raw_text = ""
    normalized_text = _normalize_extracted_resume_text(raw_text)
    if not normalized_text:
        return ResumeSourceSnapshot()

    header_lines, section_map = _split_resume_sections(normalized_text)
    role_line = (
        header_lines[1].strip() if len(header_lines) > 1 and header_lines[1].strip() else None
    )
    summary = " ".join(section_map.get("Summary", ())).strip() or None
    skill_lines = _coalesce_wrapped_skill_lines(section_map.get("Skills", ()))
    return ResumeSourceSnapshot(
        header_role=role_line,
        summary=summary,
        experience_entries=_parse_experience_entries(section_map.get("Experience", ())),
        skill_lines=skill_lines,
        word_count=len(normalized_text.split()),
    )


def _estimate_total_career_years(
    snapshot: ResumeSourceSnapshot,
    settings: UserAgentSettings,
) -> int:
    ranges = [
        parsed
        for entry in snapshot.experience_entries
        if entry.date_range
        for parsed in (_parse_date_range_months(entry.date_range),)
        if parsed is not None
    ]
    if ranges:
        earliest_start = min(start for start, _end in ranges)
        latest_end = max(end for _start, end in ranges)
        total_months = max(
            1,
            (latest_end.year - earliest_start.year) * 12
            + latest_end.month
            - earliest_start.month
            + 1,
        )
        return max(1, math.ceil(total_months / 12))
    explicit_years: list[int] = [
        years for years in settings.profile.years_experience_by_stack.values() if years > 0
    ]
    if explicit_years:
        return max(explicit_years)
    return 0


def _collect_capability_months_from_resume(snapshot: ResumeSourceSnapshot) -> dict[str, int]:
    capability_months: dict[str, int] = {}
    for entry in snapshot.experience_entries:
        duration_months = _experience_entry_duration_months(entry)
        if duration_months is None:
            continue
        haystack = " ".join(filter(None, (entry.title, entry.company_name, *entry.bullets)))
        for capability in extract_capabilities_from_text(haystack):
            capability_months[capability] = capability_months.get(capability, 0) + duration_months
    return capability_months


def _collect_capability_mentions(snapshot: ResumeSourceSnapshot) -> tuple[str, ...]:
    texts = [snapshot.header_role or "", snapshot.summary or "", *snapshot.skill_lines]
    ordered: list[str] = []
    seen: set[str] = set()
    for capability in extract_capabilities_from_text(" ".join(texts)):
        if capability in seen:
            continue
        seen.add(capability)
        ordered.append(capability)
    return tuple(ordered)


def _apply_reviewed_capability_overrides(
    *,
    base_ranges: dict[str, CapabilityExperienceRange],
    settings: UserAgentSettings,
) -> dict[str, CapabilityExperienceRange]:
    merged_ranges = dict(base_ranges)
    exact_capabilities = {
        canonical
        for stack_name in settings.profile.years_experience_by_stack
        for canonical in (canonicalize_capability_name(stack_name),)
        if canonical is not None
    }

    for raw_capability, override in settings.profile.capability_overrides.items():
        capability = canonicalize_capability_name(raw_capability)
        if capability is None:
            continue
        if capability in exact_capabilities:
            continue
        if not override.enabled:
            merged_ranges.pop(capability, None)
            continue

        min_years = max(0, override.min_years)
        max_years = max(min_years, override.max_years)
        recommended_years = override.recommended_years
        if recommended_years is None:
            recommended_years = max_years
        recommended_years = min(max_years, max(min_years, recommended_years))

        existing = merged_ranges.get(capability)
        confidence = 0.96 if existing is not None else 0.9
        source = "user_reviewed_override" if existing is None else "user_reviewed_resume_inference"
        merged_ranges[capability] = CapabilityExperienceRange(
            capability=capability,
            min_years=min_years,
            max_years=max_years,
            confidence=confidence,
            source=source,
            recommended_years_override=recommended_years,
            evidence=((existing.source,) if existing is not None else ("panel_review",)),
            inferred_from=(() if existing is None else existing.inferred_from),
        )
    return merged_ranges


def _infer_related_capability_range(
    *,
    capability: str,
    profile: CandidateCapabilityProfile,
) -> CapabilityExperienceRange | None:
    family = _CAPABILITY_FAMILIES.get(capability)
    if family is None or not profile.capabilities:
        return None

    same_family = [
        item
        for item in profile.capabilities.values()
        if _CAPABILITY_FAMILIES.get(item.capability) == family
    ]
    if same_family:
        strongest = max(same_family, key=lambda item: (item.max_years, item.confidence))
        max_years = min(profile.total_career_years, max(1, round(strongest.max_years * 0.9)))
        min_years = max(1, max_years - 2)
        return CapabilityExperienceRange(
            capability=capability,
            min_years=min_years,
            max_years=max_years,
            confidence=0.48,
            source="related_family_inference",
            evidence=("same_family_capabilities",),
            inferred_from=tuple(sorted({item.capability for item in same_family}))[:6],
        )

    neighbor_families = set(_FAMILY_NEIGHBORS.get(family, ()))
    adjacent = [
        item
        for item in profile.capabilities.values()
        if _CAPABILITY_FAMILIES.get(item.capability) in neighbor_families
    ]
    if not adjacent:
        return None
    strongest = max(adjacent, key=lambda item: (item.max_years, item.confidence))
    max_years = min(profile.total_career_years, max(1, round(strongest.max_years * 0.7)))
    min_years = max(1, max_years - 2)
    max_years = min(max_years, 4)
    return CapabilityExperienceRange(
        capability=capability,
        min_years=min_years,
        max_years=max_years,
        confidence=0.34,
        source="adjacent_family_inference",
        evidence=("adjacent_capabilities",),
        inferred_from=tuple(sorted({item.capability for item in adjacent}))[:6],
    )


def _months_to_competitive_years(months: int, *, cap_years: int) -> int:
    rounded_years = max(1, math.ceil(months / 12))
    return min(cap_years, rounded_years) if cap_years > 0 else rounded_years


def _experience_entry_duration_months(entry: ResumeExperienceEntry) -> int | None:
    if entry.date_range is None:
        return None
    parsed = _parse_date_range_months(entry.date_range)
    if parsed is None:
        return None
    start, end = parsed
    return max(1, (end.year - start.year) * 12 + end.month - start.month + 1)


def _parse_date_range_months(date_range: str) -> tuple[datetime, datetime] | None:
    match = _DATE_RANGE_PATTERN.fullmatch(date_range.strip())
    if match is None:
        return None
    start = _parse_month_year(match.group("start"))
    end_text = match.group("end")
    end = datetime.now(UTC) if end_text == "Present" else _parse_month_year(end_text)
    if start is None or end is None:
        return None
    return start, end


def _parse_month_year(value: str) -> datetime | None:
    try:
        month_text, year_text = value.split("/", maxsplit=1)
        month = int(month_text)
        year = int(year_text)
    except ValueError:
        return None
    if month not in range(1, 13):
        return None
    return datetime(year=year, month=month, day=1, tzinfo=UTC)


def _normalize_lookup_text(value: str) -> str:
    lowered = value.lower()
    collapsed = re.sub(r"[^a-z0-9+#]+", " ", lowered)
    return " ".join(collapsed.split())
