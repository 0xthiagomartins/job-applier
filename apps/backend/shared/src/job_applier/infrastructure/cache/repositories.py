"""DiskCache-backed repository implementations."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from diskcache import Cache  # type: ignore[import-untyped]

from job_applier.application.repositories import ApplyActionMemoryRepository
from job_applier.domain.entities import ApplyActionMemory


def _ensure_utc(value: datetime) -> datetime:
    """Normalize timestamps restored from cache into aware UTC datetimes."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _serialize_datetime(value: datetime | None) -> str | None:
    """Serialize one optional timestamp for portable cache storage."""

    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _deserialize_datetime(value: str | None) -> datetime | None:
    """Deserialize one optional ISO timestamp from cache storage."""

    if value is None:
        return None
    return _ensure_utc(datetime.fromisoformat(value))


class DiskCacheApplyActionMemoryRepository(ApplyActionMemoryRepository):
    """Persist adaptive Easy Apply memory in a dedicated disk cache directory."""

    _ENTRY_PREFIX = "apply-memory:entry:"
    _ID_PREFIX = "apply-memory:id:"

    def __init__(self, cache_dir: Path) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache = Cache(str(cache_dir))

    def save(self, entity: ApplyActionMemory) -> ApplyActionMemory:
        entry_key = self._entry_key(entity.task_type, entity.signature_hash)
        payload = self._serialize_entity(entity)
        ttl_seconds = self._ttl_seconds(entity.expires_at)

        existing = self.find_by_task_signature(
            task_type=entity.task_type,
            signature_hash=entity.signature_hash,
        )
        if existing is not None and existing.id != entity.id:
            self.delete(existing.id)

        previous_entry_key = self._cache.get(self._id_key(entity.id))
        if isinstance(previous_entry_key, str) and previous_entry_key != entry_key:
            self._cache.delete(previous_entry_key)

        self._cache.set(entry_key, payload, expire=ttl_seconds)
        self._cache.set(self._id_key(entity.id), entry_key, expire=ttl_seconds)
        return entity

    def get(self, entity_id: UUID) -> ApplyActionMemory | None:
        entry_key = self._cache.get(self._id_key(entity_id))
        if not isinstance(entry_key, str):
            return None
        payload = self._cache.get(entry_key)
        if not isinstance(payload, dict):
            self._cache.delete(self._id_key(entity_id))
            return None
        return self._deserialize_entity(payload)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[ApplyActionMemory]:
        entities = [
            entity
            for entity in (
                self._deserialize_entity(payload) for payload in self._iter_entry_payloads()
            )
            if entity is not None
        ]
        entities.sort(
            key=lambda item: (
                item.last_succeeded_at or item.created_at,
                item.created_at,
                item.id,
            ),
            reverse=True,
        )
        return entities[offset : offset + limit]

    def delete(self, entity_id: UUID) -> None:
        entry_key = self._cache.get(self._id_key(entity_id))
        self._cache.delete(self._id_key(entity_id))
        if isinstance(entry_key, str):
            self._cache.delete(entry_key)

    def find_active_by_task_signature(
        self,
        *,
        task_type: str,
        signature_hash: str,
        now: datetime,
    ) -> ApplyActionMemory | None:
        payload = self._cache.get(self._entry_key(task_type, signature_hash))
        if not isinstance(payload, dict):
            return None
        entity = self._deserialize_entity(payload)
        if entity is None:
            return None
        if entity.expires_at <= now:
            self.delete(entity.id)
            return None
        return entity

    def delete_expired(self, *, now: datetime) -> int:
        deleted = 0
        for payload in self._iter_entry_payloads():
            entity = self._deserialize_entity(payload)
            if entity is None:
                continue
            if entity.expires_at <= now:
                self.delete(entity.id)
                deleted += 1
        return deleted

    def find_by_task_signature(
        self,
        *,
        task_type: str,
        signature_hash: str,
    ) -> ApplyActionMemory | None:
        payload = self._cache.get(self._entry_key(task_type, signature_hash))
        if not isinstance(payload, dict):
            return None
        return self._deserialize_entity(payload)

    def _iter_entry_payloads(self) -> builtins.list[dict[str, Any]]:
        payloads: builtins.list[dict[str, Any]] = []
        for key in self._cache:
            if not isinstance(key, str) or not key.startswith(self._ENTRY_PREFIX):
                continue
            payload = self._cache.get(key)
            if isinstance(payload, dict):
                payloads.append(payload)
        return payloads

    def _entry_key(self, task_type: str, signature_hash: str) -> str:
        return f"{self._ENTRY_PREFIX}{task_type}:{signature_hash}"

    def _id_key(self, entity_id: UUID) -> str:
        return f"{self._ID_PREFIX}{entity_id}"

    def _ttl_seconds(self, expires_at: datetime) -> int:
        remaining_seconds = int((expires_at - datetime.now(tz=UTC)).total_seconds())
        return max(1, remaining_seconds)

    def _serialize_entity(self, entity: ApplyActionMemory) -> dict[str, Any]:
        return {
            "id": str(entity.id),
            "task_type": entity.task_type,
            "signature_hash": entity.signature_hash,
            "signature_json": entity.signature_json,
            "strategy_payload_json": entity.strategy_payload_json,
            "success_count": entity.success_count,
            "failure_count": entity.failure_count,
            "created_at": _serialize_datetime(entity.created_at),
            "last_used_at": _serialize_datetime(entity.last_used_at),
            "last_succeeded_at": _serialize_datetime(entity.last_succeeded_at),
            "expires_at": _serialize_datetime(entity.expires_at),
        }

    def _deserialize_entity(self, payload: dict[str, Any]) -> ApplyActionMemory | None:
        created_at = _deserialize_datetime(payload.get("created_at"))
        expires_at = _deserialize_datetime(payload.get("expires_at"))
        if created_at is None or expires_at is None:
            return None
        return ApplyActionMemory(
            id=UUID(str(payload["id"])),
            task_type=str(payload["task_type"]),
            signature_hash=str(payload["signature_hash"]),
            signature_json=str(payload["signature_json"]),
            strategy_payload_json=str(payload["strategy_payload_json"]),
            success_count=int(payload.get("success_count", 0)),
            failure_count=int(payload.get("failure_count", 0)),
            created_at=created_at,
            last_used_at=_deserialize_datetime(payload.get("last_used_at")),
            last_succeeded_at=_deserialize_datetime(payload.get("last_succeeded_at")),
            expires_at=expires_at,
        )
