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
from job_applier.domain import Platform, ScheduleFrequency, SeniorityLevel, WorkplaceType
from job_applier.domain.entities import JobPosting
from job_applier.infrastructure.linkedin import (
    RecruiterCandidate,
    RecruiterMessageGenerator,
    build_recruiter_message_template,
    select_recruiter_candidate,
)


def test_select_recruiter_candidate_prefers_who_posted_section() -> None:
    candidate = select_recruiter_candidate(
        (
            RecruiterCandidate(
                name="Thiago Martins",
                profile_url="https://www.linkedin.com/in/thiago-martins",
                context_label="People also viewed Thiago Martins profile",
            ),
            RecruiterCandidate(
                name="Maria Recruiter",
                profile_url="https://www.linkedin.com/in/maria-recruiter",
                context_label="Who posted this job Maria Recruiter Talent Acquisition",
            ),
        ),
    )

    assert candidate is not None
    assert candidate.name == "Maria Recruiter"


def test_recruiter_message_generator_falls_back_to_template() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()
    recruiter = RecruiterCandidate(
        name="Maria Recruiter",
        profile_url="https://www.linkedin.com/in/maria-recruiter",
    )
    generator = RecruiterMessageGenerator(ai_generator=NullAI())

    message = asyncio.run(
        generator.generate(
            recruiter=recruiter,
            posting=posting,
            settings=settings,
        ),
    )

    assert message == build_recruiter_message_template(
        recruiter_name="Maria Recruiter",
        company_name="Acme",
        job_title="Senior Python Engineer",
        candidate_name="Thiago Martins",
    )
    assert len(message) < 300


def test_recruiter_message_generator_uses_ai_and_enforces_limit() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()
    recruiter = RecruiterCandidate(
        name="Maria Recruiter",
        profile_url="https://www.linkedin.com/in/maria-recruiter",
    )
    generator = RecruiterMessageGenerator(ai_generator=VerboseAI())

    message = asyncio.run(
        generator.generate(
            recruiter=recruiter,
            posting=posting,
            settings=settings,
        ),
    )

    assert message.startswith("Hi Maria")
    assert len(message) <= 300


class NullAI:
    async def generate(
        self,
        *,
        recruiter: RecruiterCandidate,
        posting: JobPosting,
        settings: UserAgentSettings,
    ) -> str | None:
        return None


class VerboseAI:
    async def generate(
        self,
        *,
        recruiter: RecruiterCandidate,
        posting: JobPosting,
        settings: UserAgentSettings,
    ) -> str | None:
        return (
            "Hi Maria, I just applied for the Senior Python Engineer role at Acme and would love "
            "to connect. I have strong backend and automation experience, and I'm excited about "
            "the fit. Happy to stay in touch and share more context if helpful. Thanks, Thiago "
            "Martins. Looking forward to connecting soon."
        )


def build_posting() -> JobPosting:
    return JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/1234567890",
        title="Senior Python Engineer",
        company_name="Acme",
        location="Remote",
        description_raw="Build backend automation systems.",
    )


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
            default_responses={},
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
            auto_connect_with_recruiter=True,
        ),
        ai=AIConfig(api_key=SecretStr("sk-test"), model="o3-mini"),
        ruleset=RulesetConfig(version="ruleset-v1", auto_connect_with_recruiter=True),
    )
