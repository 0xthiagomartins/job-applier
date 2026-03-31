import json
import logging
from pathlib import Path

from job_applier.observability import (
    StructuredJsonFormatter,
    append_artifact_reference,
    append_timeline_event,
    bind_execution_context,
    bind_run_output,
    bind_submission_context,
    update_progress_snapshot,
)


def test_structured_json_formatter_binds_context_and_redacts_sensitive_fields() -> None:
    formatter = StructuredJsonFormatter()
    logger = logging.getLogger("job_applier.tests")

    with bind_execution_context("exec-123"), bind_submission_context("sub-456"):
        record = logger.makeRecord(
            name="job_applier.tests",
            level=logging.INFO,
            fn=__file__,
            lno=10,
            msg="test_event",
            args=(),
            exc_info=None,
            extra={
                "openai_api_key": "sk-test-secret",
                "payload": {
                    "safe_value": "ok",
                    "password": "top-secret",
                },
            },
        )
        payload = json.loads(formatter.format(record))

    assert payload["event"] == "test_event"
    assert payload["execution_id"] == "exec-123"
    assert payload["submission_id"] == "sub-456"
    assert payload["openai_api_key"] == "[redacted]"
    assert payload["payload"]["password"] == "[redacted]"
    assert payload["payload"]["safe_value"] == "ok"


def test_progress_timeline_and_artifact_helpers_write_last_run_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "artifacts" / "last-run"
    output_dir.mkdir(parents=True, exist_ok=True)

    with (
        bind_execution_context("exec-123"),
        bind_submission_context("sub-456"),
        bind_run_output(output_dir),
    ):
        update_progress_snapshot({"current_stage": "easy_apply_step", "current_step": 2})
        append_timeline_event("easy_apply_step_extracted", {"step_index": 1, "field_count": 3})
        append_artifact_reference(
            artifact_type="screenshot",
            label="step_02",
            path="/tmp/fake-step.png",
            sha256="abc123",
        )

    progress_payload = json.loads((output_dir / "progress.json").read_text(encoding="utf-8"))
    assert progress_payload["current_stage"] == "easy_apply_step"
    assert progress_payload["current_step"] == 2
    assert progress_payload["last_observation"]

    timeline_lines = (
        (output_dir / "timeline.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    assert len(timeline_lines) == 1
    timeline_payload = json.loads(timeline_lines[0])
    assert timeline_payload["event_type"] == "easy_apply_step_extracted"
    assert timeline_payload["execution_id"] == "exec-123"
    assert timeline_payload["submission_id"] == "sub-456"

    artifact_lines = (
        (output_dir / "artifacts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    assert len(artifact_lines) == 1
    artifact_payload = json.loads(artifact_lines[0])
    assert artifact_payload["artifact_type"] == "screenshot"
    assert artifact_payload["label"] == "step_02"
    assert artifact_payload["execution_id"] == "exec-123"
