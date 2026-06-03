from __future__ import annotations

import unittest

from job_applier.domain.enums import QuestionType
from job_applier.infrastructure.linkedin.question_resolution import LinkedInQuestionClassifier


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

    def test_classifies_current_employer_question(self) -> None:
        classification = self.classifier.classify(
            question_raw="Confirm the name of the company where you work",
            control_kind="text",
            input_type="text",
            options=(),
        )

        self.assertEqual(classification.question_type, QuestionType.FREE_TEXT_GENERIC)
        self.assertEqual(classification.normalized_key, "current_employer")
        self.assertEqual(classification.matched_rule, "current_employer")

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


if __name__ == "__main__":
    unittest.main()
