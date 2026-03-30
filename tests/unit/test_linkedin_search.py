import asyncio
from pathlib import Path
from typing import cast

from alembic import command
from alembic.config import Config
from playwright.async_api import Page
from pydantic import AnyUrl, SecretStr, TypeAdapter

from job_applier.application.config import (
    AgentConfig,
    AIConfig,
    RulesetConfig,
    ScheduleConfig,
    SearchConfig,
    UserAgentSettings,
    UserProfileConfig,
)
from job_applier.domain import Platform, ScheduleFrequency, SeniorityLevel, WorkplaceType
from job_applier.infrastructure.linkedin.browser_agent import BrowserTaskAssessment
from job_applier.infrastructure.linkedin.search import (
    LinkedInCollectedJob,
    LinkedInJobFetcher,
    LinkedInJobParser,
    LinkedInSearchCriteria,
    LinkedInSearchError,
    PlaywrightLinkedInJobsClient,
    build_paginated_search_url,
    build_search_criteria,
    build_search_results_url,
    infer_seniority,
    infer_workplace_type,
)
from job_applier.infrastructure.sqlite import (
    SqliteJobPostingRepository,
    create_session_factory,
)
from job_applier.settings import RuntimeSettings


def upgrade_to_head(database_url: str) -> None:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def test_search_criteria_and_parser_normalize_linkedin_jobs(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(data_dir=tmp_path, linkedin_max_search_pages=3)
    settings = build_user_agent_settings()

    criteria = build_search_criteria(settings, runtime_settings)
    paginated = build_paginated_search_url(
        "https://www.linkedin.com/jobs/search/?keywords=python&location=Remote",
        page_index=2,
    )
    direct_results_url = build_search_results_url(criteria)

    parser = LinkedInJobParser()
    posting = parser.parse(
        LinkedInCollectedJob(
            external_job_id="123456",
            url="https://www.linkedin.com/jobs/view/123456/?trackingId=abc",
            title="Senior Platform Engineer",
            company_name="Acme",
            location="Remote",
            description_raw="Build resilient agent automation workflows.",
            easy_apply=True,
            metadata_text="Remote | Mid-Senior level | Easy Apply",
        ),
    )

    assert criteria.keywords_text == "python automation"
    assert criteria.max_pages == 3
    assert "start=50" in paginated
    assert "keywords=python+automation" in direct_results_url
    assert "location=Remote" in direct_results_url
    assert "f_AL=true" in direct_results_url
    assert "f_TPR=r86400" in direct_results_url
    assert infer_workplace_type("This is a remote-first role.") is WorkplaceType.REMOTE
    assert (
        infer_seniority("We are hiring for a mid-senior level backend role.")
        is SeniorityLevel.SENIOR
    )
    assert posting.platform is Platform.LINKEDIN
    assert posting.external_job_id == "123456"
    assert posting.easy_apply is True
    assert posting.workplace_type is WorkplaceType.REMOTE
    assert posting.seniority is SeniorityLevel.SENIOR


def test_linkedin_job_fetcher_persists_and_deduplicates_by_external_id(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'linkedin-search.db').resolve()}"
    upgrade_to_head(database_url)

    runtime_settings = RuntimeSettings(data_dir=tmp_path)
    repository = SqliteJobPostingRepository(create_session_factory(database_url))
    settings = build_user_agent_settings()

    class FakeLinkedInJobsClient:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_jobs(self, criteria: LinkedInSearchCriteria) -> list[LinkedInCollectedJob]:
            self.calls += 1
            assert criteria.location == "Remote"
            if self.calls == 1:
                return [
                    LinkedInCollectedJob(
                        external_job_id="job-001",
                        url="https://www.linkedin.com/jobs/view/job-001",
                        title="Automation Engineer",
                        company_name="Acme",
                        location="Remote",
                        description_raw="Build browser automation.",
                        easy_apply=True,
                        metadata_text="Remote | Mid-Senior level | Easy Apply",
                    ),
                    LinkedInCollectedJob(
                        external_job_id="job-001",
                        url="https://www.linkedin.com/jobs/view/job-001?duplicate=true",
                        title="Automation Engineer Duplicate",
                        company_name="Acme",
                        location="Remote",
                        description_raw="Duplicate listing should be ignored in the same batch.",
                        easy_apply=True,
                        metadata_text="Remote | Mid-Senior level | Easy Apply",
                    ),
                ]
            return [
                LinkedInCollectedJob(
                    external_job_id="job-001",
                    url="https://www.linkedin.com/jobs/view/job-001",
                    title="Automation Engineer Updated",
                    company_name="Acme",
                    location="Remote",
                    description_raw="Updated description from a later search.",
                    easy_apply=True,
                    metadata_text="Remote | Mid-Senior level | Easy Apply",
                ),
            ]

    fetcher = LinkedInJobFetcher(
        client=FakeLinkedInJobsClient(),
        runtime_settings=runtime_settings,
        job_repository=repository,
    )

    first_batch = asyncio.run(fetcher.fetch(settings))
    second_batch = asyncio.run(fetcher.fetch(settings))
    stored = repository.list()

    assert len(first_batch) == 1
    assert len(second_batch) == 1
    assert len(stored) == 1
    assert stored[0].external_job_id == "job-001"
    assert stored[0].title == "Automation Engineer Updated"


def test_wait_for_search_surface_returns_after_first_complete_assessment(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(data_dir=tmp_path)
    client = PlaywrightLinkedInJobsClient(runtime_settings)
    criteria = build_search_criteria(build_user_agent_settings(), runtime_settings)
    assessments = iter(
        (
            BrowserTaskAssessment(status="pending", confidence=0.7, summary="loading"),
            BrowserTaskAssessment(status="complete", confidence=0.95, summary="results ready"),
        )
    )

    class FakePage:
        def __init__(self) -> None:
            self.waits: list[int] = []

        async def wait_for_timeout(self, milliseconds: int) -> None:
            self.waits.append(milliseconds)

    page = FakePage()

    async def scenario() -> None:
        async def fake_has_cards(page: object, *, attempts: int = 3) -> bool:
            return False

        async def fake_assess(
            page: object,
            criteria: LinkedInSearchCriteria,
        ) -> BrowserTaskAssessment:
            return next(assessments)

        client._wait_for_extractable_search_cards = fake_has_cards  # type: ignore[method-assign]
        client._assess_search_surface = fake_assess  # type: ignore[method-assign]
        result = await client._wait_for_search_surface(cast(Page, page), criteria=criteria)
        assert result.status == "complete"
        assert page.waits == [750]

    asyncio.run(scenario())


def test_wait_for_search_surface_raises_when_page_never_settles(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(data_dir=tmp_path)
    client = PlaywrightLinkedInJobsClient(runtime_settings)
    criteria = build_search_criteria(build_user_agent_settings(), runtime_settings)

    class FakePage:
        async def wait_for_timeout(self, milliseconds: int) -> None:
            return None

    async def scenario() -> None:
        async def fake_has_cards(page: object, *, attempts: int = 3) -> bool:
            return False

        async def fake_assess(
            page: object,
            criteria: LinkedInSearchCriteria,
        ) -> BrowserTaskAssessment:
            return BrowserTaskAssessment(status="pending", confidence=0.4, summary="still loading")

        client._wait_for_extractable_search_cards = fake_has_cards  # type: ignore[method-assign]
        client._assess_search_surface = fake_assess  # type: ignore[method-assign]
        try:
            await client._wait_for_search_surface(cast(Page, FakePage()), criteria=criteria)
        except LinkedInSearchError as exc:
            assert "still loading" in str(exc)
        else:
            raise AssertionError(
                "Expected LinkedInSearchError when the search surface never settles."
            )

    asyncio.run(scenario())


def test_wait_for_search_surface_short_circuits_when_job_cards_are_visible(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(data_dir=tmp_path)
    client = PlaywrightLinkedInJobsClient(runtime_settings)
    criteria = build_search_criteria(build_user_agent_settings(), runtime_settings)

    class FakePage:
        async def wait_for_timeout(self, milliseconds: int) -> None:
            return None

    async def scenario() -> None:
        async def fake_has_cards(page: object, *, attempts: int = 3) -> bool:
            return True

        async def fail_assessment(
            page: object,
            criteria: LinkedInSearchCriteria,
        ) -> BrowserTaskAssessment:
            raise AssertionError("The browser assessor should not run when job cards are visible.")

        client._wait_for_extractable_search_cards = fake_has_cards  # type: ignore[method-assign]
        client._assess_search_surface = fail_assessment  # type: ignore[method-assign]
        result = await client._wait_for_search_surface(cast(Page, FakePage()), criteria=criteria)
        assert result.status == "complete"
        assert "job cards" in result.summary.lower()

    asyncio.run(scenario())


def build_user_agent_settings() -> UserAgentSettings:
    url_adapter = TypeAdapter(AnyUrl)
    return UserAgentSettings(
        config_version="config-v1",
        profile=UserProfileConfig(
            name="Thiago Martins",
            email="thiago@example.com",
            phone="+5511999999999",
            city="Sao Paulo",
            linkedin_url=url_adapter.validate_python("https://www.linkedin.com/in/thiago"),
            github_url=url_adapter.validate_python("https://github.com/0xthiagomartins"),
            portfolio_url=url_adapter.validate_python("https://thiago.example.com"),
            years_experience_by_stack={"python": 8},
            work_authorized=True,
            availability="Immediate",
            default_responses={"work_authorization": "Yes"},
            positive_filters=("python",),
            blacklist=("internship",),
        ),
        search=SearchConfig(
            keywords=("python", "automation"),
            location="Remote",
            posted_within_hours=24,
            workplace_types=(WorkplaceType.REMOTE,),
            seniority=(SeniorityLevel.SENIOR,),
            easy_apply_only=True,
        ),
        agent=AgentConfig(
            schedule=ScheduleConfig(
                frequency=ScheduleFrequency.DAILY,
                run_at="23:00",
                timezone="UTC",
            ),
            auto_connect_with_recruiter=False,
        ),
        ai=AIConfig(api_key=SecretStr("sk-test"), model="o3-mini"),
        ruleset=RulesetConfig(version="ruleset-v1"),
    )
