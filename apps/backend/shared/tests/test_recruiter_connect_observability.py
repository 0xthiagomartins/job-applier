from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from job_applier.observability import bind_run_output, reset_run_output
from job_applier.recruiter_connect_observability import record_recruiter_connect_observation


class RecruiterConnectObservabilityTests(unittest.TestCase):
    def _load_summary(self, root: Path) -> dict[str, object]:
        return cast(dict[str, object], json.loads((root / "summary.json").read_text("utf-8")))

    def _load_timeline(self, root: Path) -> list[dict[str, object]]:
        lines = (root / "timeline.jsonl").read_text("utf-8").splitlines()
        return [cast(dict[str, object], json.loads(line)) for line in lines]

    def test_records_summary_and_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            reset_run_output(
                output_dir,
                execution_id="exec-rc-1",
                origin="unit-test",
                started_at=datetime.now(UTC),
            )
            with bind_run_output(output_dir):
                record_recruiter_connect_observation(
                    counters=("candidate_detected", "attempted"),
                    status="sent",
                    connect_path="direct_button",
                    send_action="send_without_note",
                    success_signal="button:pending",
                    message_source="ai",
                    note_mode="no_add_note_button",
                    recruiter_name="Alex Recruiter",
                    recruiter_profile_url="https://www.linkedin.com/in/alex/",
                    timeline_event="recruiter_connect_result",
                    extra={"job_posting_id": "job-1"},
                )

            summary = self._load_summary(output_dir)
            timeline = self._load_timeline(output_dir)

        recruiter_summary = cast(dict[str, object], summary["recruiter_connect"])
        counters = cast(dict[str, int], recruiter_summary["counters"])
        self.assertEqual(counters["candidate_detected"], 1)
        self.assertEqual(counters["attempted"], 1)
        self.assertEqual(cast(dict[str, int], recruiter_summary["status_counts"])["sent"], 1)
        self.assertEqual(
            cast(dict[str, int], recruiter_summary["connect_paths"])["direct_button"],
            1,
        )
        self.assertEqual(
            cast(dict[str, int], recruiter_summary["send_actions"])["send_without_note"],
            1,
        )
        self.assertEqual(
            cast(dict[str, int], recruiter_summary["success_signals"])["button:pending"],
            1,
        )
        event_types = [str(item.get("event_type")) for item in timeline]
        self.assertIn("recruiter_connect_result", event_types)


if __name__ == "__main__":
    unittest.main()
