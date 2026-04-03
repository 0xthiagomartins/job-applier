import asyncio
from pathlib import Path
from typing import cast

from alembic import command
from alembic.config import Config
from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
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
    LinkedInResultsPageCollection,
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


def test_search_criteria_caps_page_depth_in_agent_test_mode(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(
        data_dir=tmp_path,
        agent_test_mode=True,
        linkedin_max_search_pages=6,
    )

    criteria = build_search_criteria(build_user_agent_settings(), runtime_settings)

    assert criteria.max_pages == 2


def test_search_criteria_carries_debug_target_job_url(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(
        data_dir=tmp_path,
        linkedin_debug_target_job_url=TypeAdapter(AnyUrl).validate_python(
            "https://www.linkedin.com/jobs/view/1234567890/"
        ),
    )

    criteria = build_search_criteria(build_user_agent_settings(), runtime_settings)

    assert criteria.debug_target_job_url == "https://www.linkedin.com/jobs/view/1234567890/"


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


def test_collect_listing_cards_from_results_page_hydrates_virtualized_results(
    tmp_path: Path,
) -> None:
    runtime_settings = RuntimeSettings(data_dir=tmp_path)
    client = PlaywrightLinkedInJobsClient(runtime_settings)

    async def scenario() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.set_content(
                    """
                    <style>
                      body { margin: 0; font-family: sans-serif; }
                      #results {
                        height: 240px;
                        overflow-y: auto;
                        border: 1px solid #ddd;
                      }
                      ul {
                        list-style: none;
                        margin: 0;
                        padding: 0;
                      }
                      li {
                        min-height: 120px;
                        padding: 12px;
                        border-bottom: 1px solid #eee;
                      }
                      a {
                        display: inline-block;
                        margin-bottom: 6px;
                      }
                    </style>
                    <div id="results"><ul id="jobs"></ul></div>
                    <script>
                      const allJobs = [
                        ["101", "Senior Python Developer", "Acme", "Remote"],
                        ["102", "Staff Python Engineer", "Acme", "Remote"],
                        ["103", "Automation Engineer", "Acme", "Remote"],
                        ["104", "Data Platform Engineer", "Acme", "Remote"],
                        ["105", "Backend Engineer", "Acme", "Remote"],
                        ["106", "AI Automations Engineer", "Acme", "Remote"],
                      ];
                      let loaded = 0;
                      const list = document.getElementById("jobs");
                      const results = document.getElementById("results");
                      const appendBatch = (count) => {
                        const nextJobs = allJobs.slice(loaded, loaded + count);
                        for (const [jobId, title, company, location] of nextJobs) {
                          const li = document.createElement("li");
                          li.innerHTML = `
                            <a href="https://www.linkedin.com/jobs/view/${jobId}/">${title}</a>
                            <div>${company}</div>
                            <div>${location}</div>
                            <div>Easy Apply</div>
                          `;
                          list.appendChild(li);
                        }
                        loaded += nextJobs.length;
                      };
                      appendBatch(2);
                      results.addEventListener("scroll", () => {
                        const nearBottom =
                          results.scrollTop + results.clientHeight >= results.scrollHeight - 24;
                        if (nearBottom && loaded < allJobs.length) {
                          appendBatch(2);
                        }
                      });
                    </script>
                    """
                )

                collection = await client._collect_listing_cards_from_results_page(
                    page,
                    page_index=1,
                )

                assert isinstance(collection, LinkedInResultsPageCollection)
                assert len(collection.listings) == 6
                assert collection.rounds >= 2
                assert {listing.external_job_id for listing in collection.listings} == {
                    "101",
                    "102",
                    "103",
                    "104",
                    "105",
                    "106",
                }
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_paginated_results_navigation_retries_after_timeout(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(data_dir=tmp_path, linkedin_default_timeout_ms=15_000)
    client = PlaywrightLinkedInJobsClient(runtime_settings)

    class FakePage:
        def __init__(self) -> None:
            self.url = "https://www.linkedin.com/jobs/search/?keywords=python+automation"
            self.goto_calls = 0
            self.waits: list[int] = []

        async def goto(
            self,
            url: str,
            *,
            wait_until: str,
            timeout: int,
        ) -> None:
            del wait_until, timeout
            self.goto_calls += 1
            self.url = url
            if self.goto_calls == 1:
                raise PlaywrightTimeoutError("timed out")

        async def wait_for_timeout(self, milliseconds: int) -> None:
            self.waits.append(milliseconds)

    async def scenario() -> None:
        async def fake_wait_for_extractable_search_cards(
            page: object,
            *,
            attempts: int = 3,
        ) -> bool:
            del page, attempts
            return False

        client._wait_for_extractable_search_cards = (  # type: ignore[method-assign]
            fake_wait_for_extractable_search_cards
        )
        page = FakePage()
        await client._goto_paginated_results_page(
            cast(Page, page),
            target_page_url=(
                "https://www.linkedin.com/jobs/search/?keywords=python+automation&start=25"
            ),
            page_index=2,
        )

        assert page.goto_calls == 2
        assert page.waits == [1250]

    asyncio.run(scenario())


def test_paginated_results_navigation_accepts_ready_page_after_timeout(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(data_dir=tmp_path, linkedin_default_timeout_ms=15_000)
    client = PlaywrightLinkedInJobsClient(runtime_settings)

    class FakePage:
        def __init__(self) -> None:
            self.url = "https://www.linkedin.com/jobs/search/?keywords=python+automation&start=25"

        async def goto(
            self,
            url: str,
            *,
            wait_until: str,
            timeout: int,
        ) -> None:
            del url, wait_until, timeout
            raise PlaywrightTimeoutError("timed out")

        async def wait_for_timeout(self, milliseconds: int) -> None:
            raise AssertionError("retry wait should not happen when the page is already ready")

    async def scenario() -> None:
        async def fake_wait_for_extractable_search_cards(
            page: object,
            *,
            attempts: int = 3,
        ) -> bool:
            del page, attempts
            return True

        client._wait_for_extractable_search_cards = (  # type: ignore[method-assign]
            fake_wait_for_extractable_search_cards
        )
        await client._goto_paginated_results_page(
            cast(Page, FakePage()),
            target_page_url=(
                "https://www.linkedin.com/jobs/search/?keywords=python+automation&start=25"
            ),
            page_index=2,
        )

    asyncio.run(scenario())


def test_fetch_jobs_once_bypasses_search_when_debug_target_job_url_is_set(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(
        data_dir=tmp_path,
        linkedin_debug_target_job_url=TypeAdapter(AnyUrl).validate_python(
            "https://www.linkedin.com/jobs/view/1234567890/"
        ),
    )
    client = PlaywrightLinkedInJobsClient(runtime_settings)
    criteria = build_search_criteria(build_user_agent_settings(), runtime_settings)

    class FakeContext:
        pass

    async def scenario() -> None:
        async def fail_open_search(
            page: object,
            criteria: LinkedInSearchCriteria,
            *,
            run_dir: Path,
        ) -> None:
            del page, criteria, run_dir
            raise AssertionError("Search should be skipped when debug_target_job_url is set.")

        async def fake_load_job_details(
            context: object,
            listing: LinkedInCollectedJob,
        ) -> LinkedInCollectedJob:
            del context
            return LinkedInCollectedJob(
                external_job_id=listing.external_job_id,
                url=listing.url,
                title="Senior Python Engineer",
                company_name="Acme",
                location="SAO PAULO - SP BRASIL",
                description_raw="Debug target description",
                easy_apply=True,
                metadata_text="Remote | Easy Apply",
            )

        async def fake_capture_debug_target_artifact(
            context: object,
            *,
            target_url: str,
            run_dir: Path,
        ) -> None:
            del context, target_url, run_dir
            return None

        client._open_search = fail_open_search  # type: ignore[method-assign]
        client._load_job_details = fake_load_job_details  # type: ignore[method-assign]
        client._capture_debug_target_artifact = (  # type: ignore[method-assign]
            fake_capture_debug_target_artifact
        )

        jobs = await client._fetch_jobs_once(cast(BrowserContext, FakeContext()), criteria)

        assert len(jobs) == 1
        assert jobs[0].url == "https://www.linkedin.com/jobs/view/1234567890/"
        assert jobs[0].title == "Senior Python Engineer"
        assert jobs[0].easy_apply is True

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
