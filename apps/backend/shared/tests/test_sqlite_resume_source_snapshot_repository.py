from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from job_applier.domain.entities import ResumeSourceSnapshotRecord
from job_applier.domain.enums import SupportedLanguage
from job_applier.infrastructure.sqlite import Base
from job_applier.infrastructure.sqlite.database import (
    create_session_factory,
    create_sqlalchemy_engine,
)
from job_applier.infrastructure.sqlite.repositories import SqliteResumeSourceSnapshotRepository


class SqliteResumeSourceSnapshotRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self._database_path = Path(self._temp_dir.name) / "resume-source-snapshots.db"
        self._database_url = f"sqlite:///{self._database_path}"
        engine = create_sqlalchemy_engine(self._database_url)
        Base.metadata.create_all(engine)
        self.repository = SqliteResumeSourceSnapshotRepository(
            create_session_factory(self._database_url)
        )

    def test_save_and_find_by_owner_and_cv_hash(self) -> None:
        now = datetime.now(UTC)
        record = ResumeSourceSnapshotRecord(
            id=uuid4(),
            owner_key="local-default",
            cv_sha256="abc123",
            source_cv_filename="resume.pdf",
            source_cv_path="/tmp/resume.pdf",
            source_resume_text="Resume body",
            source_resume_language=SupportedLanguage.PORTUGUESE,
            snapshot_origin="deterministic_v1",
            snapshot_json=json.dumps({"header_role": "Software Engineer"}),
            created_at=now,
            updated_at=now,
        )

        saved = self.repository.save(record)
        loaded = self.repository.find_by_owner_and_cv_sha256(
            owner_key="local-default",
            cv_sha256="abc123",
        )

        self.assertIsNotNone(loaded)
        self.assertEqual(saved.id, loaded.id)  # type: ignore[union-attr]
        self.assertEqual("resume.pdf", loaded.source_cv_filename)  # type: ignore[union-attr]
        self.assertEqual(
            SupportedLanguage.PORTUGUESE,
            loaded.source_resume_language,  # type: ignore[union-attr]
        )


if __name__ == "__main__":
    unittest.main()
