from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAutomationError,
    BrowserDomSnapshotter,
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


class BrowserDomSnapshotterTests(unittest.TestCase):
    def test_falls_back_to_page_snapshot_when_focus_handle_stalls(self) -> None:
        snapshotter = BrowserDomSnapshotter()
        focus_handle = AsyncMock()
        focus_handle.evaluate = AsyncMock(side_effect=TimeoutError("stale focus handle"))
        focus_handle.dispose = AsyncMock()

        focus_locator = AsyncMock()
        focus_locator.first.element_handle = AsyncMock(return_value=focus_handle)

        page = AsyncMock()
        page.title = AsyncMock(return_value="Resume chooser")
        page.evaluate = AsyncMock(
            return_value={
                "visible_text": "Resume chooser fallback snapshot",
                "elements": [
                    {
                        "element_id": "agent-1",
                        "tag": "input",
                        "role": "radio",
                        "label": "Resume",
                        "text": "",
                        "placeholder": "",
                        "name": "resume_upload",
                        "input_type": "radio",
                        "href": "",
                        "current_value": "target-resume.pdf",
                        "disabled": False,
                        "focused": False,
                        "invalid": False,
                        "expanded": False,
                        "selected": True,
                        "validation_text": "",
                        "is_priority_target": False,
                        "candidate_label": "Resume target-resume.pdf",
                    }
                ],
                "active_surface": "Resume chooser",
                "active_surface_scrollable": False,
                "active_surface_can_scroll_down": False,
                "active_surface_can_scroll_up": False,
                "page_can_scroll_down": False,
                "page_can_scroll_up": False,
            }
        )

        snapshot = asyncio.run(
            snapshotter.capture(
                page,
                focus_locator=focus_locator,
                priority_locator=None,
            )
        )

        self.assertEqual(snapshot.visible_text, "Resume chooser fallback snapshot")
        self.assertGreaterEqual(page.evaluate.await_count, 1)
        focus_handle.evaluate.assert_awaited_once()
        focus_handle.dispose.assert_awaited_once()


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
