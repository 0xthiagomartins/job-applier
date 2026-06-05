"""Disk-backed cache for repeated LinkedIn search campaigns and score decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from diskcache import Cache  # type: ignore[import-untyped]

from job_applier.application.config import UserAgentSettings
from job_applier.application.repositories import JobPostingRepository
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import DebugExecutionStage
from job_applier.infrastructure.linkedin.search import LinkedInSearchCriteria

_CACHE_VERSION = 1
_CAMPAIGN_PREFIX = "linkedin-search-score:campaign:"
_SCORE_PREFIX = "linkedin-search-score:score:"


@dataclass(frozen=True, slots=True)
class CachedScoreDecision:
    """Cached qualification result reused before the apply step."""

    selected: bool
    score: float | None
    reason: str | None
    matched_role_target: str | None
    matched_specializations: tuple[str, ...]


class LinkedInSearchScoreCache:
    """Reuse one recent LinkedIn search+score pass for repeated local test runs."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        job_repository: JobPostingRepository,
        ttl_seconds: int,
    ) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache = Cache(str(cache_dir))
        self._job_repository = job_repository
        self._ttl_seconds = max(60, ttl_seconds)

    def load_campaign(
        self,
        *,
        criteria: LinkedInSearchCriteria,
        stage: DebugExecutionStage,
    ) -> list[JobPosting] | None:
        """Return cached postings for one fully fetched target campaign when available."""

        key, _ = self._campaign_cache_key(criteria=criteria, stage=stage)
        payload = self._cache.get(key)
        if not isinstance(payload, dict):
            return None
        posting_ids = payload.get("posting_ids")
        if not isinstance(posting_ids, list):
            self._cache.delete(key)
            return None

        postings: list[JobPosting] = []
        for raw_posting_id in posting_ids:
            try:
                posting_id = UUID(str(raw_posting_id))
            except ValueError:
                self._cache.delete(key)
                return None
            posting = self._job_repository.get(posting_id)
            if posting is None:
                self._cache.delete(key)
                return None
            postings.append(posting)
        return postings

    def save_campaign(
        self,
        *,
        criteria: LinkedInSearchCriteria,
        stage: DebugExecutionStage,
        postings: list[JobPosting],
    ) -> None:
        """Persist one fully fetched target campaign for later replay."""

        key, signature_json = self._campaign_cache_key(criteria=criteria, stage=stage)
        now = datetime.now(tz=UTC)
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        payload = {
            "kind": "campaign",
            "cache_version": _CACHE_VERSION,
            "signature_json": signature_json,
            "posting_ids": [str(posting.id) for posting in postings],
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        self._cache.set(key, payload, expire=self._ttl_seconds)

    def load_score_decision(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> CachedScoreDecision | None:
        """Return one cached score/match decision when the posting+settings still match."""

        key, _ = self._score_cache_key(settings=settings, posting=posting)
        payload = self._cache.get(key)
        if not isinstance(payload, dict):
            return None
        try:
            matched_specializations = tuple(
                str(item) for item in payload.get("matched_specializations", ())
            )
            return CachedScoreDecision(
                selected=bool(payload["selected"]),
                score=(float(payload["score"]) if payload.get("score") is not None else None),
                reason=(str(payload["reason"]) if payload.get("reason") is not None else None),
                matched_role_target=(
                    str(payload["matched_role_target"])
                    if payload.get("matched_role_target") is not None
                    else None
                ),
                matched_specializations=matched_specializations,
            )
        except KeyError, TypeError, ValueError:
            self._cache.delete(key)
            return None

    def save_score_decision(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
        decision: CachedScoreDecision,
    ) -> None:
        """Persist one score/match decision for a previously fetched posting."""

        key, signature_json = self._score_cache_key(settings=settings, posting=posting)
        now = datetime.now(tz=UTC)
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        payload = {
            "kind": "score",
            "cache_version": _CACHE_VERSION,
            "signature_json": signature_json,
            "selected": decision.selected,
            "score": decision.score,
            "reason": decision.reason,
            "matched_role_target": decision.matched_role_target,
            "matched_specializations": list(decision.matched_specializations),
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        self._cache.set(key, payload, expire=self._ttl_seconds)

    def _campaign_cache_key(
        self,
        *,
        criteria: LinkedInSearchCriteria,
        stage: DebugExecutionStage,
    ) -> tuple[str, str]:
        payload = {
            "cache_version": _CACHE_VERSION,
            "scope": "linkedin_search_campaign",
            "stage": stage.value,
            "active_role_target": criteria.active_role_target,
            "keywords": list(criteria.keywords),
            "keywords_text": criteria.keywords_text,
            "location": criteria.location,
            "posted_within_hours": criteria.posted_within_hours,
            "workplace_types": [item.value for item in criteria.workplace_types],
            "seniority": [item.value for item in criteria.seniority],
            "easy_apply_only": criteria.easy_apply_only,
            "max_pages": criteria.max_pages,
            "debug_target_job_url": criteria.debug_target_job_url,
        }
        signature_json = self._stable_json(payload)
        return f"{_CAMPAIGN_PREFIX}{self._digest(signature_json)}", signature_json

    def _score_cache_key(
        self,
        *,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> tuple[str, str]:
        payload = {
            "cache_version": _CACHE_VERSION,
            "scope": "job_score",
            "posting": {
                "id": str(posting.id),
                "external_job_id": posting.external_job_id,
                "url": posting.url,
                "title": posting.title,
                "company_name": posting.company_name,
                "description_hash": posting.description_hash,
                "easy_apply": posting.easy_apply,
                "workplace_type": posting.workplace_type.value if posting.workplace_type else None,
                "seniority": posting.seniority.value if posting.seniority else None,
            },
            "search": settings.search.model_dump(mode="json"),
            "positive_filters": list(settings.profile.positive_filters),
            "blacklist": list(settings.profile.blacklist),
        }
        signature_json = self._stable_json(payload)
        return f"{_SCORE_PREFIX}{self._digest(signature_json)}", signature_json

    def _stable_json(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _digest(self, value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()
