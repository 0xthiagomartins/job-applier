"""Structured logging and lightweight runtime log context helpers."""

from __future__ import annotations

import contextvars
import json
import logging
import shutil
import sys
from collections.abc import Iterator, Mapping
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
_run_output_dir_var: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "job_applier_run_output_dir",
    default=None,
)
_configured_signature: tuple[str, bool, str | None, str] | None = None

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
    signature = (settings.log_level.upper(), settings.log_json, file_path, str(settings.output_dir))
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

    output_handler = LastRunDebugHandler(settings.output_dir / "run.log")
    output_handler.setFormatter(StructuredJsonFormatter())
    root_logger.addHandler(output_handler)

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


@contextmanager
def bind_run_output(output_dir: Path | None) -> Iterator[None]:
    """Attach the current last-run output directory to async log context."""

    token = _run_output_dir_var.set(output_dir)
    try:
        yield
    finally:
        _run_output_dir_var.reset(token)


def reset_run_output(
    output_dir: Path,
    *,
    execution_id: object,
    origin: str,
    started_at: datetime,
) -> None:
    """Clear the previous last-run bundle before the next execution starts."""

    output_dir.mkdir(parents=True, exist_ok=True)
    for child in output_dir.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_dir():
            shutil.rmtree(child)
            continue
        child.unlink()

    write_output_json(
        "summary.json",
        {
            "execution_id": str(execution_id),
            "origin": origin,
            "status": "running",
            "started_at": started_at.isoformat(),
            "finished_at": None,
            "snapshot_id": None,
            "jobs_seen": 0,
            "jobs_selected": 0,
            "successful_submissions": 0,
            "error_count": 0,
            "last_error": None,
        },
        output_dir=output_dir,
    )


def write_output_json(
    relative_path: str | Path,
    payload: Mapping[str, object],
    *,
    output_dir: Path | None = None,
) -> None:
    """Persist one JSON payload into the current run output bundle."""

    target = _resolve_output_path(relative_path, output_dir=output_dir)
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            _sanitize_for_logs(dict(payload)),
            indent=2,
            ensure_ascii=True,
            default=_json_default,
        ),
        encoding="utf-8",
    )


def write_output_text(
    relative_path: str | Path,
    content: str,
    *,
    output_dir: Path | None = None,
) -> None:
    """Persist one text artifact into the current run output bundle."""

    target = _resolve_output_path(relative_path, output_dir=output_dir)
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def append_output_jsonl(
    relative_path: str | Path,
    payload: Mapping[str, object],
    *,
    output_dir: Path | None = None,
) -> None:
    """Append one structured record to a JSONL file in the current run bundle."""

    target = _resolve_output_path(relative_path, output_dir=output_dir)
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        _sanitize_for_logs(dict(payload)),
        ensure_ascii=True,
        default=_json_default,
    )
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def _resolve_output_path(relative_path: str | Path, *, output_dir: Path | None) -> Path | None:
    current_dir = output_dir or _run_output_dir_var.get()
    if current_dir is None:
        return None
    target = current_dir / Path(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


class LastRunDebugHandler(logging.Handler):
    """Mirror execution-bound logs into the `artifacts/last-run` bundle."""

    def __init__(self, target_path: Path) -> None:
        super().__init__(level=logging.NOTSET)
        self._target_path = target_path

    def emit(self, record: logging.LogRecord) -> None:
        output_dir = _run_output_dir_var.get()
        execution_id = getattr(record, "execution_id", None) or _execution_id_var.get()
        if output_dir is None or execution_id is None:
            return

        try:
            rendered = self.format(record)
            target_path = output_dir / self._target_path.name
            target_path.parent.mkdir(parents=True, exist_ok=True)
            self.acquire()
            try:
                with target_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{rendered}\n")
            finally:
                self.release()
        except Exception:  # noqa: BLE001
            self.handleError(record)


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
