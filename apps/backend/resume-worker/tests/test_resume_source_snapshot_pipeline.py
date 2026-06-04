from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_applier.application.config import (
    AgentConfig,
    AIConfig,
    RulesetConfig,
    ScheduleConfig,
    SearchConfig,
    UserAgentSettings,
    UserProfileConfig,
)
from job_applier.domain.enums import ResumeMode, SupportedLanguage
from job_applier.infrastructure.resume_dynamic import OhMyCvDynamicResumeBuilder
from job_applier.infrastructure.sqlite import Base
from job_applier.infrastructure.sqlite.database import (
    create_session_factory,
    create_sqlalchemy_engine,
)
from job_applier.infrastructure.sqlite.repositories import SqliteResumeSourceSnapshotRepository
from job_applier.settings import RuntimeSettings


class _CountingResumeBuilder(OhMyCvDynamicResumeBuilder):
    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.extract_calls = 0

    def _extract_resume_text(self, source_cv_path: Path) -> str | None:
        self.extract_calls += 1
        return super()._extract_resume_text(source_cv_path)


def _build_settings(cv_path: Path) -> UserAgentSettings:
    return UserAgentSettings(
        profile=UserProfileConfig(
            name="Thiago",
            email="thiago@example.com",
            phone="+5511999999999",
            city="Sao Paulo",
            work_authorized=True,
            needs_sponsorship=False,
            availability="Immediate",
            cv_path=str(cv_path),
            cv_filename=cv_path.name,
            resume_mode=ResumeMode.DYNAMIC,
            preferred_language=SupportedLanguage.ENGLISH,
        ),
        search=SearchConfig(
            keywords=("Software Engineer",),
            location="Remote",
        ),
        agent=AgentConfig(schedule=ScheduleConfig()),
        ai=AIConfig(api_key=None, model="o3-mini"),
        ruleset=RulesetConfig(),
    )


class ResumeSourceSnapshotPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        temp_root = Path(self._temp_dir.name)
        self.resume_path = temp_root / "resume.txt"
        self.resume_path.write_text(
            "\n".join(
                (
                    "Thiago Martins",
                    "Full Stack Software Engineer",
                    "Summary",
                    "Software engineer focused on backend systems and automation.",
                    "Experience",
                    "Senior Software Engineer | ACME | 2022 - Present",
                    "- Built Java and Python APIs",
                    "- Automated internal workflows",
                    "Skills",
                    "Backend: Java, Python, FastAPI",
                )
            ),
            encoding="utf-8",
        )
        database_path = temp_root / "resume-snapshot.db"
        database_url = f"sqlite:///{database_path}"
        engine = create_sqlalchemy_engine(database_url)
        Base.metadata.create_all(engine)
        self.repository = SqliteResumeSourceSnapshotRepository(create_session_factory(database_url))
        self.runtime_settings = RuntimeSettings(
            data_dir=temp_root / "runtime",
            output_dir=temp_root / "last-run",
            database_url=database_url,
            local_owner_key="local-default",
            resume_dynamic_enabled=True,
        )

    def test_get_or_create_source_snapshot_reuses_persisted_snapshot(self) -> None:
        builder = _CountingResumeBuilder(
            self.runtime_settings,
            resume_source_snapshot_repository=self.repository,
        )
        settings = _build_settings(self.resume_path)

        first = builder.get_or_create_source_snapshot(settings=settings)
        second = builder.get_or_create_source_snapshot(settings=settings)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(1, builder.extract_calls)
        self.assertEqual(first.cv_sha256, second.cv_sha256)  # type: ignore[union-attr]
        self.assertEqual(
            "Full Stack Software Engineer",
            second.snapshot.header_role,  # type: ignore[union-attr]
        )

    def test_update_source_snapshot_persists_user_edited_record(self) -> None:
        builder = _CountingResumeBuilder(
            self.runtime_settings,
            resume_source_snapshot_repository=self.repository,
        )
        settings = _build_settings(self.resume_path)
        existing = builder.get_or_create_source_snapshot(settings=settings)
        if existing is None:
            self.fail("expected canonical snapshot to be created")

        updated = builder.update_source_snapshot(
            settings=settings,
            snapshot_payload={
                "header_role": "Backend Software Engineer",
                "summary": "Backend-focused engineer.",
                "experience_entries": [],
                "certifications": [],
                "education_entries": [],
                "skill_lines": ["Backend: Java, Python"],
                "additional_sections": [],
                "word_count": 42,
                "phone": existing.snapshot.phone,
                "email": existing.snapshot.email,
                "city": existing.snapshot.city,
                "portfolio_hint": existing.snapshot.portfolio_hint,
            },
            source_resume_language=SupportedLanguage.ENGLISH,
        )

        self.assertIsNotNone(updated)
        self.assertTrue(updated.user_edited)  # type: ignore[union-attr]
        self.assertEqual(
            "Backend Software Engineer",
            updated.snapshot.header_role,  # type: ignore[union-attr]
        )


if __name__ == "__main__":
    unittest.main()
