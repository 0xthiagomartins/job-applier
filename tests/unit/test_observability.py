import json
import logging

from job_applier.observability import (
    StructuredJsonFormatter,
    bind_execution_context,
    bind_submission_context,
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
