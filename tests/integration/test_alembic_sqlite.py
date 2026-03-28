from pathlib import Path

from sqlalchemy import inspect

from job_applier.infrastructure.sqlite import create_sqlalchemy_engine
from tests.integration.sqlite_helpers import downgrade_to_base, upgrade_to_head


def test_alembic_upgrade_and_downgrade_work_with_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'alembic-test.db').resolve()}"

    upgrade_to_head(database_url)

    inspector = inspect(create_sqlalchemy_engine(database_url))
    assert {
        "job_postings",
        "application_submissions",
        "application_answers",
        "profile_snapshots",
        "recruiter_interactions",
        "execution_events",
        "artifact_snapshots",
    }.issubset(set(inspector.get_table_names()))

    downgrade_to_base(database_url)

    downgraded_inspector = inspect(create_sqlalchemy_engine(database_url))
    assert "job_postings" not in downgraded_inspector.get_table_names()
