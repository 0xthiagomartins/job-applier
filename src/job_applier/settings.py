"""Runtime settings for local and container execution."""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    """Settings that control local paths and on-premise runtime defaults."""

    data_dir: Path = Path("artifacts/runtime")
    panel_storage_dir: Path | None = None
    database_url: str | None = None
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    panel_port: int = 3000
    scheduler_poll_interval_seconds: int = 30
    playwright_headless: bool = False
    playwright_display: str | None = Field(default=None, alias="DISPLAY")
    linkedin_email: str | None = None
    linkedin_password: SecretStr | None = None
    linkedin_storage_state_path: Path | None = None
    linkedin_artifacts_dir: Path | None = None
    linkedin_max_search_pages: int = 2
    linkedin_default_timeout_ms: int = 15_000
    linkedin_login_timeout_seconds: int = 120

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="JOB_APPLIER_",
        extra="ignore",
        validate_default=True,
    )

    @property
    def resolved_panel_storage_dir(self) -> Path:
        """Return the path used to persist panel state."""

        return self.panel_storage_dir or self.data_dir / "panel"

    @property
    def resolved_database_url(self) -> str:
        """Return the configured database URL or the default local SQLite path."""

        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'job-applier.db').resolve()}"

    @property
    def resolved_linkedin_storage_state_path(self) -> Path:
        """Return the storage-state path used to reuse LinkedIn sessions."""

        return self.linkedin_storage_state_path or self.data_dir / "linkedin" / "storage-state.json"

    @property
    def resolved_linkedin_artifacts_dir(self) -> Path:
        """Return the directory used to store LinkedIn search screenshots."""

        return self.linkedin_artifacts_dir or self.data_dir / "artifacts" / "linkedin"


@lru_cache(maxsize=1)
def get_runtime_settings() -> RuntimeSettings:
    """Return the cached runtime settings."""

    return RuntimeSettings()


def initialize_runtime_environment(settings: RuntimeSettings) -> None:
    """Prepare local runtime directories and the default SQLite file."""

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_panel_storage_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_linkedin_storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.resolved_linkedin_artifacts_dir.mkdir(parents=True, exist_ok=True)

    sqlite_path = sqlite_path_from_url(settings.resolved_database_url)
    if sqlite_path is None:
        run_database_migrations(settings)
        return

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(sqlite_path).close()
    run_database_migrations(settings)


def sqlite_path_from_url(database_url: str) -> Path | None:
    """Extract a local filesystem path from a SQLite URL."""

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url.removeprefix(prefix))


def run_database_migrations(settings: RuntimeSettings) -> None:
    """Run Alembic migrations against the configured database."""

    project_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "alembic"))
    alembic_config.set_main_option("sqlalchemy.url", settings.resolved_database_url)
    command.upgrade(alembic_config, "head")
