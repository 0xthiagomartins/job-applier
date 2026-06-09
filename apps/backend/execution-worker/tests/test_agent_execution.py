from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from job_applier.application.agent_execution import (
    AgentExecutionOrchestrator,
    ExecutionRunSummary,
    _should_halt_execution_for_openai_rate_limit,
)
from job_applier.domain.entities import ApplicationSubmission
from job_applier.domain.enums import (
    AgentExecutionStatus,
    ExecutionOrigin,
    ResumeMode,
    SubmissionStatus,
    SupportedLanguage,
)
from job_applier.observability import reset_run_output


class AgentExecutionOpenAIRateLimitTests(unittest.TestCase):
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


class AgentExecutionSummaryPersistenceTests(unittest.TestCase):
    def test_persist_run_summary_preserves_existing_cost_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            execution_id = uuid4()
            reset_run_output(
                output_dir,
                execution_id=execution_id,
                origin=ExecutionOrigin.SCHEDULED.value,
                started_at=datetime.now(UTC),
            )
            summary_path = output_dir / "summary.json"
            payload = json.loads(summary_path.read_text("utf-8"))
            payload["cost"]["openai"]["calls_total"] = 7
            summary_path.write_text(json.dumps(payload), encoding="utf-8")

            orchestrator = object.__new__(AgentExecutionOrchestrator)
            orchestrator._output_dir = output_dir
            orchestrator._persist_run_summary(
                ExecutionRunSummary(
                    execution_id=execution_id,
                    origin=ExecutionOrigin.SCHEDULED,
                    status=AgentExecutionStatus.COMPLETED,
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    jobs_seen=3,
                    jobs_selected=1,
                    successful_submissions=1,
                    error_count=0,
                )
            )

            persisted = json.loads(summary_path.read_text("utf-8"))
            self.assertEqual(persisted["cost"]["openai"]["calls_total"], 7)
