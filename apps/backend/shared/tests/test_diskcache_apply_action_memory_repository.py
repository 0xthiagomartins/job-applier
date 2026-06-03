from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from job_applier.domain.entities import ApplyActionMemory
from job_applier.infrastructure.cache import DiskCacheApplyActionMemoryRepository


class DiskCacheApplyActionMemoryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.repository = DiskCacheApplyActionMemoryRepository(Path(self._temp_dir.name))

    def test_save_replaces_existing_signature_and_prunes_expired(self) -> None:
        now = datetime.now(tz=UTC)
        first = ApplyActionMemory(
            id=uuid4(),
            task_type="linkedin_easy_apply_primary_action",
            signature_hash="same-signature",
            signature_json=json.dumps({"step": 1}),
            strategy_payload_json=json.dumps({"action_type": "click"}),
            success_count=1,
            created_at=now,
            last_used_at=now,
            last_succeeded_at=now,
            expires_at=now + timedelta(days=30),
        )
        second = ApplyActionMemory(
            id=uuid4(),
            task_type="linkedin_easy_apply_primary_action",
            signature_hash="same-signature",
            signature_json=json.dumps({"step": 1}),
            strategy_payload_json=json.dumps({"action_type": "press"}),
            success_count=3,
            created_at=now,
            last_used_at=now,
            last_succeeded_at=now,
            expires_at=now + timedelta(days=30),
        )
        expired = ApplyActionMemory(
            id=uuid4(),
            task_type="linkedin_easy_apply_finalize_field_interaction",
            signature_hash="expired-signature",
            signature_json=json.dumps({"step": 9}),
            strategy_payload_json=json.dumps({"action_type": "fill"}),
            created_at=now - timedelta(days=60),
            expires_at=now - timedelta(seconds=1),
        )

        self.repository.save(first)
        self.repository.save(second)
        self.repository.save(expired)

        self.assertIsNone(self.repository.get(first.id))
        resolved = self.repository.find_by_task_signature(
            task_type=second.task_type,
            signature_hash=second.signature_hash,
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, second.id)  # type: ignore[union-attr]
        self.assertEqual(resolved.strategy_payload_json, second.strategy_payload_json)  # type: ignore[union-attr]

        deleted = self.repository.delete_expired(now=now)
        self.assertEqual(deleted, 1)
        self.assertIsNone(
            self.repository.find_active_by_task_signature(
                task_type=expired.task_type,
                signature_hash=expired.signature_hash,
                now=now,
            )
        )


if __name__ == "__main__":
    unittest.main()
