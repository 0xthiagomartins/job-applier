from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from unittest.mock import patch
from uuid import uuid4

from pydantic import SecretStr

from job_applier.application.config import (
    AgentConfig,
    AIConfig,
    PrivateMetadataConfig,
    RulesetConfig,
    ScheduleConfig,
    SearchConfig,
    UserAgentSettings,
    UserProfileConfig,
)
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import (
    AnswerSource,
    FillStrategy,
    Platform,
    QuestionType,
    ResumeMode,
    SupportedLanguage,
)
from job_applier.infrastructure.linkedin.question_resolution import (
    EasyApplyField,
    GeneratedAnswer,
    LinkedInAnswerResolver,
    LinkedInQuestionClassifier,
    OpenAIResponsesAnswerGenerator,
    OpenAIResponsesRateLimitError,
    OpenAISemanticStepPlanner,
    SemanticFieldPlan,
    _validation_feedback_requires_semantic_retry,
)
from job_applier.observability import bind_run_output, reset_run_output


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class LinkedInQuestionClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = LinkedInQuestionClassifier()

    def test_classifies_referral_source_question(self) -> None:
        classification = self.classifier.classify(
            question_raw="Como ficou sabendo da nossa vaga?",
            control_kind="select",
            input_type="select",
            options=("Select an option", "LinkedIn", "Indicação"),
        )

        self.assertEqual(classification.question_type, QuestionType.FREE_TEXT_GENERIC)
        self.assertEqual(classification.normalized_key, "referral_source")
        self.assertEqual(classification.matched_rule, "referral_source")

    def test_classifies_factual_fields_that_must_not_be_inferred(self) -> None:
        cases = (
            (
                "Confirm the name of the company where you work",
                "text",
                "text",
                (),
                "current_employer",
            ),
            (
                "Você trabalha atualmente no Inter?",
                "select",
                "select",
                ("Sim", "Não"),
                "current_employer",
            ),
            (
                "Informe seu salário atual/último:",
                "text",
                "text",
                (),
                "current_salary",
            ),
            (
                "Informe seus Benefícios atuais/últimos:",
                "text",
                "text",
                (),
                "current_benefits",
            ),
            ("CPF", "text", "text", (), "cpf"),
        )

        for question_raw, control_kind, input_type, options, normalized_key in cases:
            with self.subTest(normalized_key=normalized_key, question_raw=question_raw):
                classification = self.classifier.classify(
                    question_raw=question_raw,
                    control_kind=cast(
                        Literal["text", "textarea", "select", "radio", "checkbox", "file"],
                        control_kind,
                    ),
                    input_type=input_type,
                    options=options,
                )

                self.assertEqual(
                    classification.question_type,
                    QuestionType.FREE_TEXT_GENERIC,
                )
                self.assertEqual(classification.normalized_key, normalized_key)
                self.assertEqual(classification.matched_rule, normalized_key)

    def test_does_not_misclassify_workplace_availability_as_start_date(self) -> None:
        classification = self.classifier.classify(
            question_raw=(
                "Você tem disponibilidade para trabalho presencial na cidade de Campinas "
                "todos os dias?"
            ),
            control_kind="select",
            input_type="select",
            options=(
                "Select an option",
                "Sim, moro em Campinas",
                "Sim, tenho interesse em mudança",
                "Não tenho disponibilidade",
            ),
        )

        self.assertEqual(classification.question_type, QuestionType.FREE_TEXT_GENERIC)
        self.assertEqual(classification.normalized_key, "workplace_availability")
        self.assertEqual(classification.matched_rule, "workplace_availability")

    def test_classifies_proficiency_ladder_questions(self) -> None:
        classification = self.classifier.classify(
            question_raw="Como você avalia seu conhecimento com Java 8+?",
            control_kind="radio",
            input_type="radio",
            options=("Básico", "Intermediário", "Avançado"),
        )

        self.assertEqual(classification.question_type, QuestionType.FREE_TEXT_GENERIC)
        self.assertEqual(classification.normalized_key, "java_proficiency")
        self.assertEqual(classification.matched_rule, "proficiency_ladder")

    def test_classifies_language_work_environment_comfort(self) -> None:
        classification = self.classifier.classify(
            question_raw="How comfortable do you feel working in an English-speaking environment?",
            control_kind="text",
            input_type="text",
            options=(),
        )

        self.assertEqual(classification.question_type, QuestionType.FREE_TEXT_GENERIC)
        self.assertEqual(classification.normalized_key, "english_work_environment_comfort")
        self.assertEqual(classification.matched_rule, "language_working_comfort")

    def test_classifies_disability_status_with_stable_key(self) -> None:
        classification = self.classifier.classify(
            question_raw="Você é pessoa com deficiência?",
            control_kind="select",
            input_type="select",
            options=("Select an option", "Sim", "Não"),
        )

        self.assertEqual(classification.question_type, QuestionType.YES_NO_GENERIC)
        self.assertEqual(classification.normalized_key, "disability_status")
        self.assertEqual(classification.matched_rule, "disability_status")


class _RecordingAnswerGenerator:
    def __init__(self, answer: GeneratedAnswer | None = None) -> None:
        self.answer = answer
        self.calls = 0

    async def generate(self, **_: object) -> GeneratedAnswer | None:
        self.calls += 1
        return self.answer


class _RateLimitAnswerGenerator:
    async def generate(self, **_: object) -> GeneratedAnswer | None:
        msg = (
            "OpenAI Responses API rate limit while generating a LinkedIn Easy Apply "
            "autofill answer. This is not a LinkedIn page-rate-limit signal."
        )
        raise OpenAIResponsesRateLimitError(msg)


def _build_settings() -> UserAgentSettings:
    return UserAgentSettings(
        profile=UserProfileConfig(
            name="Thiago Martins",
            email="thiago@example.com",
            phone="+5511999999999",
            city="Sao Paulo, Brazil",
            work_authorized=True,
            availability="Immediate",
            resume_mode=ResumeMode.STATIC,
            preferred_language=SupportedLanguage.PORTUGUESE,
        ),
        search=SearchConfig(
            keywords=("Backend Developer",),
            location="Brazil",
        ),
        agent=AgentConfig(schedule=ScheduleConfig()),
        ai=AIConfig(model="gpt-5", api_key=None),
        ruleset=RulesetConfig(allow_best_effort_autofill=True),
    )


def _build_posting() -> JobPosting:
    return JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/1234567890/",
        title="Backend Engineer",
        company_name="Example",
        description_raw="Example backend engineering role in Brazil.",
    )


class LinkedInAnswerResolverSensitiveGuardrailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.generator = _RecordingAnswerGenerator(
            GeneratedAnswer(
                value="Homem Cisgênero",
                confidence=0.95,
                reasoning="should_not_be_used",
            )
        )
        self.resolver = LinkedInAnswerResolver(ambiguous_answer_generator=self.generator)
        self.settings = _build_settings()
        self.posting = _build_posting()

    async def test_optional_gender_question_ignores_semantic_plan_and_ai(self) -> None:
        field = EasyApplyField(
            question_raw="Gostaria de nos dizer sua identidade de gênero?",
            normalized_key="gostaria_de_nos_dizer_sua_identidade_de_genero",
            question_type=QuestionType.UNKNOWN,
            control_kind="select",
            required=False,
            options=("Homem Cisgênero", "Mulher Cisgênero", "Pessoa Não Binária"),
        )

        resolved = await self.resolver.resolve(
            field,
            self.settings,
            posting=self.posting,
            semantic_plan=SemanticFieldPlan(
                field_ref=field.normalized_key,
                semantic_slot="candidate.gender",
                answer="Homem Cisgênero",
                confidence=0.95,
                reasoning="semantic guess",
            ),
        )

        self.assertIsNone(resolved)
        self.assertEqual(self.generator.calls, 0)

    async def test_sensitive_question_uses_opt_out_option_when_available(self) -> None:
        field = EasyApplyField(
            question_raw="Como você autodeclara sua cor/raça?",
            normalized_key="como_voce_autodeclara_sua_cor_raca",
            question_type=QuestionType.UNKNOWN,
            control_kind="select",
            required=False,
            options=("Branca", "Parda", "Prefiro não informar"),
        )

        resolved = await self.resolver.resolve(
            field,
            self.settings,
            posting=self.posting,
            semantic_plan=SemanticFieldPlan(
                field_ref=field.normalized_key,
                semantic_slot="candidate.ethnicity",
                answer="Branca",
                confidence=0.9,
                reasoning="semantic guess",
            ),
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.value, "Prefiro não informar")
        self.assertEqual(resolved.answer_source, AnswerSource.BEST_EFFORT_AUTOFILL)
        self.assertEqual(resolved.fill_strategy, FillStrategy.BEST_EFFORT)
        self.assertEqual(self.generator.calls, 0)

    async def test_demographic_gate_declines_before_optional_questions_open(self) -> None:
        field = EasyApplyField(
            question_raw=(
                "As respostas abaixo são opcionais. Estes dados serão usados para nossas "
                "ações afirmativas. Considerando esse cenário, você se sente confortável "
                "de responder as questões abaixo?"
            ),
            normalized_key="demographic_questions_comfort_gate",
            question_type=QuestionType.YES_NO_GENERIC,
            control_kind="select",
            required=False,
            options=("Sim", "Não"),
        )

        resolved = await self.resolver.resolve(
            field,
            self.settings,
            posting=self.posting,
            semantic_plan=SemanticFieldPlan(
                field_ref=field.normalized_key,
                semantic_slot="candidate.demographic_disclosure",
                answer="Sim",
                confidence=0.92,
                reasoning="semantic guess",
            ),
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.value, "Não")
        self.assertEqual(resolved.reasoning, "sensitive_demographic_gate_decline")
        self.assertEqual(self.generator.calls, 0)

    async def test_generic_drug_test_binary_question_is_not_misclassified_as_sensitive(
        self,
    ) -> None:
        resolver = LinkedInAnswerResolver(
            ambiguous_answer_generator=_RecordingAnswerGenerator(),
        )
        field = EasyApplyField(
            question_raw=(
                "Are you willing to take a drug test, in accordance with local law/regulations?"
            ),
            normalized_key="are_you_willing_to_take_a_drug_test_in_accordance_with_local_law_regulations",
            question_type=QuestionType.YES_NO_GENERIC,
            control_kind="radio",
            required=True,
            options=("Yes", "No"),
            field_context=(
                "Are you willing to take a drug test, in accordance with local law/regulations? "
                "Yes No"
            ),
        )

        resolved = await resolver.resolve(
            field,
            self.settings,
            posting=self.posting,
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.value, "Yes")
        self.assertEqual(resolved.answer_source, AnswerSource.BEST_EFFORT_AUTOFILL)
        self.assertEqual(resolved.fill_strategy, FillStrategy.BEST_EFFORT)


class LinkedInAnswerResolverRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.generator = _RecordingAnswerGenerator()
        self.resolver = LinkedInAnswerResolver(ambiguous_answer_generator=self.generator)
        self.settings = _build_settings()
        self.posting = _build_posting()

    async def test_resolve_propagates_openai_rate_limit(self) -> None:
        resolver = LinkedInAnswerResolver(ambiguous_answer_generator=_RateLimitAnswerGenerator())
        settings = _build_settings().model_copy(
            update={"ai": AIConfig(model="gpt-5", api_key=SecretStr("test-key"))}
        )
        posting = _build_posting()
        field = EasyApplyField(
            question_raw="How many years of Python experience do you have?",
            normalized_key="python_years_experience",
            question_type=QuestionType.YEARS_EXPERIENCE,
            control_kind="text",
            input_type="number",
            required=True,
        )

        with self.assertRaises(OpenAIResponsesRateLimitError):
            await resolver.resolve(field, settings, posting=posting)

    async def test_required_current_employer_without_profile_fact_stays_unresolved(self) -> None:
        field = EasyApplyField(
            question_raw="Confirm the name of the company where you work",
            normalized_key="current_employer",
            question_type=QuestionType.FREE_TEXT_GENERIC,
            control_kind="text",
            input_type="text",
            required=True,
        )

        resolved = await self.resolver.resolve(
            field,
            self.settings,
            posting=self.posting,
        )

        self.assertIsNone(resolved)
        self.assertEqual(self.generator.calls, 0)

    async def test_required_current_employer_uses_single_active_employer_from_cv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cv_path = Path(temp_dir) / "resume.txt"
            cv_path.write_text(
                "\n".join(
                    (
                        "Thiago Martins",
                        "Full Stack Software Engineer",
                        "Summary",
                        "Software engineer focused on backend systems and automation.",
                        "Experience",
                        "Automation Developer  SAMSUNG SDS  11/2024 - Present",
                        "- Built automation systems",
                        "Skills",
                        "Backend: Java, Python",
                    )
                ),
                encoding="utf-8",
            )
            settings = self.settings.model_copy(
                update={
                    "profile": self.settings.profile.model_copy(
                        update={
                            "cv_path": str(cv_path),
                            "cv_filename": cv_path.name,
                        }
                    )
                }
            )
            field = EasyApplyField(
                question_raw="Confirm the name of the company where you work",
                normalized_key="current_employer",
                question_type=QuestionType.FREE_TEXT_GENERIC,
                control_kind="text",
                input_type="text",
                required=True,
            )

            resolved = await self.resolver.resolve(
                field,
                settings,
                posting=self.posting,
            )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.value, "SAMSUNG SDS")
        self.assertEqual(resolved.answer_source, AnswerSource.PROFILE_SNAPSHOT)
        self.assertEqual(self.generator.calls, 0)

    async def test_current_employer_binary_question_uses_posting_company_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cv_path = Path(temp_dir) / "resume.txt"
            cv_path.write_text(
                "\n".join(
                    (
                        "Thiago Martins",
                        "Full Stack Software Engineer",
                        "Summary",
                        "Software engineer focused on backend systems and automation.",
                        "Experience",
                        "Automation Developer  SAMSUNG SDS  11/2024 - Present",
                        "- Built automation systems",
                        "Freelance Developer  Self-Employed  01/2019 - Present",
                        "- Built APIs",
                        "Skills",
                        "Backend: Java, Python",
                    )
                ),
                encoding="utf-8",
            )
            settings = self.settings.model_copy(
                update={
                    "profile": self.settings.profile.model_copy(
                        update={
                            "cv_path": str(cv_path),
                            "cv_filename": cv_path.name,
                        }
                    )
                }
            )
            field = EasyApplyField(
                question_raw="Você trabalha atualmente no Inter?",
                normalized_key="current_employer",
                question_type=QuestionType.FREE_TEXT_GENERIC,
                control_kind="select",
                input_type="select",
                options=("Sim", "Não"),
                required=True,
            )
            posting = JobPosting(
                id=uuid4(),
                platform=self.posting.platform,
                url=self.posting.url,
                title=self.posting.title,
                company_name="Inter",
                description_raw=self.posting.description_raw,
                location=self.posting.location,
                workplace_type=self.posting.workplace_type,
                seniority=self.posting.seniority,
                easy_apply=self.posting.easy_apply,
                description_hash=self.posting.description_hash,
                detail_quality_score=self.posting.detail_quality_score,
                detail_description_score=self.posting.detail_description_score,
                detail_quality_source=self.posting.detail_quality_source,
                detail_quality_signals=self.posting.detail_quality_signals,
                captured_at=self.posting.captured_at,
            )

            resolved = await self.resolver.resolve(
                field,
                settings,
                posting=posting,
            )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.value, "Não")
        self.assertEqual(resolved.answer_source, AnswerSource.PROFILE_SNAPSHOT)
        self.assertEqual(self.generator.calls, 0)

    async def test_semantic_text_feedback_does_not_blindly_reuse_rejected_value(
        self,
    ) -> None:
        field = EasyApplyField(
            question_raw="Enter city or location",
            normalized_key="city",
            question_type=QuestionType.CITY,
            control_kind="text",
            input_type="text",
            required=True,
            prefilled=True,
            current_value="Sao Paulo",
            field_context="Location (city)* This field is required",
        )

        resolved = await self.resolver.resolve_with_validation_feedback(
            field,
            self.settings,
            posting=self.posting,
            validation_message="This field is required",
            current_value="Sao Paulo",
            previous_answer="Sao Paulo",
        )

        if resolved is not None:
            self.assertNotEqual(resolved.value, "Sao Paulo")
            self.assertNotEqual(
                resolved.reasoning,
                "reuse_previous_answer_with_validation_feedback",
            )
            self.assertNotEqual(
                resolved.reasoning,
                "reuse_current_value_with_validation_feedback",
            )

    async def test_missing_factual_fields_stay_unresolved_without_profile_or_metadata(self) -> None:
        cases = (
            EasyApplyField(
                question_raw="What is your salary expectation?",
                normalized_key="salary_expectation",
                question_type=QuestionType.SALARY_EXPECTATION,
                control_kind="text",
                input_type="text",
                required=True,
            ),
            EasyApplyField(
                question_raw="Informe seu salário atual/último:",
                normalized_key="current_salary",
                question_type=QuestionType.FREE_TEXT_GENERIC,
                control_kind="text",
                input_type="text",
                required=True,
            ),
            EasyApplyField(
                question_raw="Informe seus Benefícios atuais/últimos:",
                normalized_key="current_benefits",
                question_type=QuestionType.FREE_TEXT_GENERIC,
                control_kind="text",
                input_type="text",
                required=False,
            ),
            EasyApplyField(
                question_raw="CPF",
                normalized_key="cpf",
                question_type=QuestionType.FREE_TEXT_GENERIC,
                control_kind="text",
                input_type="text",
                required=True,
            ),
        )

        for field in cases:
            with self.subTest(normalized_key=field.normalized_key):
                self.generator.calls = 0
                resolved = await self.resolver.resolve(
                    field,
                    self.settings,
                    posting=self.posting,
                )

                self.assertIsNone(resolved)
                self.assertEqual(self.generator.calls, 0)

    async def test_required_cpf_with_private_metadata_uses_agentic_answer(self) -> None:
        generator = _RecordingAnswerGenerator(
            GeneratedAnswer(
                value="507.329.848-90",
                confidence=0.93,
                reasoning="user_provided_private_metadata",
            )
        )
        resolver = LinkedInAnswerResolver(ambiguous_answer_generator=generator)
        settings = _build_settings().model_copy(
            update={
                "ai": AIConfig(model="gpt-5", api_key=SecretStr("test-key")),
                "private_metadata": PrivateMetadataConfig(
                    entries={"cpf": "507.329.848-90", "mother_name": "Maria Example"},
                    consent_to_ai_usage=True,
                ),
            }
        )
        field = EasyApplyField(
            question_raw="CPF",
            normalized_key="cpf",
            question_type=QuestionType.FREE_TEXT_GENERIC,
            control_kind="text",
            input_type="text",
            required=True,
        )

        resolved = await resolver.resolve(
            field,
            settings,
            posting=self.posting,
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.value, "507.329.848-90")
        self.assertEqual(resolved.answer_source, AnswerSource.AI)
        self.assertEqual(resolved.fill_strategy, FillStrategy.AUTOFILL_AI)
        self.assertEqual(generator.calls, 1)

    async def test_optional_disability_type_question_stays_blank_without_opt_out(self) -> None:
        field = EasyApplyField(
            question_raw="Qual é o tipo de deficiência?",
            normalized_key="disability_type",
            question_type=QuestionType.FREE_TEXT_GENERIC,
            control_kind="text",
            required=False,
        )

        resolved = await self.resolver.resolve(
            field,
            self.settings,
            posting=self.posting,
        )

        self.assertIsNone(resolved)
        self.assertEqual(self.generator.calls, 0)

    async def test_accessibility_accommodation_prefers_explicit_no_need_option(self) -> None:
        field = EasyApplyField(
            question_raw=(
                "Você precisa de algum tipo de acessibilidade para participar do processo "
                "seletivo e/ou no seu dia-a-dia?"
            ),
            normalized_key="accessibility_accommodation",
            question_type=QuestionType.UNKNOWN,
            control_kind="select",
            required=True,
            options=(
                "Select an option",
                "Descrição de imagens e audiodescrição de vídeos",
                "Elevador/Rampa",
                "Não necessito de nenhuma acessibilidade",
            ),
        )

        resolved = await self.resolver.resolve(
            field,
            self.settings,
            posting=self.posting,
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.value, "Não necessito de nenhuma acessibilidade")
        self.assertEqual(resolved.reasoning, "accessibility_accommodation_not_requested")
        self.assertEqual(self.generator.calls, 0)

    async def test_optional_checkbox_defaults_to_no_without_ai(self) -> None:
        field = EasyApplyField(
            question_raw="Autorizo receber comunicações sobre futuras oportunidades",
            normalized_key="future_opportunities_opt_in",
            question_type=QuestionType.YES_NO_GENERIC,
            control_kind="checkbox",
            required=False,
            options=("Yes", "No"),
        )

        resolved = await self.resolver.resolve(
            field,
            self.settings,
            posting=self.posting,
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.value, "No")
        self.assertEqual(resolved.answer_source, AnswerSource.RULE)
        self.assertEqual(resolved.fill_strategy, FillStrategy.DETERMINISTIC)
        self.assertEqual(resolved.reasoning, "optional_checkbox_declined_by_default")
        self.assertEqual(self.generator.calls, 0)

    async def test_optional_checkbox_allows_explicit_default_response_override(self) -> None:
        settings = _build_settings()
        settings.profile.default_responses["future_opportunities_opt_in"] = "Yes"
        field = EasyApplyField(
            question_raw="Autorizo receber comunicações sobre futuras oportunidades",
            normalized_key="future_opportunities_opt_in",
            question_type=QuestionType.YES_NO_GENERIC,
            control_kind="checkbox",
            required=False,
            options=("Yes", "No"),
        )

        resolved = await self.resolver.resolve(
            field,
            settings,
            posting=self.posting,
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.value, "Yes")
        self.assertEqual(resolved.answer_source, AnswerSource.DEFAULT_RESPONSE)

    def test_private_metadata_prompt_payload_is_field_scoped(self) -> None:
        generator = OpenAIResponsesAnswerGenerator()
        settings = _build_settings().model_copy(
            update={
                "private_metadata": PrivateMetadataConfig(
                    entries={"cpf": "507.329.848-90", "mother_name": "Maria Example"},
                    consent_to_ai_usage=True,
                )
            }
        )
        cpf_field = EasyApplyField(
            question_raw="CPF",
            normalized_key="cpf",
            question_type=QuestionType.FREE_TEXT_GENERIC,
            control_kind="text",
            input_type="text",
            required=True,
        )
        email_field = EasyApplyField(
            question_raw="Email",
            normalized_key="email",
            question_type=QuestionType.EMAIL,
            control_kind="text",
            input_type="email",
            required=True,
        )

        cpf_payload = generator._build_prompt_payload(  # noqa: SLF001
            field=cpf_field,
            settings=settings,
            posting=self.posting,
        )
        email_payload = generator._build_prompt_payload(  # noqa: SLF001
            field=email_field,
            settings=settings,
            posting=self.posting,
        )

        self.assertEqual(
            cpf_payload["private_metadata_context"]["values"],  # type: ignore[index]
            {"cpf": "507.329.848-90"},
        )
        self.assertNotIn("private_metadata_context", email_payload)

    def test_semantic_step_planner_marks_private_metadata_per_matching_field(self) -> None:
        planner = OpenAISemanticStepPlanner()
        settings = _build_settings().model_copy(
            update={
                "private_metadata": PrivateMetadataConfig(
                    entries={"cpf": "507.329.848-90", "mother_name": "Maria Example"},
                    consent_to_ai_usage=True,
                )
            }
        )
        cpf_field = EasyApplyField(
            question_raw="CPF",
            normalized_key="cpf",
            question_type=QuestionType.FREE_TEXT_GENERIC,
            control_kind="text",
            input_type="text",
            required=True,
        )
        city_field = EasyApplyField(
            question_raw="Cidade",
            normalized_key="city",
            question_type=QuestionType.CITY,
            control_kind="text",
            input_type="text",
            required=True,
        )

        payload = planner._build_prompt_payload(  # noqa: SLF001
            fields=(cpf_field, city_field),
            candidate_fields=(cpf_field, city_field),
            step_index=0,
            total_steps=1,
            surface_text="CPF e cidade",
            settings=settings,
            posting=self.posting,
        )

        self.assertEqual(
            payload["private_metadata_context_by_field"],
            {
                "cpf": {
                    "values": {"cpf": "507.329.848-90"},
                    "labels": {"cpf": "CPF"},
                    "usage_policy": [
                        (
                            "These values were explicitly provided by the user outside the "
                            "base resume."
                        ),
                        (
                            "Use only the fields directly relevant to the current application "
                            "question."
                        ),
                        "Do not mention unrelated private metadata in the answer.",
                    ],
                }
            },
        )

    def test_answer_generator_records_openai_cost_summary(self) -> None:
        generator = OpenAIResponsesAnswerGenerator()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            reset_run_output(
                output_dir,
                execution_id="question-resolution-test",
                origin="unit-test",
                started_at=datetime.now(UTC),
            )
            with (
                bind_run_output(output_dir),
                patch(
                    "job_applier.infrastructure.linkedin.question_resolution.request.urlopen",
                    return_value=_FakeHttpResponse(
                        {
                            "output": [],
                            "usage": {
                                "input_tokens": 50,
                                "output_tokens": 10,
                                "total_tokens": 60,
                            },
                        }
                    ),
                ),
            ):
                payload = generator._create_response(  # noqa: SLF001
                    api_key="test-key",
                    model="gpt-5",
                    prompt_payload={"normalized_key": "email"},
                )
            summary = cast(
                dict[str, object],
                json.loads((output_dir / "summary.json").read_text(encoding="utf-8")),
            )

        self.assertIn("usage", payload)
        by_category = cast(
            dict[str, object],
            cast(dict[str, object], cast(dict[str, object], summary["cost"])["openai"])[
                "by_category"
            ],
        )
        category_summary = cast(dict[str, object], by_category["openai.easy_apply.autofill_answer"])
        self.assertEqual(category_summary["calls"], 1)
        self.assertEqual(
            cast(dict[str, object], category_summary["tokens"])["total"],
            60,
        )


class ValidationFeedbackSemanticRetryTests(unittest.TestCase):
    def test_semantic_retry_detects_required_chooser_feedback_for_text_field(self) -> None:
        field = EasyApplyField(
            question_raw="Enter city or location",
            normalized_key="city",
            question_type=QuestionType.CITY,
            control_kind="text",
            input_type="text",
            required=True,
            field_context="Location (city)* This field is required",
        )

        self.assertTrue(
            _validation_feedback_requires_semantic_retry(
                field=field,
                normalized_validation="this field is required",
            )
        )


if __name__ == "__main__":
    unittest.main()
