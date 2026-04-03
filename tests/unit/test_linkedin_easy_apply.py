import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from playwright.async_api import Locator, Page, async_playwright
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
from job_applier.domain import (
    AnswerSource,
    ApplicationAnswer,
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
    ResolvedFieldValue,
)
from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAgentAction,
    OpenAIResponsesBrowserAgent,
)
from job_applier.infrastructure.linkedin.easy_apply import (
    ControlValidationState,
    EasyApplyStep,
    LinkedInEasyApplyError,
    PlaywrightLinkedInEasyApplyExecutor,
    TextFieldInteractionState,
    _pick_option_index,
    _pick_resume_option_index,
    _step_surface_changed,
)
from job_applier.infrastructure.linkedin.question_resolution import (
    field_has_meaningful_current_value,
)
from job_applier.settings import RuntimeSettings


@pytest.mark.parametrize(
    ("question_raw", "control_kind", "input_type", "options", "expected_type", "min_confidence"),
    [
        ("Email address", "text", "email", (), QuestionType.EMAIL, 0.99),
        ("Phone number", "text", "tel", (), QuestionType.PHONE, 0.99),
        ("First name", "text", "text", (), QuestionType.FIRST_NAME, 0.98),
        ("Last name", "text", "text", (), QuestionType.LAST_NAME, 0.98),
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
        ("Número de celular", "text", "tel", (), QuestionType.PHONE, 0.99),
        (
            "Código do país",
            "select",
            "select",
            ("Selecione uma opção", "Brasil +55"),
            QuestionType.PHONE,
            0.95,
        ),
        ("Carregue o currículo", "file", "file", (), QuestionType.RESUME_UPLOAD, 0.99),
        (
            "Você possui experiência sólida com Python em nível avançado?",
            "select",
            "select",
            ("Selecionar opção", "Sim", "Não"),
            QuestionType.YES_NO_GENERIC,
            0.9,
        ),
        (
            "Qual sua pretensão salarial para atuar como PJ?",
            "text",
            "number",
            (),
            QuestionType.SALARY_EXPECTATION,
            0.95,
        ),
        (
            "How many years of experience do you have with Python?",
            "text",
            "number",
            (),
            QuestionType.YEARS_EXPERIENCE,
            0.9,
        ),
        ("Resume", "file", "file", (), QuestionType.RESUME_UPLOAD, 0.99),
        (
            "Select resume",
            "radio",
            "radio",
            ("Thiago Martins - CV 2026.pdf", "Thiago Martins - CV 2024.pdf"),
            QuestionType.RESUME_UPLOAD,
            0.9,
        ),
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
            "Are you an expert with python software development?",
            "select",
            "select",
            ("Select an option", "Yes", "No"),
            QuestionType.YES_NO_GENERIC,
            0.9,
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
            "dom_ref": "job-applier-3",
            "name": "experience_python",
            "dom_id": "experience-python",
            "required": True,
            "prefilled": False,
            "current_value": "",
            "options": ["Select an option", "2", "4", "8"],
            "option_refs": ["index:0", "value:two", "value:four", "value:eight"],
        }
    )

    assert field.question_raw == "How many years of experience do you have with Python?"
    assert field.question_type is QuestionType.YEARS_EXPERIENCE
    assert field.control_kind == "select"
    assert field.dom_ref == "job-applier-3"
    assert field.required is True
    assert field.options == ("Select an option", "2", "4", "8")
    assert field.option_refs == ("index:0", "value:two", "value:four", "value:eight")
    assert field.classification_confidence >= 0.9
    assert field.classification_rule == "years_experience"
    assert field.normalized_key == "how_many_years_of_experience_do_you_have_with_python"


def test_question_classifier_assigns_phone_country_code_key_for_portuguese_label() -> None:
    classifier = LinkedInQuestionClassifier()

    classification = classifier.classify(
        question_raw="Código do país",
        control_kind="select",
        input_type="select",
        options=("Selecione uma opção", "Brasil +55"),
    )

    assert classification.question_type is QuestionType.PHONE
    assert classification.normalized_key == "phone_country_code"
    assert classification.matched_rule == "phone_country_code"


def test_question_classifier_recognizes_binary_language_comfort_question() -> None:
    classifier = LinkedInQuestionClassifier()

    classification = classifier.classify(
        question_raw="Você se sente confortável trabalhando em um ambiente onde se fala inglês?",
        control_kind="text",
        input_type="text",
        options=(),
    )

    assert classification.question_type is QuestionType.YES_NO_GENERIC
    assert classification.matched_rule == "yes_no_generic"


def test_pick_option_index_skips_placeholder_entries() -> None:
    options = ("Select an option", "Yes", "No")

    assert _pick_option_index(options, preferred="Yes") == 1
    assert _pick_option_index(options, preferred="No") == 2
    assert _pick_option_index(options, preferred=None) == 1


def test_pick_option_index_matches_localized_yes_no_options() -> None:
    options = ("Selecionar opção", "Sim", "Não")

    assert _pick_option_index(options, preferred="Yes") == 1
    assert _pick_option_index(options, preferred="No") == 2


def test_prefilled_placeholder_select_is_not_treated_as_preserved() -> None:
    field = EasyApplyField(
        question_raw="English proficiency",
        normalized_key="english_proficiency",
        question_type=QuestionType.UNKNOWN,
        control_kind="select",
        input_type="select",
        required=True,
        prefilled=True,
        current_value="Select an option",
        options=("Select an option", "Basic", "Advanced"),
    )

    assert field_has_meaningful_current_value(field) is False


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
    first_name_field = EasyApplyField(
        question_raw="First name",
        normalized_key="first_name",
        question_type=QuestionType.FIRST_NAME,
        control_kind="text",
        classification_confidence=0.98,
    )
    last_name_field = EasyApplyField(
        question_raw="Last name",
        normalized_key="last_name",
        question_type=QuestionType.LAST_NAME,
        control_kind="text",
        classification_confidence=0.98,
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
    first_name_answer = asyncio.run(resolver.resolve(first_name_field, settings, posting=posting))
    last_name_answer = asyncio.run(resolver.resolve(last_name_field, settings, posting=posting))
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

    assert first_name_answer is not None
    assert first_name_answer.value == "Thiago"
    assert first_name_answer.answer_source is AnswerSource.PROFILE_SNAPSHOT

    assert last_name_answer is not None
    assert last_name_answer.value == "Martins"
    assert last_name_answer.answer_source is AnswerSource.PROFILE_SNAPSHOT

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


def test_answer_resolver_uses_binary_guardrail_for_language_comfort_question() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=NoopGenerator(),
    )
    field = EasyApplyField(
        question_raw="Você se sente confortável trabalhando em um ambiente onde se fala inglês?",
        normalized_key="english_comfort",
        question_type=QuestionType.YES_NO_GENERIC,
        control_kind="text",
        input_type="text",
    )

    answer = asyncio.run(resolver.resolve(field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "Yes"
    assert answer.reasoning == "typed_guardrail_binary"


def test_answer_resolver_rejects_target_company_as_current_employer_answer() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()

    class TargetCompanyGenerator:
        async def generate(
            self,
            *,
            field: EasyApplyField,
            settings: UserAgentSettings,
            posting: JobPosting,
        ) -> GeneratedAnswer | None:
            del field, settings, posting
            return GeneratedAnswer(
                value="Acme",
                confidence=0.74,
                reasoning="guessed_from_target_company",
            )

    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=TargetCompanyGenerator(),
    )
    field = EasyApplyField(
        question_raw="Confirm the name of the company where you work",
        normalized_key="confirm_current_company_name",
        question_type=QuestionType.FREE_TEXT_GENERIC,
        control_kind="text",
        input_type="text",
    )

    answer = asyncio.run(resolver.resolve(field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "Freelancer"
    assert answer.answer_source is AnswerSource.BEST_EFFORT_AUTOFILL
    assert answer.reasoning == "guardrail_unknown_current_employer"


def test_answer_resolver_uses_conservative_guardrail_for_language_proficiency_options() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=NoopGenerator(),
    )
    field = EasyApplyField(
        question_raw="English proficiency",
        normalized_key="english_proficiency",
        question_type=QuestionType.UNKNOWN,
        control_kind="select",
        input_type="select",
        options=("Select an option", "Beginner", "Intermediate II", "Advanced"),
    )

    answer = asyncio.run(resolver.resolve(field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "Intermediate II"
    assert answer.answer_source is AnswerSource.BEST_EFFORT_AUTOFILL
    assert answer.reasoning == "guardrail_conservative_language_proficiency"


def test_answer_resolver_uses_opt_out_for_sensitive_questions_when_available() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=NoopGenerator(),
    )
    field = EasyApplyField(
        question_raw="Gender identity",
        normalized_key="gender_identity",
        question_type=QuestionType.UNKNOWN,
        control_kind="select",
        input_type="select",
        options=("Select an option", "Woman", "Man", "Prefer not to say"),
    )

    answer = asyncio.run(resolver.resolve(field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "Prefer not to say"
    assert answer.answer_source is AnswerSource.BEST_EFFORT_AUTOFILL
    assert answer.fill_strategy is FillStrategy.BEST_EFFORT
    assert answer.reasoning == "sensitive_question_opt_out"


def test_answer_resolver_leaves_sensitive_questions_unanswered_without_opt_out() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=SuccessfulGenerator(),
    )
    field = EasyApplyField(
        question_raw="Do you identify as a person with disability?",
        normalized_key="person_with_disability",
        question_type=QuestionType.UNKNOWN,
        control_kind="radio",
        input_type="radio",
        options=("Yes", "No"),
    )

    answer = asyncio.run(resolver.resolve(field, settings, posting=posting))

    assert answer is None


def test_answer_resolver_resolves_phone_country_code_for_brazilian_profile() -> None:
    settings = build_user_agent_settings()
    posting = build_posting()
    resolver = LinkedInAnswerResolver()
    field = EasyApplyField(
        question_raw="Código do país",
        normalized_key="phone_country_code",
        question_type=QuestionType.PHONE,
        control_kind="select",
        options=("Selecionar opção", "Estados Unidos +1", "Brasil +55"),
    )

    answer = asyncio.run(resolver.resolve(field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "Brasil +55"
    assert answer.answer_source is AnswerSource.PROFILE_SNAPSHOT


def test_answer_resolver_uses_matched_stack_overlap_for_years_experience() -> None:
    settings = build_user_agent_settings().model_copy(
        update={
            "profile": build_user_agent_settings().profile.model_copy(
                update={"years_experience_by_stack": {"python": 8, "sql": 5, "fastapi": 4}}
            )
        }
    )
    posting = build_posting()
    resolver = LinkedInAnswerResolver()
    years_field = EasyApplyField(
        question_raw=(
            "How many years of experience do you have working with Python and SQL in "
            "analytics or BI environments?"
        ),
        normalized_key=(
            "how_many_years_of_experience_do_you_have_working_with_python_and_sql_in_"
            "analytics_or_bi_environments"
        ),
        question_type=QuestionType.YEARS_EXPERIENCE,
        control_kind="text",
        input_type="text",
    )

    answer = asyncio.run(resolver.resolve(years_field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "5"
    assert answer.answer_source is AnswerSource.PROFILE_SNAPSHOT
    assert answer.fill_strategy is FillStrategy.DETERMINISTIC


def test_answer_resolver_uses_ai_for_years_experience_when_profile_data_is_missing() -> None:
    base_settings = build_user_agent_settings()
    settings = base_settings.model_copy(
        update={
            "profile": base_settings.profile.model_copy(update={"years_experience_by_stack": {}})
        }
    )
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=YearsExperienceGenerator(),
    )
    years_field = EasyApplyField(
        question_raw="How many years of experience do you have with Python?",
        normalized_key="how_many_years_of_experience_do_you_have_with_python",
        question_type=QuestionType.YEARS_EXPERIENCE,
        control_kind="text",
        input_type="text",
    )

    answer = asyncio.run(resolver.resolve(years_field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "6"
    assert answer.answer_source is AnswerSource.AI
    assert answer.fill_strategy is FillStrategy.AUTOFILL_AI
    assert answer.ambiguity_flag is True


def test_answer_resolver_avoids_generic_yes_fallback_for_years_experience() -> None:
    base_settings = build_user_agent_settings()
    settings = base_settings.model_copy(
        update={
            "profile": base_settings.profile.model_copy(update={"years_experience_by_stack": {}})
        }
    )
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=NoopGenerator(),
    )
    years_field = EasyApplyField(
        question_raw="How many years of experience do you have with Python?",
        normalized_key="how_many_years_of_experience_do_you_have_with_python",
        question_type=QuestionType.YEARS_EXPERIENCE,
        control_kind="text",
        input_type="text",
    )

    answer = asyncio.run(resolver.resolve(years_field, settings, posting=posting))

    assert answer is None


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
    assert answer.reasoning == "typed_guardrail_option_pick"


def test_answer_resolver_uses_plausible_profile_inference_for_related_years() -> None:
    settings = build_user_agent_settings().model_copy(
        update={
            "profile": build_user_agent_settings().profile.model_copy(
                update={"years_experience_by_stack": {"python": 8, "sql": 5}}
            )
        }
    )
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=NoopGenerator(),
    )
    field = EasyApplyField(
        question_raw="How many years of experience do you have with LangChain?",
        normalized_key="how_many_years_of_experience_do_you_have_with_langchain",
        question_type=QuestionType.YEARS_EXPERIENCE,
        control_kind="text",
        input_type="number",
    )

    answer = asyncio.run(resolver.resolve(field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "2"
    assert answer.answer_source is AnswerSource.BEST_EFFORT_AUTOFILL
    assert answer.fill_strategy is FillStrategy.BEST_EFFORT
    assert answer.reasoning == "plausible_profile_inference"


def test_answer_resolver_reinterprets_invalid_numeric_feedback_for_ambiguous_experience_field() -> (
    None
):
    settings = build_user_agent_settings().model_copy(
        update={
            "profile": build_user_agent_settings().profile.model_copy(
                update={"years_experience_by_stack": {"python": 8}}
            )
        }
    )
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=NoopGenerator(),
    )
    field = EasyApplyField(
        question_raw="Do You have experience in robot framework?",
        normalized_key="do_you_have_experience_in_robot_framework",
        question_type=QuestionType.YES_NO_GENERIC,
        control_kind="text",
        input_type="text",
    )

    answer = asyncio.run(
        resolver.resolve_with_validation_feedback(
            field,
            settings,
            posting=posting,
            validation_message="Enter a decimal number larger than 0.0",
            current_value="Yes, I have worked with robot framework in automation projects.",
            previous_answer="Yes, I have worked with robot framework in automation projects.",
        )
    )

    assert answer is not None
    assert answer.value == "2"
    assert answer.reasoning == "plausible_profile_inference"


def test_answer_resolver_uses_city_lookup_query_for_invalid_location_combobox() -> None:
    settings = build_user_agent_settings().model_copy(
        update={
            "profile": build_user_agent_settings().profile.model_copy(
                update={"city": "SAO PAULO - SP BRASIL"}
            )
        }
    )
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=NoopGenerator(),
    )
    field = EasyApplyField(
        question_raw="Location (city)",
        normalized_key="city",
        question_type=QuestionType.CITY,
        control_kind="text",
        input_type="text",
    )

    answer = asyncio.run(
        resolver.resolve_with_validation_feedback(
            field,
            settings,
            posting=posting,
            validation_message="Please enter a valid answer",
            current_value="SAO PAULO - SP BRASIL",
            previous_answer="SAO PAULO - SP BRASIL",
        )
    )

    assert answer is not None
    assert answer.value == "Sao Paulo"
    assert answer.reasoning == "city_lookup_query"


def test_answer_resolver_maps_inferred_years_to_numeric_option() -> None:
    settings = build_user_agent_settings().model_copy(
        update={
            "profile": build_user_agent_settings().profile.model_copy(
                update={"years_experience_by_stack": {"python": 8}}
            )
        }
    )
    posting = build_posting()
    resolver = LinkedInAnswerResolver(
        ambiguous_answer_generator=NoopGenerator(),
    )
    field = EasyApplyField(
        question_raw="How many years of experience do you have with LangChain?",
        normalized_key="how_many_years_of_experience_do_you_have_with_langchain",
        question_type=QuestionType.YEARS_EXPERIENCE,
        control_kind="select",
        input_type="select",
        options=("Select an option", "0-1", "2-3", "4+"),
    )

    answer = asyncio.run(resolver.resolve(field, settings, posting=posting))

    assert answer is not None
    assert answer.value == "2-3"
    assert answer.reasoning == "plausible_profile_inference"


def test_text_field_interaction_requires_follow_up_when_field_is_invalid() -> None:
    executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
    state = TextFieldInteractionState(
        current_value="Sao Paulo",
        focused=True,
        role="combobox",
        aria_autocomplete="list",
        aria_expanded=False,
        has_popup_binding=False,
        active_descendant=None,
        visible_option_count=0,
        invalid=True,
        validation_message="Please enter a valid answer",
    )

    assert state.needs_agentic_follow_up is True
    assert executor._text_field_interaction_complete(state) is False


def test_text_field_interaction_completes_when_value_is_accepted() -> None:
    executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
    state = TextFieldInteractionState(
        current_value="Sao Paulo, Sao Paulo, Brazil",
        focused=False,
        role=None,
        aria_autocomplete=None,
        aria_expanded=False,
        has_popup_binding=False,
        active_descendant=None,
        visible_option_count=0,
        invalid=False,
        validation_message=None,
    )

    assert state.needs_agentic_follow_up is False
    assert executor._text_field_interaction_complete(state) is True


def test_inspect_text_field_interaction_detects_nearby_combobox_options() -> None:
    async def scenario() -> None:
        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                await page.set_content(
                    """
                    <style>
                      body {
                        margin: 0;
                        font-family: sans-serif;
                      }
                      .dialog {
                        position: fixed;
                        inset: 48px auto auto 120px;
                        width: 520px;
                        background: white;
                        border: 1px solid #ddd;
                        padding: 24px;
                      }
                      .options {
                        margin-top: 12px;
                        border: 1px solid #ccc;
                        background: white;
                      }
                      .option {
                        padding: 10px 12px;
                      }
                    </style>
                    <div class="dialog" role="dialog" aria-label="Apply to Example Corp">
                      <label for="city">Location (city)</label>
                      <input
                        id="city"
                        type="text"
                        role="combobox"
                        aria-autocomplete="list"
                        value="Sao Paulo"
                      />
                      <div class="options">
                        <div class="option" role="option">São Paulo, Brazil</div>
                        <div class="option" role="option">São Paulo, São Paulo, Brazil</div>
                      </div>
                    </div>
                    """
                )

                state = await executor._inspect_text_field_interaction(page.locator("#city"))

                assert state.visible_option_count == 2
                assert state.visible_option_texts == (
                    "São Paulo, Brazil",
                    "São Paulo, São Paulo, Brazil",
                )
                assert state.needs_agentic_follow_up is True
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_inspect_text_field_interaction_raises_domain_error_on_locator_timeout() -> None:
    async def scenario() -> None:
        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)

        class SlowLocator:
            async def evaluate(
                self,
                expression: str,
                timeout: int | None = None,
            ) -> dict[str, object]:
                del expression, timeout
                raise PlaywrightTimeoutError("locator inspection timed out")

        with pytest.raises(LinkedInEasyApplyError, match="Timed out while inspecting"):
            await executor._inspect_text_field_interaction(cast(Locator, SlowLocator()))

    asyncio.run(scenario())


def test_complete_text_field_interaction_reports_visible_options_to_agent() -> None:
    async def scenario() -> None:
        settings = build_user_agent_settings()
        field = EasyApplyField(
            question_raw="Location (city) Location (city)",
            normalized_key="city",
            question_type=QuestionType.CITY,
            control_kind="text",
            input_type="text",
            required=True,
        )
        fake_root = object()
        fake_locator = object()
        captured_extra_rules: tuple[str, ...] = ()
        states = iter(
            (
                TextFieldInteractionState(
                    current_value="Sao Paulo",
                    focused=False,
                    role="combobox",
                    aria_autocomplete="list",
                    aria_expanded=True,
                    has_popup_binding=False,
                    active_descendant=None,
                    visible_option_count=2,
                    visible_option_texts=(
                        "São Paulo, Brazil",
                        "São Paulo, São Paulo, Brazil",
                    ),
                    invalid=True,
                    validation_message="Please enter a valid answer",
                ),
                TextFieldInteractionState(
                    current_value="São Paulo, Brazil",
                    focused=False,
                    role="combobox",
                    aria_autocomplete="list",
                    aria_expanded=False,
                    has_popup_binding=False,
                    active_descendant=None,
                    visible_option_count=0,
                    visible_option_texts=(),
                    invalid=False,
                    validation_message=None,
                ),
            )
        )

        class FakeBrowserAgent:
            async def perform_single_task_action(self, **kwargs):
                nonlocal captured_extra_rules
                captured_extra_rules = kwargs["extra_rules"]
                return BrowserAgentAction(
                    action_type="click",
                    element_id="agent-2",
                    value_source=None,
                    value=None,
                    action_intent="select_dropdown_option",
                    key_name=None,
                    scroll_target=None,
                    scroll_direction=None,
                    scroll_amount=120,
                    wait_seconds=0,
                    reasoning="Click the visible matching option.",
                )

        class ExecutorDouble(PlaywrightLinkedInEasyApplyExecutor):
            def __init__(self) -> None:
                pass

            def _create_browser_agent(
                self,
                settings: UserAgentSettings,
            ) -> OpenAIResponsesBrowserAgent:
                del settings
                return cast(OpenAIResponsesBrowserAgent, FakeBrowserAgent())

            async def _easy_apply_root(self, page: Page) -> Locator:
                del page
                return cast(Locator, fake_root)

            async def _find_control_locator(
                self,
                root: Locator,
                easy_apply_field: EasyApplyField,
            ) -> Locator | None:
                del root, easy_apply_field
                return cast(Locator, fake_locator)

            async def _field_interaction_focus_locator(
                self,
                root: Locator,
                easy_apply_field: EasyApplyField,
            ) -> Locator | None:
                del root, easy_apply_field
                return None

            async def _inspect_text_field_interaction(
                self,
                locator: Locator,
            ) -> TextFieldInteractionState:
                del locator
                return next(states)

        executor = ExecutorDouble()

        result = await executor._complete_text_field_interaction(
            page=cast(Page, object()),
            field=field,
            target_value="Sao Paulo",
            settings=settings,
        )

        assert result == "São Paulo, Brazil"
        assert any(
            "Visible chooser options" in rule and "São Paulo, Brazil" in rule
            for rule in captured_extra_rules
        )
        assert any(
            "prefer clicking the best matching visible option" in rule
            for rule in captured_extra_rules
        )

    asyncio.run(scenario())


def test_build_easy_apply_remediation_values_uses_descriptive_sources() -> None:
    executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
    settings = build_user_agent_settings()
    posting = build_posting()
    answers = (
        ApplicationAnswer(
            submission_id=uuid4(),
            step_index=0,
            question_raw="City",
            question_type=QuestionType.CITY,
            normalized_key="city",
            answer_raw="Sao Paulo",
            answer_source=AnswerSource.PROFILE_SNAPSHOT,
            fill_strategy=FillStrategy.DETERMINISTIC,
        ),
        ApplicationAnswer(
            submission_id=uuid4(),
            step_index=0,
            question_raw="Email address",
            question_type=QuestionType.EMAIL,
            normalized_key="email",
            answer_raw="thiago@example.com",
            answer_source=AnswerSource.PROFILE_SNAPSHOT,
            fill_strategy=FillStrategy.DETERMINISTIC,
        ),
    )

    values = executor._build_easy_apply_remediation_values(
        settings=settings,
        posting=posting,
        step_answers=answers,
    )

    assert values["field_value_city"] == "Sao Paulo"
    assert values["field_value_email"] == "thiago@example.com"
    assert values["profile_full_name"] == "Thiago Martins"
    assert values["profile_first_name"] == "Thiago"
    assert values["profile_last_name"] == "Martins"
    assert values["profile_city"] == "Sao Paulo"
    assert values["profile_phone"] == "+5511999999999"
    assert values["job_title"] == "Senior Python Engineer"
    assert values["job_company_name"] == "Acme"


def test_pick_resume_option_index_prefers_semantic_match_for_configured_cv() -> None:
    options = (
        "Thiago Martins - CV 2024.pdf",
        "Thiago Martins CV 2026 - Brazil",
        "Generic Resume",
    )

    best_index = _pick_resume_option_index(options, "Thiago Martins - CV 2026.pdf")

    assert best_index == 1


def test_retry_invalid_fields_uses_fresh_answer_resolution_for_remediation() -> None:
    async def scenario() -> None:
        settings = build_user_agent_settings()
        posting = build_posting()
        field = EasyApplyField(
            question_raw="How many years of experience do you have with LangChain?",
            normalized_key="how_many_years_of_experience_do_you_have_with_langchain",
            question_type=QuestionType.YEARS_EXPERIENCE,
            control_kind="text",
            input_type="number",
            required=True,
        )
        step = EasyApplyStep(step_index=0, total_steps=1, fields=(field,))
        captured: dict[str, str] = {}

        class FakeResolver:
            async def resolve_with_validation_feedback(
                self,
                field: EasyApplyField,
                settings: UserAgentSettings,
                *,
                posting: JobPosting,
                validation_message: str | None,
                current_value: str = "",
                previous_answer: str | None = None,
            ) -> ResolvedFieldValue | None:
                del field, settings, posting, validation_message, current_value, previous_answer
                return ResolvedFieldValue(
                    value="2",
                    answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
                    fill_strategy=FillStrategy.BEST_EFFORT,
                    ambiguity_flag=True,
                    confidence=0.42,
                    reasoning="plausible_profile_inference",
                )

        class ExecutorDouble(PlaywrightLinkedInEasyApplyExecutor):
            def __init__(self) -> None:
                pass

            async def _easy_apply_modal_visible(self, page: Page) -> bool:
                del page
                return True

            async def _extract_step(
                self,
                page: Page,
                *,
                last_known_step_index: int,
                last_known_total_steps: int,
            ) -> EasyApplyStep:
                del page, last_known_step_index, last_known_total_steps
                return step

            async def _easy_apply_root(self, page: Page) -> Locator:
                del page
                return cast(Locator, object())

            async def _find_control_locator(
                self,
                root: Locator,
                easy_apply_field: EasyApplyField,
            ) -> Locator | None:
                del root, easy_apply_field
                return cast(Locator, object())

            async def _inspect_text_field_interaction(
                self,
                locator: Locator,
            ) -> TextFieldInteractionState:
                del locator
                return TextFieldInteractionState(
                    current_value="",
                    focused=True,
                    role="combobox",
                    aria_autocomplete="list",
                    aria_expanded=False,
                    has_popup_binding=False,
                    active_descendant=None,
                    visible_option_count=0,
                    invalid=True,
                    validation_message="Please enter a valid answer",
                )

            async def _apply_field_value(
                self,
                page: Page,
                root: Locator,
                field: EasyApplyField,
                resolution: ResolvedFieldValue,
                settings: UserAgentSettings,
                *,
                submission_cv_path: Path | None,
            ) -> str | None:
                del page, root, field, settings, submission_cv_path
                captured["target_value"] = resolution.value
                return resolution.value

        executor = ExecutorDouble()
        executor._answer_resolver = cast(LinkedInAnswerResolver, FakeResolver())
        executor._runtime_settings = cast(
            RuntimeSettings,
            SimpleNamespace(linkedin_field_interaction_timeout_seconds=1),
        )

        await executor._retry_invalid_fields_after_primary_action(
            page=cast(Page, object()),
            settings=settings,
            posting=posting,
            execution_id=uuid4(),
            submission_id=uuid4(),
            execution_events=[],
            previous_step=step,
            step_answers=(),
        )

        assert captured["target_value"] == "2"

    asyncio.run(scenario())


def test_retry_invalid_field_prefers_fresh_resolution_when_selection_is_required() -> None:
    async def scenario() -> None:
        settings = build_user_agent_settings()
        posting = build_posting()
        field = EasyApplyField(
            question_raw=(
                "Você se sente confortável trabalhando em um ambiente onde se fala inglês?"
            ),
            normalized_key="english_comfort",
            question_type=QuestionType.YES_NO_GENERIC,
            control_kind="text",
            input_type="text",
            required=True,
        )
        step = EasyApplyStep(step_index=0, total_steps=1, fields=(field,))
        captured: dict[str, str] = {}

        class FakeResolver:
            async def resolve_with_validation_feedback(
                self,
                field: EasyApplyField,
                settings: UserAgentSettings,
                *,
                posting: JobPosting,
                validation_message: str | None,
                current_value: str = "",
                previous_answer: str | None = None,
            ) -> ResolvedFieldValue | None:
                del field, settings, posting, validation_message, current_value, previous_answer
                return ResolvedFieldValue(
                    value="Yes",
                    answer_source=AnswerSource.AI,
                    fill_strategy=FillStrategy.AUTOFILL_AI,
                    ambiguity_flag=True,
                    confidence=0.72,
                    reasoning="selection_required_refresh",
                )

        class ExecutorDouble(PlaywrightLinkedInEasyApplyExecutor):
            def __init__(self) -> None:
                pass

            async def _easy_apply_modal_visible(self, page: Page) -> bool:
                del page
                return True

            async def _extract_step(
                self,
                page: Page,
                *,
                last_known_step_index: int,
                last_known_total_steps: int,
            ) -> EasyApplyStep:
                del page, last_known_step_index, last_known_total_steps
                return step

            async def _easy_apply_root(self, page: Page) -> Locator:
                del page
                return cast(Locator, object())

            async def _find_control_locator(
                self,
                root: Locator,
                easy_apply_field: EasyApplyField,
            ) -> Locator | None:
                del root, easy_apply_field
                return cast(Locator, object())

            async def _inspect_text_field_interaction(
                self,
                locator: Locator,
            ) -> TextFieldInteractionState:
                del locator
                return TextFieldInteractionState(
                    current_value="",
                    focused=True,
                    role="combobox",
                    aria_autocomplete="list",
                    aria_expanded=False,
                    has_popup_binding=False,
                    active_descendant=None,
                    visible_option_count=0,
                    invalid=True,
                    validation_message="Please make a selection",
                )

            async def _apply_field_value(
                self,
                page: Page,
                root: Locator,
                field: EasyApplyField,
                resolution: ResolvedFieldValue,
                settings: UserAgentSettings,
                *,
                submission_cv_path: Path | None,
            ) -> str | None:
                del page, root, field, settings, submission_cv_path
                captured["target_value"] = resolution.value
                return resolution.value

        executor = ExecutorDouble()
        executor._answer_resolver = cast(LinkedInAnswerResolver, FakeResolver())
        executor._runtime_settings = cast(
            RuntimeSettings,
            SimpleNamespace(linkedin_field_interaction_timeout_seconds=1),
        )

        await executor._retry_invalid_fields_after_primary_action(
            page=cast(Page, object()),
            settings=settings,
            posting=posting,
            execution_id=uuid4(),
            submission_id=uuid4(),
            execution_events=[],
            previous_step=step,
            step_answers=(
                ApplicationAnswer(
                    submission_id=uuid4(),
                    step_index=0,
                    question_raw=field.question_raw,
                    question_type=field.question_type,
                    normalized_key=field.normalized_key,
                    answer_raw="Sim, me sinto confortável em ambientes profissionais.",
                    answer_source=AnswerSource.AI,
                    fill_strategy=FillStrategy.AUTOFILL_AI,
                    ambiguity_flag=True,
                ),
            ),
        )

        assert captured["target_value"] == "Yes"

    asyncio.run(scenario())


def test_retry_invalid_select_field_reapplies_visible_option_with_fresh_resolution() -> None:
    async def scenario() -> None:
        settings = build_user_agent_settings()
        posting = build_posting()
        field = EasyApplyField(
            question_raw="English proficiency",
            normalized_key="english_proficiency",
            question_type=QuestionType.UNKNOWN,
            control_kind="select",
            input_type="select",
            required=True,
            options=("Select an option", "Basic", "Intermediate II", "Advanced"),
        )
        step = EasyApplyStep(step_index=0, total_steps=1, fields=(field,))
        captured: dict[str, str] = {}

        class FakeResolver:
            async def resolve_with_validation_feedback(
                self,
                field: EasyApplyField,
                settings: UserAgentSettings,
                *,
                posting: JobPosting,
                validation_message: str | None,
                current_value: str = "",
                previous_answer: str | None = None,
            ) -> ResolvedFieldValue | None:
                del field, settings, posting, validation_message, current_value, previous_answer
                return ResolvedFieldValue(
                    value="Intermediate II",
                    answer_source=AnswerSource.AI,
                    fill_strategy=FillStrategy.AUTOFILL_AI,
                    ambiguity_flag=True,
                    confidence=0.71,
                    reasoning="conservative_language_choice",
                )

        class ExecutorDouble(PlaywrightLinkedInEasyApplyExecutor):
            def __init__(self) -> None:
                pass

            async def _easy_apply_modal_visible(self, page: Page) -> bool:
                del page
                return True

            async def _extract_step(
                self,
                page: Page,
                *,
                last_known_step_index: int,
                last_known_total_steps: int,
            ) -> EasyApplyStep:
                del page, last_known_step_index, last_known_total_steps
                return step

            async def _easy_apply_root(self, page: Page) -> Locator:
                del page
                return cast(Locator, object())

            async def _find_control_locator(
                self,
                root: Locator,
                easy_apply_field: EasyApplyField,
            ) -> Locator | None:
                del root, easy_apply_field
                return cast(Locator, object())

            async def _inspect_control_validation_state(
                self,
                locator: Locator,
            ) -> ControlValidationState:
                del locator
                return ControlValidationState(
                    invalid=True,
                    validation_message="Please select an option",
                    current_value="Select an option",
                )

            async def _apply_field_value(
                self,
                page: Page,
                root: Locator,
                field: EasyApplyField,
                resolution: ResolvedFieldValue,
                settings: UserAgentSettings,
                *,
                submission_cv_path: Path | None,
            ) -> str | None:
                del page, root, field, settings, submission_cv_path
                captured["value"] = resolution.value
                captured["reasoning"] = resolution.reasoning or ""
                return resolution.value

        executor = ExecutorDouble()
        executor._answer_resolver = cast(LinkedInAnswerResolver, FakeResolver())
        executor._runtime_settings = cast(
            RuntimeSettings,
            SimpleNamespace(linkedin_field_interaction_timeout_seconds=1),
        )

        await executor._retry_invalid_fields_after_primary_action(
            page=cast(Page, object()),
            settings=settings,
            posting=posting,
            execution_id=uuid4(),
            submission_id=uuid4(),
            execution_events=[],
            previous_step=step,
            step_answers=(),
        )

        assert captured["value"] == "Intermediate II"
        assert captured["reasoning"] == "conservative_language_choice"

    asyncio.run(scenario())


def test_step_surface_changed_detects_new_fields_without_counter_change() -> None:
    previous_step = EasyApplyStep(
        step_index=0,
        total_steps=1,
        fields=(
            EasyApplyField(
                question_raw="Carregue o currículo",
                normalized_key="resume_upload",
                question_type=QuestionType.RESUME_UPLOAD,
                control_kind="file",
            ),
        ),
    )
    current_step = EasyApplyStep(
        step_index=0,
        total_steps=1,
        fields=(
            EasyApplyField(
                question_raw="Qual sua pretensão salarial para atuar como PJ?",
                normalized_key="salary_expectation",
                question_type=QuestionType.SALARY_EXPECTATION,
                control_kind="text",
                input_type="number",
            ),
            EasyApplyField(
                question_raw="Você possui experiência sólida com Python em nível avançado?",
                normalized_key="python_experience",
                question_type=QuestionType.YES_NO_GENERIC,
                control_kind="select",
                options=("Selecionar opção", "Sim", "Não"),
            ),
        ),
    )

    assert _step_surface_changed(previous_step, current_step) is True


def test_set_checkbox_state_falls_back_to_clickable_label_surface() -> None:
    async def scenario() -> None:
        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)

        class EmptyLocator:
            async def count(self) -> int:
                return 0

        class CheckboxLocator:
            def __init__(self) -> None:
                self.checked = False

            async def is_checked(self) -> bool:
                return self.checked

            async def check(self, timeout: int | None = None) -> None:
                del timeout
                raise RuntimeError("input surface intercepted")

            async def uncheck(self, timeout: int | None = None) -> None:
                del timeout
                raise RuntimeError("input surface intercepted")

            async def get_attribute(self, name: str) -> str | None:
                if name == "id":
                    return "consent"
                return None

            def locator(self, selector: str) -> EmptyLocator:
                del selector
                return EmptyLocator()

            async def evaluate(
                self,
                expression: str,
                payload: dict[str, object],
            ) -> bool:
                del expression
                self.checked = bool(payload["desiredChecked"])
                return self.checked

        class LabelLocator:
            def __init__(self, checkbox: CheckboxLocator) -> None:
                self._checkbox = checkbox

            async def click(self, timeout: int | None = None) -> None:
                del timeout
                self._checkbox.checked = not self._checkbox.checked

        class LabelQuery:
            def __init__(self, checkbox: CheckboxLocator) -> None:
                self.first = LabelLocator(checkbox)
                self._checkbox = checkbox

            async def count(self) -> int:
                return 1

        class RootLocator:
            def __init__(self, checkbox: CheckboxLocator) -> None:
                self._checkbox = checkbox

            def locator(self, selector: str) -> LabelQuery:
                assert selector == 'label[for="consent"]'
                return LabelQuery(self._checkbox)

        checkbox = CheckboxLocator()
        root = RootLocator(checkbox)

        checked = await executor._set_checkbox_state(
            cast(Locator, root),
            cast(Locator, checkbox),
            desired_checked=True,
        )
        unchecked = await executor._set_checkbox_state(
            cast(Locator, root),
            cast(Locator, checkbox),
            desired_checked=False,
        )

        assert checked is True
        assert unchecked is True
        assert checkbox.checked is False

    asyncio.run(scenario())


def test_apply_field_value_uses_agentic_checkbox_fallback_when_direct_toggle_fails() -> None:
    async def scenario() -> None:
        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
        settings = build_user_agent_settings()
        field = EasyApplyField(
            question_raw="I consent to the processing of my data",
            normalized_key="consent_processing_data",
            question_type=QuestionType.UNKNOWN,
            control_kind="checkbox",
            input_type="checkbox",
        )
        resolution = ResolvedFieldValue(
            value="Yes",
            answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
            fill_strategy=FillStrategy.BEST_EFFORT,
        )
        checkbox = cast(Locator, object())

        async def fake_find_control_locator(
            root: Locator,
            easy_apply_field: EasyApplyField,
        ) -> Locator:
            del root, easy_apply_field
            return checkbox

        async def fake_set_checkbox_state(
            root: Locator,
            locator: Locator,
            *,
            desired_checked: bool,
        ) -> bool:
            del root, locator, desired_checked
            return False

        async def fake_complete_checkbox_interaction(
            *,
            page: Page,
            field: EasyApplyField,
            settings: UserAgentSettings,
            desired_checked: bool,
        ) -> bool:
            del page, field, settings
            return desired_checked

        executor.__dict__["_find_control_locator"] = fake_find_control_locator
        executor.__dict__["_set_checkbox_state"] = fake_set_checkbox_state
        executor.__dict__["_complete_checkbox_interaction"] = fake_complete_checkbox_interaction

        applied = await executor._apply_field_value(
            cast(Page, object()),
            cast(Locator, object()),
            field,
            resolution,
            settings,
            submission_cv_path=None,
        )

        assert applied == "Yes"

    asyncio.run(scenario())


def test_apply_field_value_uses_agentic_radio_fallback_when_direct_selection_fails() -> None:
    async def scenario() -> None:
        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
        settings = build_user_agent_settings()
        field = EasyApplyField(
            question_raw="English comfort",
            normalized_key="english_comfort",
            question_type=QuestionType.YES_NO_GENERIC,
            control_kind="radio",
            input_type="radio",
            options=("Yes", "No"),
        )
        resolution = ResolvedFieldValue(
            value="Yes",
            answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
            fill_strategy=FillStrategy.BEST_EFFORT,
        )

        async def fake_check_radio_option(
            root: Locator,
            easy_apply_field: EasyApplyField,
            option: str,
        ) -> bool:
            del root, easy_apply_field, option
            return False

        async def fake_complete_radio_interaction(
            *,
            page: Page,
            field: EasyApplyField,
            option_index: int,
            option_label: str,
            settings: UserAgentSettings,
        ) -> bool:
            del page, field, option_index, settings
            return option_label == "Yes"

        executor.__dict__["_check_radio_option"] = fake_check_radio_option
        executor.__dict__["_complete_radio_interaction"] = fake_complete_radio_interaction

        applied = await executor._apply_field_value(
            cast(Page, object()),
            cast(Locator, object()),
            field,
            resolution,
            settings,
            submission_cv_path=None,
        )

        assert applied == "Yes"

    asyncio.run(scenario())


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


class YearsExperienceGenerator:
    async def generate(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> GeneratedAnswer | None:
        return GeneratedAnswer(
            value="6",
            confidence=0.58,
            reasoning="best effort numeric estimate",
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


class NoopGenerator:
    async def generate(
        self,
        *,
        field: EasyApplyField,
        settings: UserAgentSettings,
        posting: JobPosting,
    ) -> GeneratedAnswer | None:
        return None


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
            cv_path="/tmp/thiago-martins-cv-2026.pdf",
            cv_filename="Thiago Martins - CV 2026.pdf",
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
