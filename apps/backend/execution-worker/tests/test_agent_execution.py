from __future__ import annotations

import unittest
from uuid import uuid4

from job_applier.application.agent_execution import (
    _notes_hit_terminal_openai_rate_limit,
    _should_halt_execution_for_openai_rate_limit,
)
from job_applier.domain.entities import ApplicationSubmission
from job_applier.domain.enums import ResumeMode, SubmissionStatus, SupportedLanguage


class AgentExecutionOpenAIRateLimitTests(unittest.TestCase):
    def test_notes_hit_terminal_openai_rate_limit(self) -> None:
        notes = (
            "OpenAI Responses API rate limit while generating a LinkedIn Easy Apply "
            "autofill answer. This is not a LinkedIn page-rate-limit signal."
        )

        self.assertTrue(_notes_hit_terminal_openai_rate_limit(notes))

    def test_submission_halts_execution_for_openai_rate_limit(self) -> None:
        submission = ApplicationSubmission(
            job_posting_id=uuid4(),
            status=SubmissionStatus.FAILED,
            resume_mode=ResumeMode.DYNAMIC,
            target_language=SupportedLanguage.PORTUGUESE,
            notes=(
                "OpenAI Responses API rate limit while planning a LinkedIn Easy Apply "
                "semantic step. This is not a LinkedIn page-rate-limit signal."
            ),
        )

        self.assertTrue(_should_halt_execution_for_openai_rate_limit(submission))
