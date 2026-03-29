"""Structured logging and lightweight runtime log context helpers."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_applier.settings import RuntimeSettings

_execution_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "job_applier_execution_id",
    default=None,
)
_submission_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "job_applier_submission_id",
    default=None,
)
_configured_signature: tuple[str, bool, str | None] | None = None

_LOG_RECORD_RESERVED = frozenset(logging.makeLogRecord({}).__dict__.keys()) | {
    "message",
    "asctime",
}
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
)


def configure_logging(settings: RuntimeSettings) -> None:
    """Configure structured logging for the application process."""

    global _configured_signature

    file_path = str(settings.log_file_path) if settings.log_file_path else None
    signature = (settings.log_level.upper(), settings.log_json, file_path)
    if _configured_signature == signature:
        return

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(settings.log_level.upper())

    formatter: logging.Formatter
    if settings.log_json:
        formatter = StructuredJsonFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    if settings.log_file_path is not None:
        settings.log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(settings.log_file_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(settings.log_level.upper())

    _configured_signature = signature


@contextmanager
def bind_execution_context(execution_id: object) -> Iterator[None]:
    """Attach the execution identifier to logs emitted inside the context."""

    token = _execution_id_var.set(str(execution_id))
    try:
        yield
    finally:
        _execution_id_var.reset(token)


@contextmanager
def bind_submission_context(submission_id: object) -> Iterator[None]:
    """Attach the submission identifier to logs emitted inside the context."""

    token = _submission_id_var.set(str(submission_id))
    try:
        yield
    finally:
        _submission_id_var.reset(token)


class StructuredJsonFormatter(logging.Formatter):
    """Render log records as structured JSON with basic redaction."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        execution_id = getattr(record, "execution_id", None) or _execution_id_var.get()
        submission_id = getattr(record, "submission_id", None) or _submission_id_var.get()
        if execution_id is not None:
            payload["execution_id"] = str(execution_id)
        if submission_id is not None:
            payload["submission_id"] = str(submission_id)

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _LOG_RECORD_RESERVED and not key.startswith("_")
        }
        if extras:
            payload.update(_sanitize_for_logs(extras))

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(_sanitize_for_logs(payload), ensure_ascii=True, default=_json_default)


def _sanitize_for_logs(value: Any, *, key: str | None = None) -> Any:
    if key is not None and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_for_logs(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_for_logs(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)
