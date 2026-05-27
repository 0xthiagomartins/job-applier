"""Rule-based job scoring used to qualify fetched vacancies."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from job_applier.application.agent_execution import JobScorer, ScoredJobPosting
from job_applier.application.config import UserAgentSettings
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import WorkplaceType
from job_applier.infrastructure.candidate_capabilities import build_candidate_capability_profile

logger = logging.getLogger(__name__)

TITLE_WEIGHT = 0.35
STACK_WEIGHT = 0.30
LOCATION_WEIGHT = 0.20
POSITIVE_KEYWORD_WEIGHT = 0.15
DETAIL_QUALITY_FOR_DESCRIPTION_MATCHING = 0.55
DETAIL_DESCRIPTION_CONTEXT_THRESHOLD = 0.45

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
        r"\bback[\s\-]?end\b",
        r"\bbackend (engineer|developer)\b",
        r"\bback[\s\-]?end (engineer|developer)\b",
        r"\bserver[\s\-]?side\b",
    ),
    "full stack developer": (
        r"\bfull[\s\-]?stack\b",
        r"\bfullstack\b",
    ),
}

_ROLE_TARGET_SPECIALIZATION_HINTS: dict[str, tuple[str, ...]] = {
    "automation engineer": ("uipath", "langchain", "rag"),
    "automation developer": ("uipath", "langchain", "rag"),
    "rpa developer": ("uipath",),
    "backend developer": ("python", "java", "fastapi"),
    "full stack developer": ("javascript", "typescript", "react", "react native"),
}

_ROLE_TARGET_CANONICAL_ALIASES: dict[str, str] = {
    "automation engineer": "automation engineer",
    "automation developer": "automation developer",
    "rpa developer": "rpa developer",
    "backend developer": "backend developer",
    "full stack developer": "full stack developer",
    "engenheiro de automacao": "automation engineer",
    "desenvolvedor de automacao": "automation developer",
    "desenvolvedor rpa": "rpa developer",
    "desenvolvedor backend": "backend developer",
    "desenvolvedor full stack": "full stack developer",
}

_GENERIC_ENGINEERING_ROLE_PATTERNS: tuple[str, ...] = (
    r"\bengineer\b",
    r"\bdeveloper\b",
    r"\bspecialist\b",
    r"\bprogrammer\b",
)

_GENERIC_SOFTWARE_ROLE_PATTERNS: tuple[str, ...] = (
    r"\bsoftware engineer\b",
    r"\bsoftware developer\b",
    r"\bapplication developer\b",
    r"\bapplications engineer\b",
    r"\bengenheir(?:o|a)(?:\s+a)?\s+de\s+software\b",
    r"\bdesenvolvedor(?:a)?(?:\s+a)?\s+de\s+software\b",
    r"\bingenier(?:o|a)\s+de\s+software\b",
    r"\bdesarrollador(?:a)?\s+de\s+software\b",
)

_GENERIC_ROLE_TARGET_TOKENS = frozenset({"engineer", "developer", "specialist", "programmer"})

_TITLE_ROLE_HARD_EXCLUSION_PATTERNS: tuple[str, ...] = (
    r"\bsdet\b",
    r"\bsoftware engineer in test\b",
    r"\bengineer in test\b",
    r"\btest engineer\b",
    r"\bqa engineer\b",
    r"\bquality assurance\b",
    r"\bquality engineer\b",
    r"\btester\b",
    r"\bsupport engineer\b",
    r"\bnetwork support\b",
    r"\bcustomer support\b",
    r"\bit support\b",
    r"\bhelp desk\b",
)

_TITLE_NON_SOFTWARE_DISCIPLINE_PATTERNS: tuple[str, ...] = (
    r"\belectrical engineer\b",
    r"\bmechanical engineer\b",
    r"\bcivil engineer\b",
    r"\bstructural engineer\b",
    r"\bmanufacturing engineer\b",
    r"\bindustrial engineer\b",
    r"\bpower engineer\b",
    r"\btransmission engineer\b",
    r"\bcontrols engineer\b",
    r"\bautomation controls engineer\b",
    r"\bbiomedical engineer\b",
    r"\bchemical engineer\b",
)

_TITLE_SPECIALIZATION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", (r"\bpython\b",)),
    ("javascript", (r"\bjavascript\b", r"\bnode\.?js\b")),
    ("typescript", (r"\btypescript\b",)),
    ("java", (r"\bjava\b",)),
    ("aws", (r"\baws\b",)),
    ("gcp", (r"\bgcp\b", r"\bgoogle cloud\b")),
    ("azure", (r"\bazure\b",)),
    ("react", (r"\breact\b",)),
    ("react native", (r"\breact[\s\-]?native\b",)),
    ("fastapi", (r"\bfastapi\b",)),
    ("uipath", (r"\buipath\b",)),
    ("langchain", (r"\blangchain\b",)),
    ("rag", (r"\brag\b", r"\bretrieval[\s\-]augmented generation\b")),
)

_GENERIC_SOFTWARE_ROLE_TARGET_SCORES: dict[str, float] = {
    "backend developer": 0.56,
    "full stack developer": 0.6,
}


@dataclass(frozen=True, slots=True)
class ScoreComputation:
    """Computed score details used for logging and decision-making."""

    score: float
    selected: bool
    reason: str
    matched_role_target: str | None
    matched_specializations: tuple[str, ...]
    title_matches: tuple[str, ...]
    stack_matches: tuple[str, ...]
    positive_matches: tuple[str, ...]
    blacklist_matches: tuple[str, ...]
    title_component: float
    stack_component: float
    location_component: float
    positive_component: float
    threshold: float


class RuleBasedJobScorer(JobScorer):
    """Score vacancies with deterministic rules and user-configured keywords."""

    async def score(self, settings: UserAgentSettings, posting: JobPosting) -> ScoredJobPosting:
        computation = self.compute(settings, posting)
        logger.info(
            "job_score_stage",
            extra={
                "job_posting_id": str(posting.id),
                "selected": computation.selected,
                "score": computation.score,
                "threshold": computation.threshold,
                "matched_role_target": computation.matched_role_target,
                "matched_specializations": list(computation.matched_specializations),
                "title_matches": list(computation.title_matches),
                "stack_matches": list(computation.stack_matches),
                "positive_matches": list(computation.positive_matches),
                "blacklist_matches": list(computation.blacklist_matches),
                "reason": computation.reason,
            },
        )
        return ScoredJobPosting(
            posting=posting,
            selected=computation.selected,
            score=computation.score,
            reason=computation.reason,
            matched_role_target=computation.matched_role_target,
            matched_specializations=computation.matched_specializations,
        )

    def compute(self, settings: UserAgentSettings, posting: JobPosting) -> ScoreComputation:
        """Return the deterministic score computation for one vacancy."""

        full_searchable_text = normalize_text(
            " ".join(
                filter(
                    None,
                    (
                        posting.title,
                        posting.company_name,
                        posting.location,
                        posting.description_raw,
                    ),
                ),
            ),
        )
        searchable_text = _searchable_job_context(
            posting,
            full_searchable_text=full_searchable_text,
        )
        normalized_title = normalize_text(posting.title)
        threshold = settings.search.minimum_score_threshold

        blacklist_matches = match_terms(settings.profile.blacklist, searchable_text)
        if blacklist_matches:
            reason = f"Rejected by blacklist: {', '.join(blacklist_matches)}."
            return ScoreComputation(
                score=0.0,
                selected=False,
                reason=reason,
                matched_role_target=None,
                matched_specializations=(),
                title_matches=(),
                stack_matches=(),
                positive_matches=(),
                blacklist_matches=blacklist_matches,
                title_component=0.0,
                stack_component=0.0,
                location_component=0.0,
                positive_component=0.0,
                threshold=threshold,
            )

        hard_rejection_reason = evaluate_hard_rejection(settings, posting)
        if hard_rejection_reason is not None:
            return ScoreComputation(
                score=0.0,
                selected=False,
                reason=hard_rejection_reason,
                matched_role_target=None,
                matched_specializations=(),
                title_matches=(),
                stack_matches=(),
                positive_matches=(),
                blacklist_matches=(),
                title_component=0.0,
                stack_component=0.0,
                location_component=0.0,
                positive_component=0.0,
                threshold=threshold,
            )

        role_target_match = match_role_targets(
            settings.search.keywords,
            normalized_title,
            searchable_text,
        )
        title_component = role_target_match.best_score
        if settings.search.keywords and not role_target_match.matching_targets:
            reason = (
                "Rejected because the job title does not map clearly to the configured role "
                f"targets; title={role_target_match.best_score:.2f}."
            )
            return ScoreComputation(
                score=0.0,
                selected=False,
                reason=reason,
                matched_role_target=None,
                matched_specializations=(),
                title_matches=(),
                stack_matches=(),
                positive_matches=(),
                blacklist_matches=(),
                title_component=role_target_match.best_score,
                stack_component=0.0,
                location_component=0.0,
                positive_component=0.0,
                threshold=threshold,
            )
        reviewed_capability_profile = build_candidate_capability_profile(settings)
        stack_terms = tuple(
            item.capability
            for item in sorted(
                reviewed_capability_profile.capabilities.values(),
                key=lambda item: (item.recommended_years, item.confidence, item.capability),
                reverse=True,
            )
            if item.source
            in {
                "profile_years",
                "user_reviewed_override",
                "user_reviewed_resume_inference",
            }
        )
        stack_matches = match_terms(stack_terms, searchable_text)
        positive_matches = match_terms(settings.profile.positive_filters, searchable_text)
        matched_specializations = match_specializations(
            stack_terms=stack_terms,
            positive_terms=settings.profile.positive_filters,
            normalized_title=normalized_title,
            searchable_text=searchable_text,
        )
        stack_component = fraction(len(stack_matches), len(stack_terms))
        location_component = compute_location_component(settings, posting, searchable_text)
        positive_component = fraction(len(positive_matches), len(settings.profile.positive_filters))

        weighted_components = [
            (TITLE_WEIGHT, title_component, bool(settings.search.keywords)),
            (STACK_WEIGHT, stack_component, bool(stack_terms)),
            (
                LOCATION_WEIGHT,
                location_component,
                bool(settings.search.location or settings.search.workplace_types),
            ),
            (
                POSITIVE_KEYWORD_WEIGHT,
                positive_component,
                bool(settings.profile.positive_filters),
            ),
        ]
        total_weight = sum(weight for weight, _, enabled in weighted_components if enabled)
        weighted_score = sum(
            weight * component for weight, component, enabled in weighted_components if enabled
        )
        score = round(weighted_score / total_weight, 4) if total_weight else 0.0
        selected = score >= threshold

        reason_parts = [
            f"title={title_component:.2f}",
            f"stack={stack_component:.2f}",
            f"location={location_component:.2f}",
            f"positive={positive_component:.2f}",
            f"detail_quality={posting.detail_quality_score:.2f}",
            f"detail_description={posting.detail_description_score:.2f}",
        ]
        if posting.detail_description_score < DETAIL_DESCRIPTION_CONTEXT_THRESHOLD:
            reason_parts.append("description context downweighted due to sparse detail extraction")
        if positive_matches:
            reason_parts.append(f"positive matches: {', '.join(positive_matches)}")

        if selected:
            reason = f"Accepted with score {score:.2f} >= {threshold:.2f}; " + "; ".join(
                reason_parts
            )
        else:
            reason = f"Rejected with score {score:.2f} < {threshold:.2f}; " + "; ".join(
                reason_parts
            )
        return ScoreComputation(
            score=score,
            selected=selected,
            reason=reason,
            matched_role_target=role_target_match.best_target,
            matched_specializations=matched_specializations,
            title_matches=role_target_match.matching_targets,
            stack_matches=stack_matches,
            positive_matches=positive_matches,
            blacklist_matches=(),
            title_component=title_component,
            stack_component=stack_component,
            location_component=location_component,
            positive_component=positive_component,
            threshold=threshold,
        )


def normalize_text(value: str | None) -> str:
    """Normalize text for case-insensitive term matching."""

    if value is None:
        return ""
    lowered = value.lower()
    collapsed = re.sub(r"[^a-z0-9+#]+", " ", lowered)
    return " ".join(collapsed.split())


def match_terms(terms: tuple[str, ...], normalized_text: str) -> tuple[str, ...]:
    """Return normalized terms found in the provided text."""

    matches: list[str] = []
    for term in terms:
        normalized_term = normalize_text(term)
        if not normalized_term:
            continue
        if normalized_term in normalized_text and normalized_term not in matches:
            matches.append(normalized_term)
    return tuple(matches)


def _searchable_job_context(
    posting: JobPosting,
    *,
    full_searchable_text: str,
) -> str:
    if (
        posting.detail_quality_score >= DETAIL_QUALITY_FOR_DESCRIPTION_MATCHING
        and posting.detail_description_score >= DETAIL_DESCRIPTION_CONTEXT_THRESHOLD
    ):
        return full_searchable_text
    return normalize_text(
        " ".join(
            filter(
                None,
                (
                    posting.title,
                    posting.company_name,
                    posting.location,
                ),
            ),
        ),
    )


def match_role_targets(
    role_targets: tuple[str, ...],
    normalized_title: str,
    searchable_text: str,
) -> RoleTargetMatchResult:
    """Return matching role targets plus the strongest match score."""

    canonical_role_targets: list[str] = []
    seen_targets: set[str] = set()
    for target in role_targets:
        canonical_target = canonicalize_role_target_label(target)
        if not canonical_target or canonical_target in seen_targets:
            continue
        seen_targets.add(canonical_target)
        canonical_role_targets.append(canonical_target)

    scored_matches = [
        (
            target,
            compute_role_target_match_score(target, normalized_title, searchable_text),
        )
        for target in canonical_role_targets
    ]
    title_matches = tuple(target for target, score in scored_matches if score >= 0.55)
    best_target = None
    best_score = 0.0
    for target, score in scored_matches:
        if score > best_score:
            best_target = target
            best_score = score
    if best_target is not None and best_score < 0.55:
        best_target = None
    return RoleTargetMatchResult(
        matching_targets=title_matches,
        best_target=best_target,
        best_score=best_score,
    )


@dataclass(frozen=True, slots=True)
class RoleTargetMatchResult:
    """Best-fit mapping between a vacancy title and configured role families."""

    matching_targets: tuple[str, ...]
    best_target: str | None
    best_score: float


def match_specializations(
    *,
    stack_terms: tuple[str, ...],
    positive_terms: tuple[str, ...],
    normalized_title: str,
    searchable_text: str,
) -> tuple[str, ...]:
    """Return target stack cues enriched by profile-aligned filters when available."""

    title_specializations = extract_title_specializations(normalized_title)
    matched_from_stack = match_terms(stack_terms, searchable_text)
    matched_from_positive = match_terms(positive_terms, searchable_text)
    merged_matches: list[str] = []
    seen: set[str] = set()
    for term in (*title_specializations, *matched_from_stack, *matched_from_positive):
        normalized_term = normalize_text(term)
        if not normalized_term or normalized_term in seen:
            continue
        seen.add(normalized_term)
        merged_matches.append(term)
    return tuple(merged_matches)


def extract_title_specializations(normalized_title: str) -> tuple[str, ...]:
    """Return explicit stack cues found in the vacancy title in title order."""

    positioned_matches: list[tuple[int, int, str]] = []
    for pattern_index, (label, patterns) in enumerate(_TITLE_SPECIALIZATION_PATTERNS):
        earliest_position: int | None = None
        for pattern in patterns:
            match = re.search(pattern, normalized_title)
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


def compute_role_target_match_score(
    role_target: str,
    normalized_title: str,
    searchable_text: str,
) -> float:
    """Compute how strongly a vacancy maps to one configured role family."""

    del searchable_text
    canonical_target = canonicalize_role_target_label(role_target)
    if not canonical_target:
        return 0.0

    if _title_has_hard_exclusion(normalized_title):
        return 0.0

    title_tokens = set(normalized_title.split())
    target_tokens = tuple(
        token
        for token in canonical_target.split()
        if token and token not in _GENERIC_ROLE_TARGET_TOKENS
    )
    alias_patterns = _ROLE_TARGET_ALIAS_PATTERNS.get(canonical_target, ())
    title_specializations = extract_title_specializations(normalized_title)

    if canonical_target in normalized_title:
        return 1.0
    if alias_patterns and any(re.search(pattern, normalized_title) for pattern in alias_patterns):
        return 1.0

    title_overlap = fraction(
        sum(token in title_tokens for token in target_tokens),
        len(target_tokens),
    )
    inferred_score = _infer_role_target_score_from_title(
        canonical_target=canonical_target,
        normalized_title=normalized_title,
        title_specializations=title_specializations,
    )
    if title_overlap >= 0.5:
        return round(max(title_overlap, inferred_score), 4)
    return round(inferred_score, 4)


def canonicalize_role_target_label(role_target: str) -> str:
    """Map localized target labels into one canonical internal role family."""

    normalized_target = normalize_text(role_target)
    if not normalized_target:
        return ""
    return _ROLE_TARGET_CANONICAL_ALIASES.get(normalized_target, normalized_target)


def _infer_role_target_score_from_title(
    *,
    canonical_target: str,
    normalized_title: str,
    title_specializations: tuple[str, ...],
) -> float:
    """Infer role-family fit from generic engineering titles plus explicit stack cues."""

    if any(re.search(pattern, normalized_title) for pattern in _GENERIC_SOFTWARE_ROLE_PATTERNS):
        software_role_score = _GENERIC_SOFTWARE_ROLE_TARGET_SCORES.get(canonical_target)
        if software_role_score is not None:
            return software_role_score

    if not any(
        re.search(pattern, normalized_title) for pattern in _GENERIC_ENGINEERING_ROLE_PATTERNS
    ):
        return 0.0

    specialization_hints = _ROLE_TARGET_SPECIALIZATION_HINTS.get(canonical_target, ())
    if not specialization_hints:
        return 0.0

    matched_hints = [
        hint for hint in specialization_hints if normalize_text(hint) in title_specializations
    ]
    if not matched_hints:
        return 0.0

    hint_coverage = fraction(len(matched_hints), len(specialization_hints))
    return min(0.85, round(0.65 + 0.20 * hint_coverage, 4))


def _title_has_hard_exclusion(normalized_title: str) -> bool:
    """Return whether the title belongs to a role category we should reject outright."""

    return any(
        re.search(pattern, normalized_title) for pattern in _TITLE_ROLE_HARD_EXCLUSION_PATTERNS
    ) or any(
        re.search(pattern, normalized_title) for pattern in _TITLE_NON_SOFTWARE_DISCIPLINE_PATTERNS
    )


def fraction(matches: int, total: int) -> float:
    """Return a bounded score fraction."""

    if total == 0:
        return 0.0
    return min(1.0, matches / total)


def compute_location_component(
    settings: UserAgentSettings,
    posting: JobPosting,
    searchable_text: str,
) -> float:
    """Score location and workplace compatibility."""

    requested_location = normalize_text(settings.search.location)
    posting_location = normalize_text(posting.location)

    workplace_match = True
    if settings.search.workplace_types and posting.workplace_type is not None:
        workplace_match = posting.workplace_type in settings.search.workplace_types

    location_match = True
    if requested_location:
        location_match = (
            requested_location in posting_location
            or posting_location in requested_location
            or requested_location in searchable_text
            or ("remote" in requested_location and posting.workplace_type == WorkplaceType.REMOTE)
        )

    if workplace_match and location_match:
        return 1.0
    if workplace_match or location_match:
        return 0.5
    return 0.0


def evaluate_hard_rejection(settings: UserAgentSettings, posting: JobPosting) -> str | None:
    """Return a hard-rejection reason when the vacancy is incompatible."""

    if settings.search.easy_apply_only and not posting.easy_apply:
        return "Rejected because the vacancy is not Easy Apply."

    if settings.search.workplace_types and posting.workplace_type is not None:
        if posting.workplace_type not in settings.search.workplace_types:
            return (
                "Rejected due to workplace mismatch: "
                f"{posting.workplace_type.value} is outside the configured filters."
            )

    if settings.search.seniority and posting.seniority is not None:
        if posting.seniority not in settings.search.seniority:
            return (
                "Rejected due to seniority mismatch: "
                f"{posting.seniority.value} is outside the configured filters."
            )

    return None
