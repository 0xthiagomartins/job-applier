import asyncio

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
from job_applier.application.job_scoring import RuleBasedJobScorer
from job_applier.domain import (
    JobPosting,
    Platform,
    ScheduleFrequency,
    SeniorityLevel,
    WorkplaceType,
)


def test_rule_based_scorer_accepts_high_match_vacancy() -> None:
    scorer = RuleBasedJobScorer()
    settings = build_settings()
    posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/1001",
        title="Senior Python Automation Engineer",
        company_name="Acme",
        location="Remote",
        workplace_type=WorkplaceType.REMOTE,
        seniority=SeniorityLevel.SENIOR,
        description_raw="Build FastAPI services and browser automation in Python.",
    )

    scored = asyncio.run(scorer.score(settings, posting))

    assert scored.selected is True
    assert scored.score is not None
    assert scored.score >= settings.search.minimum_score_threshold


def test_rule_based_scorer_rejects_low_match_below_threshold() -> None:
    scorer = RuleBasedJobScorer()
    settings = build_settings(threshold=0.7)
    posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/1002",
        title="Operations Coordinator",
        company_name="Acme",
        location="Remote",
        workplace_type=WorkplaceType.REMOTE,
        seniority=SeniorityLevel.SENIOR,
        description_raw="Coordinate administrative routines and internal meetings.",
    )

    scored = asyncio.run(scorer.score(settings, posting))

    assert scored.selected is False
    assert scored.score is not None
    assert scored.score < settings.search.minimum_score_threshold
    assert scored.reason is not None
    assert "Rejected with score" in scored.reason


def test_rule_based_scorer_rejects_blacklist_matches() -> None:
    scorer = RuleBasedJobScorer()
    settings = build_settings(negative_keywords=("internship",))
    posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/1003",
        title="Python Internship",
        company_name="Acme",
        location="Remote",
        workplace_type=WorkplaceType.REMOTE,
        seniority=SeniorityLevel.JUNIOR,
        description_raw="Python internship focused on support activities.",
    )

    scored = asyncio.run(scorer.score(settings, posting))

    assert scored.selected is False
    assert scored.score == 0.0
    assert scored.reason is not None
    assert "blacklist" in scored.reason.lower()


def test_positive_keywords_increase_the_score() -> None:
    scorer = RuleBasedJobScorer()
    base_settings = build_settings(positive_keywords=())
    boosted_settings = build_settings(positive_keywords=("fastapi",))
    posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/1004",
        title="Senior Python Engineer",
        company_name="Acme",
        location="Remote",
        workplace_type=WorkplaceType.REMOTE,
        seniority=SeniorityLevel.SENIOR,
        description_raw="Build APIs with FastAPI and Python.",
    )

    base_score = asyncio.run(scorer.score(base_settings, posting))
    boosted_score = asyncio.run(scorer.score(boosted_settings, posting))

    assert base_score.score is not None
    assert boosted_score.score is not None
    assert boosted_score.score > base_score.score


def build_settings(
    *,
    threshold: float = 0.55,
    positive_keywords: tuple[str, ...] = ("fastapi",),
    negative_keywords: tuple[str, ...] = ("internship",),
) -> UserAgentSettings:
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
            years_experience_by_stack={"python": 8, "fastapi": 4, "automation": 6},
            work_authorized=True,
            availability="Immediate",
            default_responses={"work_authorization": "Yes"},
            positive_filters=positive_keywords,
            blacklist=negative_keywords,
        ),
        search=SearchConfig(
            keywords=("python", "automation"),
            location="Remote",
            posted_within_hours=24,
            workplace_types=(WorkplaceType.REMOTE,),
            seniority=(SeniorityLevel.SENIOR,),
            easy_apply_only=True,
            minimum_score_threshold=threshold,
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
