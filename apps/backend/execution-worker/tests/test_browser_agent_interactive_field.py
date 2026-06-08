from __future__ import annotations

import unittest

from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAutomationError,
    parse_browser_interactive_field_assessment,
)
from job_applier.infrastructure.linkedin.easy_apply import (
    _interactive_field_recovery_directive,
)


class BrowserInteractiveFieldAssessmentTests(unittest.TestCase):
    def test_parses_interactive_field_assessment_payload(self) -> None:
        assessment = parse_browser_interactive_field_assessment(
            {
                "status": "needs_option_selection",
                "confidence": 0.82,
                "summary": "Visible autocomplete options need one committed choice.",
                "evidence": ["listbox visible", "typed query preserved"],
            }
        )

        self.assertEqual(assessment.status, "needs_option_selection")
        self.assertEqual(assessment.confidence, 0.82)
        self.assertEqual(
            assessment.evidence,
            ("listbox visible", "typed query preserved"),
        )

    def test_rejects_unsupported_interactive_field_status(self) -> None:
        with self.assertRaises(BrowserAutomationError):
            parse_browser_interactive_field_assessment(
                {
                    "status": "magic_mode",
                    "confidence": 0.3,
                    "summary": "unsupported",
                    "evidence": [],
                }
            )


class InteractiveFieldRecoveryDirectiveTests(unittest.TestCase):
    def test_maps_option_selection_to_specific_task(self) -> None:
        directive = _interactive_field_recovery_directive(
            assessment=parse_browser_interactive_field_assessment(
                {
                    "status": "needs_option_selection",
                    "confidence": 0.91,
                    "summary": "Choose one visible option.",
                    "evidence": ["options visible"],
                }
            ),
            field_label="Location (city)*",
            target_value="Sao Paulo",
        )

        self.assertEqual(
            directive.task_name,
            "linkedin_easy_apply_select_chooser_option",
        )
        self.assertIn("Select the best visible chooser", directive.goal)

    def test_maps_query_reformulation_to_specific_task(self) -> None:
        directive = _interactive_field_recovery_directive(
            assessment=parse_browser_interactive_field_assessment(
                {
                    "status": "needs_query_reformulation",
                    "confidence": 0.77,
                    "summary": "The typed query is too narrow.",
                    "evidence": ["invalid field", "no options visible"],
                }
            ),
            field_label="School",
            target_value="Universidade de Sao Paulo",
        )

        self.assertEqual(
            directive.task_name,
            "linkedin_easy_apply_reformulate_chooser_query",
        )
        self.assertIn("surface a semantically matching option", directive.goal)
