from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from job_applier.application.config import (
    AgentConfig,
    AIConfig,
    RulesetConfig,
    ScheduleConfig,
    SearchConfig,
    UserAgentSettings,
    UserProfileConfig,
)
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import DebugExecutionStage, Platform, ResumeMode, SupportedLanguage
from job_applier.infrastructure.linkedin.search import LinkedInSearchCriteria
from job_applier.infrastructure.linkedin.search_score_cache import (
    CachedScoreDecision,
    LinkedInSearchScoreCache,
)


class _InMemoryJobPostingRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, JobPosting] = {}

    def save(self, entity: JobPosting) -> JobPosting:
        self._items[entity.id] = entity
        return entity

    def get(self, entity_id: UUID) -> JobPosting | None:
        return self._items.get(entity_id)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[JobPosting]:
        return list(self._items.values())[offset : offset + limit]

    def delete(self, entity_id: UUID) -> None:
        self._items.pop(entity_id, None)

    def find_by_external_job_id(
        self,
        *,
        platform: str,
        external_job_id: str,
    ) -> JobPosting | None:
        for posting in self._items.values():
            if posting.platform.value == platform and posting.external_job_id == external_job_id:
                return posting
        return None


def _make_settings(
    *,
    positive_filters: tuple[str, ...] = (),
    blacklist: tuple[str, ...] = (),
    minimum_score_threshold: float = 0.55,
) -> UserAgentSettings:
    return UserAgentSettings(
        profile=UserProfileConfig(
            name="Thiago Martins",
            email="thiago@example.com",
            phone="+55 11 99999-9999",
            city="Sao Paulo",
            work_authorized=True,
            needs_sponsorship=False,
            availability="Immediate",
            cv_path="resume.pdf",
            cv_filename="resume.pdf",
            resume_mode=ResumeMode.DYNAMIC,
            preferred_language=SupportedLanguage.PORTUGUESE,
            positive_filters=positive_filters,
            blacklist=blacklist,
        ),
        search=SearchConfig(
            keywords=("Desenvolvedor Backend",),
            location="Brasil",
            easy_apply_only=True,
            minimum_score_threshold=minimum_score_threshold,
        ),
        agent=AgentConfig(schedule=ScheduleConfig()),
        ai=AIConfig(model="gpt-5", api_key=None),
        ruleset=RulesetConfig(),
    )


def _make_posting() -> JobPosting:
    return JobPosting(
        platform=Platform.LINKEDIN,
        external_job_id="4424429497",
        url="https://www.linkedin.com/jobs/view/4424429497/",
        title="Java Developer with Elixir",
        company_name="AllianceIT Inc",
        description_raw="Backend role with Java, Kotlin and APIs.",
        location="Brazil",
        easy_apply=True,
    )


def _make_criteria() -> LinkedInSearchCriteria:
    return LinkedInSearchCriteria(
        keywords=("Desenvolvedor Backend",),
        keywords_text="Desenvolvedor Backend",
        active_role_target="Desenvolvedor Backend",
        location="Brasil",
        posted_within_hours=24,
        workplace_types=(),
        seniority=(),
        easy_apply_only=True,
        max_pages=4,
    )


class LinkedInSearchScoreCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.repository = _InMemoryJobPostingRepository()
        self.cache = LinkedInSearchScoreCache(
            cache_dir=Path(self._temp_dir.name),
            job_repository=self.repository,
            ttl_seconds=3600,
        )
        self.addCleanup(self.cache._cache.close)  # noqa: SLF001

    def test_campaign_cache_roundtrip_reuses_saved_postings(self) -> None:
        posting = self.repository.save(_make_posting())
        criteria = _make_criteria()

        self.assertIsNone(
            self.cache.load_campaign(criteria=criteria, stage=DebugExecutionStage.FULL)
        )

        self.cache.save_campaign(
            criteria=criteria,
            stage=DebugExecutionStage.FULL,
            postings=[posting],
        )

        cached_postings = self.cache.load_campaign(
            criteria=criteria,
            stage=DebugExecutionStage.FULL,
        )
        self.assertIsNotNone(cached_postings)
        self.assertEqual([item.id for item in cached_postings or []], [posting.id])

    def test_score_cache_roundtrip_invalidates_when_filters_change(self) -> None:
        posting = self.repository.save(_make_posting())
        settings = _make_settings(positive_filters=("java",))
        decision = CachedScoreDecision(
            selected=True,
            score=0.81,
            reason="Matched backend target",
            matched_role_target="Desenvolvedor Backend",
            matched_specializations=("java", "apis"),
        )

        self.assertIsNone(self.cache.load_score_decision(settings=settings, posting=posting))
        self.cache.save_score_decision(
            settings=settings,
            posting=posting,
            decision=decision,
        )

        cached = self.cache.load_score_decision(settings=settings, posting=posting)
        self.assertEqual(cached, decision)

        changed_settings = _make_settings(positive_filters=("elixir",))
        self.assertIsNone(
            self.cache.load_score_decision(settings=changed_settings, posting=posting)
        )


if __name__ == "__main__":
    unittest.main()
