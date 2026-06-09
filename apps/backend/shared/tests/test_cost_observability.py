from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from job_applier.cost_observability import record_efficiency_counter, record_openai_usage
from job_applier.observability import bind_run_output, reset_run_output


class CostObservabilityTests(unittest.TestCase):
    def _load_summary(self, root: Path) -> dict[str, object]:
        return cast(dict[str, object], json.loads((root / "summary.json").read_text("utf-8")))

    def _load_timeline(self, root: Path) -> list[dict[str, object]]:
        lines = (root / "timeline.jsonl").read_text("utf-8").splitlines()
        return [cast(dict[str, object], json.loads(line)) for line in lines]

    def test_records_openai_usage_and_efficiency_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            reset_run_output(
                output_dir,
                execution_id="exec-1",
                origin="unit-test",
                started_at=datetime.now(UTC),
            )
            with bind_run_output(output_dir):
                record_openai_usage(
                    category="openai.easy_apply.autofill_answer",
                    model="gpt-5",
                    latency_ms=321,
                    response_payload={
                        "usage": {
                            "input_tokens": 120,
                            "output_tokens": 45,
                            "total_tokens": 165,
                        }
                    },
                )
                record_openai_usage(
                    category="openai.easy_apply.autofill_answer",
                    model="gpt-5",
                    latency_ms=210,
                    status="rate_limited",
                    error_status=429,
                    error_message="rate limited",
                )
                record_efficiency_counter(group="search_cache", metric="campaign_hit")
                record_efficiency_counter(group="apply_memory", metric="replayed", delta=2)

            summary = self._load_summary(output_dir)
            timeline = self._load_timeline(output_dir)

        cost = cast(dict[str, object], summary["cost"])
        openai = cast(dict[str, object], cost["openai"])
        self.assertEqual(openai["calls_total"], 2)
        self.assertEqual(openai["rate_limit_count"], 1)
        self.assertEqual(openai["failure_count"], 1)
        self.assertEqual(openai["latency_ms_total"], 531)
        self.assertEqual(
            cast(dict[str, object], openai["tokens"]),
            {"input": 120, "output": 45, "total": 165},
        )

        by_category = cast(dict[str, object], openai["by_category"])
        autofill = cast(dict[str, object], by_category["openai.easy_apply.autofill_answer"])
        self.assertEqual(autofill["calls"], 2)
        self.assertEqual(autofill["rate_limit_count"], 1)
        self.assertEqual(autofill["failure_count"], 1)

        efficiency = cast(dict[str, object], cost["efficiency"])
        self.assertEqual(cast(dict[str, int], efficiency["search_cache"])["campaign_hit"], 1)
        self.assertEqual(cast(dict[str, int], efficiency["apply_memory"])["replayed"], 2)

        event_types = [str(item.get("event_type")) for item in timeline]
        self.assertIn("openai_cost_recorded", event_types)
        self.assertIn("cost_efficiency_recorded", event_types)


if __name__ == "__main__":
    unittest.main()
