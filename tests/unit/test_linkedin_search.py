import asyncio
from pathlib import Path

from pydantic import AnyUrl, SecretStr, TypeAdapter
from tests.integration.sqlite_helpers import upgrade_to_head

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
from job_applier.infrastructure.linkedin.search import (
    LinkedInCollectedJob,
    LinkedInJobFetcher,
    LinkedInJobParser,
    LinkedInSearchCriteria,
    build_paginated_search_url,
    build_search_criteria,
    infer_seniority,
    infer_workplace_type,
)
from job_applier.infrastructure.sqlite import (
    SqliteJobPostingRepository,
    create_session_factory,
)
from job_applier.settings import RuntimeSettings


def test_search_criteria_and_parser_normalize_linkedin_jobs(tmp_path: Path) -> None:
    runtime_settings = RuntimeSettings(data_dir=tmp_path, linkedin_max_search_pages=3)
    settings = build_user_agent_settings()

    criteria = build_search_criteria(settings, runtime_settings)
    paginated = build_paginated_search_url(
        "https://www.linkedin.com/jobs/search/?keywords=python&location=Remote",
        page_index=2,
    )

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
