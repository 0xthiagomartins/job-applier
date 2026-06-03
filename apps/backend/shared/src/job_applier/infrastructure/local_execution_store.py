"""Local file-backed storage for agent execution summaries and events."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from job_applier.application.agent_execution import ExecutionRunSummary
from job_applier.application.panel import ensure_runtime_dir
from job_applier.application.repositories import ExecutionEventRepository
from job_applier.domain.entities import ExecutionEvent


class ExecutionEventEnvelope(BaseModel):
    """Serializable wrapper around the domain execution event."""

    id: UUID
    execution_id: UUID
    submission_id: UUID | None = None
    event_type: str
    timestamp: str
    payload_json: str

    @classmethod
    def from_domain(cls, event: ExecutionEvent) -> ExecutionEventEnvelope:
        """Convert the domain event into a serializable envelope."""

        return cls(
            id=event.id,
            execution_id=event.execution_id,
            submission_id=event.submission_id,
            event_type=event.event_type.value,
            timestamp=event.timestamp.isoformat(),
            payload_json=event.payload_json,
        )


class ExecutionStoreDocument(BaseModel):
    """Persisted execution state document."""

    executions: list[ExecutionRunSummary] = Field(default_factory=list)
    events: list[ExecutionEventEnvelope] = Field(default_factory=list)


class LocalExecutionStore:
    """Persist execution summaries and events in a local gitignored file."""

    def __init__(self, root_dir: Path | None = None) -> None:
        base_dir = ensure_runtime_dir(root_dir or Path("artifacts/runtime/executions"))
        self._state_path = base_dir / "execution-state.json"
        self._write_lock = Lock()

    def save_execution(self, summary: ExecutionRunSummary) -> None:
        """Persist a new or updated execution summary."""

        with self._write_lock:
            document = self._load_document()
            executions = [
                item for item in document.executions if item.execution_id != summary.execution_id
            ]
            executions.append(summary)
            updated_document = document.model_copy(update={"executions": executions})
            self._write_document(updated_document)

    def append_event(self, event: ExecutionEvent) -> None:
        """Persist a new execution event."""

        with self._write_lock:
            document = self._load_document()
            events = [*document.events, ExecutionEventEnvelope.from_domain(event)]
            updated_document = document.model_copy(update={"events": events})
            self._write_document(updated_document)

    def list_recent_executions(self, *, limit: int = 10) -> list[ExecutionRunSummary]:
        """Return recent executions in reverse chronological order."""

        document = self._load_document()
        ordered = sorted(document.executions, key=lambda item: item.started_at, reverse=True)
        return ordered[:limit]

    def get_execution(self, execution_id: UUID) -> ExecutionRunSummary | None:
        """Return a single execution summary when present."""

        document = self._load_document()
        for summary in document.executions:
            if summary.execution_id == execution_id:
                return summary
        return None

    def list_events(self, execution_id: UUID) -> list[dict[str, Any]]:
        """Return events for one execution."""

        document = self._load_document()
        return [
            event.model_dump(mode="json")
            for event in document.events
            if event.execution_id == execution_id
        ]

    def _load_document(self) -> ExecutionStoreDocument:
        """Load the persisted execution state or return defaults."""

        if not self._state_path.exists():
            return ExecutionStoreDocument()
        payload = self._state_path.read_text(encoding="utf-8")
        return ExecutionStoreDocument.model_validate_json(payload)

    def _write_document(self, document: ExecutionStoreDocument) -> None:
        """Write the updated execution state atomically."""

        payload = document.model_dump(mode="json")
        temp_path = self._state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self._state_path)


class MirroredExecutionStore(LocalExecutionStore):
    """Mirror execution events to SQLite while preserving the local debug file."""

    def __init__(
        self,
        *,
        event_repository: ExecutionEventRepository,
        root_dir: Path | None = None,
    ) -> None:
        super().__init__(root_dir=root_dir)
        self._event_repository = event_repository

    def append_event(self, event: ExecutionEvent) -> None:
        """Persist the event locally and in the relational event store."""

        super().append_event(event)
        self._event_repository.save(event)
