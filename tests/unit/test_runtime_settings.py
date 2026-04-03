from pathlib import Path

from pydantic import AnyUrl, TypeAdapter

from job_applier.settings import (
    RuntimeSettings,
    initialize_runtime_environment,
    sqlite_path_from_url,
)


def test_runtime_settings_fall_back_to_local_sqlite(tmp_path: Path) -> None:
    settings = RuntimeSettings(data_dir=tmp_path / "runtime")
    expected_database_url = f"sqlite:///{(tmp_path / 'runtime' / 'job-applier.db').resolve()}"

    assert settings.resolved_panel_storage_dir == tmp_path / "runtime" / "panel"
    assert settings.output_dir == Path("artifacts/last-run")
    assert settings.resolved_database_url == expected_database_url
    assert settings.resolved_playwright_mcp_url == "http://localhost:8931/mcp"
    assert settings.resolved_playwright_mcp_stdio_command == (
        "npx",
        "-y",
        "@playwright/mcp@latest",
    )
    assert settings.linkedin_min_action_delay_ms == 350
    assert settings.linkedin_max_action_delay_ms == 950
    assert settings.linkedin_min_navigation_delay_ms == 800
    assert settings.linkedin_max_navigation_delay_ms == 1_800
    assert settings.resolved_openai_responses_max_retries == 2
    assert settings.resolved_browser_agent_single_action_max_attempts == 3
    assert settings.resolved_browser_agent_stall_threshold == 3
    assert settings.resolved_agent_max_selected_jobs_per_run is None
    assert settings.resolved_stagehand_cache_dir == tmp_path / "runtime" / "stagehand-cache"


def test_runtime_settings_test_mode_enables_cheaper_single_job_execution() -> None:
    settings = RuntimeSettings(
        agent_test_mode=True,
        agent_test_minimum_score_threshold=0.5,
        openai_responses_max_retries=4,
        browser_agent_single_action_max_attempts=5,
        browser_agent_stall_threshold=6,
    )

    assert settings.resolved_openai_responses_max_retries == 0
    assert settings.resolved_browser_agent_single_action_max_attempts == 5
    assert settings.resolved_browser_agent_stall_threshold == 6
    assert settings.resolved_agent_max_selected_jobs_per_run == 1
    assert settings.resolved_agent_test_minimum_score_threshold == 0.5


def test_runtime_settings_debug_target_forces_zero_score_threshold_in_test_mode() -> None:
    settings = RuntimeSettings(
        agent_test_mode=True,
        linkedin_debug_target_job_url=TypeAdapter(AnyUrl).validate_python(
            "https://www.linkedin.com/jobs/view/1234567890/"
        ),
        agent_test_minimum_score_threshold=0.5,
    )

    assert settings.resolved_agent_test_minimum_score_threshold == 0.0


def test_runtime_settings_normalize_playwright_mcp_root_url() -> None:
    url_adapter = TypeAdapter(AnyUrl)
    settings = RuntimeSettings(
        playwright_mcp_url=url_adapter.validate_python("http://127.0.0.1:8931")
    )

    assert settings.resolved_playwright_mcp_url == "http://localhost:8931/mcp"


def test_runtime_settings_parse_custom_playwright_mcp_stdio_command() -> None:
    settings = RuntimeSettings(
        playwright_mcp_stdio_command="npx @playwright/mcp@latest --browser chrome"
    )

    assert settings.resolved_playwright_mcp_stdio_command == (
        "npx",
        "@playwright/mcp@latest",
        "--browser",
        "chrome",
    )


def test_initialize_runtime_environment_creates_panel_dir_and_sqlite_file(tmp_path: Path) -> None:
    settings = RuntimeSettings(data_dir=tmp_path / "runtime")

    initialize_runtime_environment(settings)

    assert settings.resolved_panel_storage_dir.exists()
    assert settings.output_dir.exists()
    assert settings.resolved_stagehand_cache_dir.exists()
    sqlite_path = sqlite_path_from_url(settings.resolved_database_url)
    assert sqlite_path is not None
    assert sqlite_path.exists()
