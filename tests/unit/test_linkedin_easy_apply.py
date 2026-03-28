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
from job_applier.domain import (
    AnswerSource,
    FillStrategy,
    QuestionType,
    ScheduleFrequency,
    SeniorityLevel,
    WorkplaceType,
)
from job_applier.infrastructure.linkedin import (
    EasyApplyField,
    LinkedInAnswerResolver,
)


def test_answer_resolver_prefers_known_profile_values_and_best_effort() -> None:
    settings = build_user_agent_settings()
    resolver = LinkedInAnswerResolver()

    email_field = EasyApplyField(
        question_raw="Email address",
        normalized_key="email",
        question_type=QuestionType.EMAIL,
        control_kind="text",
        input_type="email",
    )
    experience_field = EasyApplyField(
        question_raw="How many years of experience do you have with Python?",
        normalized_key="how_many_years_of_experience_do_you_have_with_python",
        question_type=QuestionType.YEARS_EXPERIENCE,
        control_kind="text",
        input_type="number",
    )
    ambiguous_radio = EasyApplyField(
        question_raw="Are you willing to relocate?",
        normalized_key="are_you_willing_to_relocate",
        question_type=QuestionType.YES_NO_GENERIC,
        control_kind="radio",
        input_type="radio",
        options=("Yes", "No"),
    )
    prefilled_city = EasyApplyField(
        question_raw="City",
        normalized_key="city",
        question_type=QuestionType.CITY,
        control_kind="text",
        prefilled=True,
        current_value="Sao Paulo",
    )

    email_answer = resolver.resolve(email_field, settings)
    experience_answer = resolver.resolve(experience_field, settings)
    ambiguous_answer = resolver.resolve(ambiguous_radio, settings)

    assert email_answer is not None
    assert email_answer.value == "thiago@example.com"
    assert email_answer.answer_source is AnswerSource.PROFILE_SNAPSHOT
    assert email_answer.fill_strategy is FillStrategy.DETERMINISTIC

    assert experience_answer is not None
    assert experience_answer.value == "8"

    assert ambiguous_answer is not None
    assert ambiguous_answer.value == "Yes"
    assert ambiguous_answer.answer_source is AnswerSource.BEST_EFFORT_AUTOFILL
    assert ambiguous_answer.fill_strategy is FillStrategy.BEST_EFFORT
    assert ambiguous_answer.ambiguity_flag is True

    assert resolver.resolve(prefilled_city, settings) is None


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
            years_experience_by_stack={"python": 8, "fastapi": 4},
            work_authorized=True,
            availability="Immediate",
            default_responses={"cover_letter": "Open to discuss the role."},
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
