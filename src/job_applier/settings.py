"""Runtime settings for local and container execution."""

from __future__ import annotations

import shlex
import sqlite3
from functools import lru_cache
from pathlib import Path
from urllib import parse

from alembic import command
from alembic.config import Config
from pydantic import AnyUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    """Settings that control local paths and on-premise runtime defaults."""

    data_dir: Path = Path("artifacts/runtime")
    output_dir: Path = Path("output")
    panel_storage_dir: Path | None = None
    database_url: str | None = None
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    panel_port: int = 3000
    playwright_mcp_url: AnyUrl | None = None
    playwright_mcp_host: str = "localhost"
    playwright_mcp_port: int = 8931
    playwright_mcp_prefer_stdio_for_local: bool = True
    playwright_mcp_stdio_command: str | None = None
    scheduler_poll_interval_seconds: int = 30
    playwright_headless: bool = False
    playwright_trace_enabled: bool = True
    playwright_display: str | None = Field(default=None, alias="DISPLAY")
    linkedin_email: str | None = None
    linkedin_password: SecretStr | None = None
    linkedin_storage_state_path: Path | None = None
    linkedin_artifacts_dir: Path | None = None
    linkedin_max_search_pages: int = 2
    linkedin_default_timeout_ms: int = 15_000
    linkedin_login_timeout_seconds: int = 120
    linkedin_min_action_delay_ms: int = 350
    linkedin_max_action_delay_ms: int = 950
    linkedin_min_navigation_delay_ms: int = 800
    linkedin_max_navigation_delay_ms: int = 1_800
    openai_api_key: SecretStr | None = None
    bootstrap_panel_on_empty_state: bool = True
    bootstrap_profile_name: str | None = None
    bootstrap_profile_email: str | None = None
    bootstrap_profile_phone: str | None = None
    bootstrap_profile_city: str = "Sao Paulo"
    bootstrap_profile_linkedin_url: AnyUrl | None = None
    bootstrap_profile_github_url: AnyUrl | None = None
    bootstrap_profile_portfolio_url: AnyUrl | None = None
    bootstrap_profile_cv_path: Path | None = None
    bootstrap_profile_availability: str = "Immediate"
    bootstrap_profile_work_authorized: bool = True
    bootstrap_profile_needs_sponsorship: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    log_file_path: Path | None = None

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
    def resolved_playwright_mcp_url(self) -> str:
        """Return the external Playwright MCP URL or the default local sidecar address."""

        raw_url = (
            str(self.playwright_mcp_url)
            if self.playwright_mcp_url is not None
            else f"http://{self.playwright_mcp_host}:{self.playwright_mcp_port}"
        )
        parsed = parse.urlparse(raw_url)
        hostname = parsed.hostname or ""
        if hostname in {"127.0.0.1", "0.0.0.0"}:
            host = "localhost"
            if parsed.port is not None:
                netloc = f"{host}:{parsed.port}"
            else:
                netloc = host
            parsed = parsed._replace(netloc=netloc)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/mcp"
        elif path not in {"/mcp", "/sse"}:
            path = f"{path}/mcp"
        return parse.urlunparse(parsed._replace(path=path, params="", query="", fragment=""))

    @property
    def resolved_playwright_mcp_stdio_command(self) -> tuple[str, ...]:
        """Return the local stdio command used for Playwright MCP subprocess mode."""

        raw_value = self.playwright_mcp_stdio_command or "npx -y @playwright/mcp@latest"
        return tuple(part for part in shlex.split(raw_value) if part)

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
    settings.output_dir.mkdir(parents=True, exist_ok=True)
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
