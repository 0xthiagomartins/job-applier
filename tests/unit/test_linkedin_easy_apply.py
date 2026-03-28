import asyncio

import pytest
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
    JobPosting,
    Platform,
    QuestionType,
    ScheduleFrequency,
    SeniorityLevel,
    WorkplaceType,
)
from job_applier.infrastructure.linkedin import (
    EasyApplyField,
    GeneratedAnswer,
    LinkedInAnswerResolver,
    LinkedInQuestionClassifier,
    LinkedInQuestionExtractor,
)


@pytest.mark.parametrize(
    ("question_raw", "control_kind", "input_type", "options", "expected_type", "min_confidence"),
    [
        ("Email address", "text", "email", (), QuestionType.EMAIL, 0.99),
        ("Phone number", "text", "tel", (), QuestionType.PHONE, 0.99),
        ("LinkedIn profile", "text", "url", (), QuestionType.LINKEDIN_URL, 0.98),
        ("GitHub URL", "text", "url", (), QuestionType.GITHUB_URL, 0.98),
        ("Portfolio website", "text", "url", (), QuestionType.PORTFOLIO_URL, 0.9),
        (
            "Are you legally authorized to work in Brazil?",
            "radio",
            "radio",
            ("Yes", "No"),
            QuestionType.WORK_AUTHORIZATION,
            0.98,
        ),
        (
            "Will you require visa sponsorship?",
            "radio",
            "radio",
            ("Yes", "No"),
            QuestionType.VISA_SPONSORSHIP,
            0.98,
        ),
        ("Salary expectation", "text", "number", (), QuestionType.SALARY_EXPECTATION, 0.95),
        ("When can you start?", "text", "text", (), QuestionType.START_DATE, 0.95),
        ("Current city", "text", "text", (), QuestionType.CITY, 0.9),
        (
            "How many years of experience do you have with Python?",
            "text",
            "number",
            (),
            QuestionType.YEARS_EXPERIENCE,
            0.9,
        ),
        ("Resume", "file", "file", (), QuestionType.RESUME_UPLOAD, 0.99),
        ("Cover letter", "textarea", "textarea", (), QuestionType.COVER_LETTER, 0.99),
        (
            "Are you willing to relocate?",
            "radio",
            "radio",
            ("Yes", "No"),
            QuestionType.YES_NO_GENERIC,
            0.75,
        ),
        (
            "Tell us more about yourself",
            "textarea",
            "textarea",
            (),
            QuestionType.FREE_TEXT_GENERIC,
            0.7,
        ),
        ("Favorite color", "text", "text", (), QuestionType.UNKNOWN, 0.25),
    ],
)
def test_question_classifier_recognizes_core_question_types(
    question_raw: str,
    control_kind: str,
    input_type: str,
    options: tuple[str, ...],
    expected_type: QuestionType,
    min_confidence: float,
) -> None:
    classifier = LinkedInQuestionClassifier()

    classification = classifier.classify(
        question_raw=question_raw,
        control_kind=control_kind,  # type: ignore[arg-type]
        input_type=input_type,
        options=options,
    )

    assert classification.question_type is expected_type
    assert classification.confidence >= min_confidence
    assert classification.normalized_key


def test_question_extractor_returns_standardized_field_structure() -> None:
    extractor = LinkedInQuestionExtractor()

    field = extractor.build_field(
        {
            "question_raw": "How many years of experience do you have with Python?",
            "control_kind": "select",
            "input_type": "select",
            "name": "experience_python",
            "dom_id": "experience-python",
            "required": True,
            "prefilled": False,
            "current_value": "",
            "options": ["Select an option", "2", "4", "8"],
        }
    )

    assert field.question_raw == "How many years of experience do you have with Python?"
    assert field.question_type is QuestionType.YEARS_EXPERIENCE
    assert field.control_kind == "select"
    assert field.required is True
    assert field.options == ("Select an option", "2", "4", "8")
    assert field.classification_confidence >= 0.9
    assert field.classification_rule == "years_experience"
    assert field.normalized_key == "how_many_years_of_experience_do_you_have_with_python"


def test_answer_resolver_respects_priority_chain_and_prefilled_fields() -> None:
    settings = build_user_agent_settings()
    resolver = LinkedInAnswerResolver()
    posting = build_posting()

    work_auth_field = EasyApplyField(
        question_raw="Are you legally authorized to work in Brazil?",
        normalized_key="work_authorization",
        question_type=QuestionType.WORK_AUTHORIZATION,
        control_kind="radio",
        classification_confidence=0.98,
        options=("Yes", "No"),
    )
    email_field = EasyApplyField(
        question_raw="Email address",
        normalized_key="email",
        question_type=QuestionType.EMAIL,
        control_kind="text",
        input_type="email",
        classification_confidence=0.99,
    )
    cover_letter_field = EasyApplyField(
        question_raw="Cover letter",
        normalized_key="cover_letter",
        question_type=QuestionType.COVER_LETTER,
        control_kind="textarea",
        classification_confidence=0.99,
    )
    prefilled_city = EasyApplyField(
        question_raw="City",
        normalized_key="city",
        question_type=QuestionType.CITY,
        control_kind="text",
        prefilled=True,
        current_value="Sao Paulo",
    )

    work_auth_answer = asyncio.run(resolver.resolve(work_auth_field, settings, posting=posting))
    email_answer = asyncio.run(resolver.resolve(email_field, settings, posting=posting))
    cover_letter_answer = asyncio.run(
        resolver.resolve(cover_letter_field, settings, posting=posting)
    )

    assert work_auth_answer is not None
    assert work_auth_answer.value == "Yes"
    assert work_auth_answer.answer_source is AnswerSource.RULE
    assert work_auth_answer.fill_strategy is FillStrategy.DETERMINISTIC

    assert email_answer is not None
    assert email_answer.value == "thiago@example.com"
    assert email_answer.answer_source is AnswerSource.PROFILE_SNAPSHOT
    assert email_answer.fill_strategy is FillStrategy.DETERMINISTIC

    assert cover_letter_answer is not None
    assert cover_letter_answer.value == "Open to discuss the role."
    assert cover_letter_answer.answer_source is AnswerSource.DEFAULT_RESPONSE
    assert cover_letter_answer.fill_strategy is FillStrategy.DETERMINISTIC

    assert asyncio.run(resolver.resolve(prefilled_city, settings, posting=posting)) is None


def test_answer_resolver_uses_ai_for_ambiguous_questions() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=SuccessfulGenerator(),
    )
    ambiguous_field = EasyApplyField(
        question_raw="Are you willing to relocate?",
        normalized_key="are_you_willing_to_relocate",
        question_type=QuestionType.YES_NO_GENERIC,
        control_kind="radio",
        input_type="radio",
        options=("Yes", "No"),
    )

    answer = asyncio.run(resolver.resolve(ambiguous_field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "No"
    assert answer.answer_source is AnswerSource.AI
    assert answer.fill_strategy is FillStrategy.AUTOFILL_AI
    assert answer.ambiguity_flag is True
    assert answer.reasoning == "matched user preference"


def test_answer_resolver_falls_back_gracefully_when_ai_fails() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=FailingGenerator(),
    )
    ambiguous_field = EasyApplyField(
        question_raw="Are you willing to relocate?",
        normalized_key="are_you_willing_to_relocate",
        question_type=QuestionType.YES_NO_GENERIC,
        control_kind="radio",
        input_type="radio",
        options=("Yes", "No"),
    )

    answer = asyncio.run(resolver.resolve(ambiguous_field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "Yes"
    assert answer.answer_source is AnswerSource.BEST_EFFORT_AUTOFILL
    assert answer.fill_strategy is FillStrategy.BEST_EFFORT
    assert answer.ambiguity_flag is True
    assert answer.reasoning == "heuristic_fallback"


class SuccessfulGenerator:
    async def generate(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> GeneratedAnswer | None:
        return GeneratedAnswer(
            value="No",
            confidence=0.64,
            reasoning="matched user preference",
        )


class FailingGenerator:
    async def generate(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> GeneratedAnswer | None:
        raise RuntimeError("temporary OpenAI outage")


def build_posting() -> JobPosting:
    return JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/1234567890",
        title="Senior Python Engineer",
        company_name="Acme",
        location="Remote - Brazil",
        description_raw="Build backend automation and AI-assisted workflows.",
        easy_apply=True,
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
            years_experience_by_stack={"python": 8, "fastapi": 4},
            work_authorized=True,
            availability="Immediate",
            default_responses={
                "cover_letter": "Open to discuss the role.",
                "email": "should-not-win@example.com",
                "work_authorization": "No",
            },
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
