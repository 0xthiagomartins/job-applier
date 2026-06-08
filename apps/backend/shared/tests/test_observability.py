from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from job_applier.observability import update_progress_snapshot


class ProgressObservabilityTests(unittest.TestCase):
    def _load_progress(self, root: Path) -> dict[str, object]:
        return cast(
            dict[str, object],
            json.loads((root / "progress.json").read_text(encoding="utf-8")),
        )

    def test_replaces_current_job_when_submission_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            update_progress_snapshot(
                {
                    "current_stage": "submit_skipped",
                    "current_job": {
                        "job_posting_id": "job-1",
                        "submission_id": "sub-1",
                        "company_name": "Old Corp",
                        "status": "skipped",
                        "skip_reason": "already_applied",
                    },
                },
                output_dir=output_dir,
            )
            update_progress_snapshot(
                {
                    "current_stage": "easy_apply_job_page_loaded",
                    "current_job": {
                        "job_posting_id": "job-2",
                        "submission_id": "sub-2",
                        "company_name": "New Corp",
                    },
                },
                output_dir=output_dir,
            )

            payload = self._load_progress(output_dir)

        current_job = payload["current_job"]
        assert isinstance(current_job, dict)
        self.assertEqual(current_job["job_posting_id"], "job-2")
        self.assertEqual(current_job["submission_id"], "sub-2")
        self.assertEqual(current_job["company_name"], "New Corp")
        self.assertNotIn("skip_reason", current_job)
        self.assertNotIn("status", current_job)

    def test_preserves_same_job_fields_when_payload_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            update_progress_snapshot(
                {
                    "current_stage": "score_job_started",
                    "current_job": {
                        "job_posting_id": "job-1",
                        "submission_id": "sub-1",
                        "company_name": "Example Corp",
                        "score": 0.77,
                        "external_job_id": "4420000000",
                    },
                },
                output_dir=output_dir,
            )
            update_progress_snapshot(
                {
                    "current_stage": "easy_apply_job_page_loaded",
                    "current_job": {
                        "job_posting_id": "job-1",
                        "submission_id": "sub-1",
                        "status": "running",
                    },
                },
                output_dir=output_dir,
            )

            payload = self._load_progress(output_dir)

        current_job = payload["current_job"]
        assert isinstance(current_job, dict)
        self.assertEqual(current_job["score"], 0.77)
        self.assertEqual(current_job["external_job_id"], "4420000000")
        self.assertEqual(current_job["status"], "running")

    def test_clears_stale_last_error_on_stage_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            update_progress_snapshot(
                {
                    "current_stage": "easy_apply_failed",
                    "last_error": "resume failed",
                },
                output_dir=output_dir,
            )
            update_progress_snapshot(
                {
                    "current_stage": "easy_apply_job_page_loaded",
                },
                output_dir=output_dir,
            )

            payload = self._load_progress(output_dir)

        self.assertIsNone(payload["last_error"])


if __name__ == "__main__":
    unittest.main()
