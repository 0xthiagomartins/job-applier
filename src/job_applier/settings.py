"""Runtime settings for local and container execution."""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from pydantic import Field
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


@lru_cache(maxsize=1)
def get_runtime_settings() -> RuntimeSettings:
    """Return the cached runtime settings."""

    return RuntimeSettings()


def initialize_runtime_environment(settings: RuntimeSettings) -> None:
    """Prepare local runtime directories and the default SQLite file."""

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_panel_storage_dir.mkdir(parents=True, exist_ok=True)

    sqlite_path = sqlite_path_from_url(settings.resolved_database_url)
    if sqlite_path is None:
        return

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(sqlite_path).close()


def sqlite_path_from_url(database_url: str) -> Path | None:
    """Extract a local filesystem path from a SQLite URL."""

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url.removeprefix(prefix))
