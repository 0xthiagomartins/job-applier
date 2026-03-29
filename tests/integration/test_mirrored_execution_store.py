from pathlib import Path
from uuid import uuid4

from job_applier.domain import ExecutionEvent, ExecutionEventType
from job_applier.infrastructure.local_execution_store import MirroredExecutionStore
from job_applier.infrastructure.sqlite import create_session_factory
from job_applier.infrastructure.sqlite.repositories import SqliteExecutionEventRepository
from tests.integration.sqlite_helpers import upgrade_to_head


def test_mirrored_execution_store_persists_events_locally_and_in_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'execution-events.db').resolve()}"
    upgrade_to_head(database_url)

    session_factory = create_session_factory(database_url)
    event_repository = SqliteExecutionEventRepository(session_factory)
    execution_store = MirroredExecutionStore(
        event_repository=event_repository,
        root_dir=tmp_path / "executions",
    )

    execution_id = uuid4()
    event = ExecutionEvent(
        execution_id=execution_id,
        event_type=ExecutionEventType.EXECUTION_STARTED,
        payload_json='{"stage":"boot"}',
    )

    execution_store.append_event(event)

    local_events = execution_store.list_events(execution_id)
    sqlite_events = event_repository.list_for_execution(execution_id)

    assert len(local_events) == 1
    assert local_events[0]["event_type"] == ExecutionEventType.EXECUTION_STARTED.value
    assert sqlite_events == [event]
