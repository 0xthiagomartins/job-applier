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
    output_dir: Path = Path("artifacts/last-run")
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
    agent_test_mode: bool = False
    agent_max_selected_jobs_per_run: int | None = None
    agent_test_minimum_score_threshold: float | None = None
    playwright_headless: bool = False
    playwright_trace_enabled: bool = True
    playwright_display: str | None = Field(default=None, alias="DISPLAY")
    linkedin_email: str | None = None
    linkedin_password: SecretStr | None = None
    linkedin_storage_state_path: Path | None = None
    linkedin_artifacts_dir: Path | None = None
    linkedin_debug_target_job_url: AnyUrl | None = None
    linkedin_max_search_pages: int = 4
    linkedin_default_timeout_ms: int = 15_000
    linkedin_login_timeout_seconds: int = 120
    linkedin_min_action_delay_ms: int = 350
    linkedin_max_action_delay_ms: int = 950
    linkedin_min_navigation_delay_ms: int = 800
    linkedin_max_navigation_delay_ms: int = 1_800
    openai_api_key: SecretStr | None = None
    openai_responses_max_retries: int = 2
    openai_responses_retry_max_delay_seconds: float = 20.0
    browser_agent_single_action_max_attempts: int = 3
    browser_agent_stall_threshold: int = 3
    linkedin_field_interaction_timeout_seconds: int = 45
    bootstrap_panel_on_empty_state: bool = True
    bootstrap_profile_name: str | None = None
    bootstrap_profile_email: str | None = None
    bootstrap_profile_phone: str | None = None
    bootstrap_profile_city: str = "SAO PAULO - SP BRASIL"
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
    def resolved_agent_max_selected_jobs_per_run(self) -> int | None:
        """Return the selected-job cap for one run when a test limit is configured."""

        if self.agent_max_selected_jobs_per_run is not None:
            return max(1, self.agent_max_selected_jobs_per_run)
        if self.agent_test_mode:
            return 1
        return None

    @property
    def resolved_agent_test_minimum_score_threshold(self) -> float | None:
        """Return the score threshold override used only in agent test mode."""

        if not self.agent_test_mode:
            return None
        if self.resolved_linkedin_debug_target_job_url is not None:
            return 0.0
        if self.agent_test_minimum_score_threshold is None:
            return None
        return min(1.0, max(0.0, self.agent_test_minimum_score_threshold))

    @property
    def resolved_openai_responses_max_retries(self) -> int:
        """Return the effective OpenAI retry budget for the current runtime mode."""

        if self.agent_test_mode:
            return 0
        return max(0, self.openai_responses_max_retries)

    @property
    def resolved_browser_agent_single_action_max_attempts(self) -> int:
        """Return how many retries one browser micro-action may take."""

        return max(1, self.browser_agent_single_action_max_attempts)

    @property
    def resolved_browser_agent_stall_threshold(self) -> int:
        """Return how many unchanged snapshots trigger a stall diagnosis."""

        return max(2, self.browser_agent_stall_threshold)

    @property
    def resolved_linkedin_max_search_pages(self) -> int:
        """Return the effective LinkedIn pagination depth for the current runtime mode."""

        max_pages = max(1, self.linkedin_max_search_pages)
        if self.agent_test_mode:
            return min(max_pages, 2)
        return max_pages

    @property
    def resolved_linkedin_storage_state_path(self) -> Path:
        """Return the storage-state path used to reuse LinkedIn sessions."""

        return self.linkedin_storage_state_path or self.data_dir / "linkedin" / "storage-state.json"

    @property
    def resolved_linkedin_artifacts_dir(self) -> Path:
        """Return the directory used to store LinkedIn search screenshots."""

        return self.linkedin_artifacts_dir or self.data_dir / "artifacts" / "linkedin"

    @property
    def resolved_linkedin_debug_target_job_url(self) -> str | None:
        """Return an optional LinkedIn job URL used to bypass search during fast debugging."""

        if self.linkedin_debug_target_job_url is None:
            return None
        return str(self.linkedin_debug_target_job_url)


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
