"""Rule-based job scoring used to qualify fetched vacancies."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from job_applier.application.agent_execution import JobScorer, ScoredJobPosting
from job_applier.application.config import UserAgentSettings
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import WorkplaceType

logger = logging.getLogger(__name__)

TITLE_WEIGHT = 0.35
STACK_WEIGHT = 0.30
LOCATION_WEIGHT = 0.20
POSITIVE_KEYWORD_WEIGHT = 0.15

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

        searchable_text = normalize_text(
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
        stack_terms = tuple(settings.profile.years_experience_by_stack.keys())
        stack_matches = match_terms(stack_terms, searchable_text)
        positive_matches = match_terms(settings.profile.positive_filters, searchable_text)
        matched_specializations = match_specializations(
            stack_terms=tuple(
                stack_name
                for stack_name, _years in sorted(
                    settings.profile.years_experience_by_stack.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ),
            positive_terms=settings.profile.positive_filters,
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
        ]
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


def match_role_targets(
    role_targets: tuple[str, ...],
    normalized_title: str,
    searchable_text: str,
) -> RoleTargetMatchResult:
    """Return matching role targets plus the strongest match score."""

    scored_matches = [
        (target, compute_role_target_match_score(target, normalized_title, searchable_text))
        for target in role_targets
        if target.strip()
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
    searchable_text: str,
) -> tuple[str, ...]:
    """Return matched stack/specialization terms already grounded in user profile data."""

    matched_from_stack = match_terms(stack_terms, searchable_text)
    matched_from_positive = match_terms(positive_terms, searchable_text)
    merged_matches: list[str] = []
    seen: set[str] = set()
    for term in (*matched_from_stack, *matched_from_positive):
        normalized_term = normalize_text(term)
        if not normalized_term or normalized_term in seen:
            continue
        seen.add(normalized_term)
        merged_matches.append(term)
    return tuple(merged_matches)


def compute_role_target_match_score(
    role_target: str,
    normalized_title: str,
    searchable_text: str,
) -> float:
    """Compute how strongly a vacancy maps to one configured role family."""

    del searchable_text
    canonical_target = normalize_text(role_target)
    if not canonical_target:
        return 0.0

    title_tokens = set(normalized_title.split())
    target_tokens = tuple(token for token in canonical_target.split() if token)
    alias_patterns = _ROLE_TARGET_ALIAS_PATTERNS.get(canonical_target, ())

    if canonical_target in normalized_title:
        return 1.0
    if alias_patterns and any(re.search(pattern, normalized_title) for pattern in alias_patterns):
        return 1.0

    title_overlap = fraction(
        sum(token in title_tokens for token in target_tokens),
        len(target_tokens),
    )
    if title_overlap >= 0.5:
        return round(title_overlap, 4)
    return 0.0


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
