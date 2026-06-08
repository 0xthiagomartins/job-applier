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
    def test_builds_distinct_recovery_directives_for_chooser_states(self) -> None:
        option_selection = _interactive_field_recovery_directive(
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
        query_reformulation = _interactive_field_recovery_directive(
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

        self.assertNotEqual(option_selection.task_name, query_reformulation.task_name)
        self.assertIn("chooser/autocomplete", option_selection.goal)
        self.assertIn("matching option", query_reformulation.goal)
        self.assertIn("intended value", option_selection.extra_rules[0])
        self.assertIn("intended value", query_reformulation.extra_rules[0])
