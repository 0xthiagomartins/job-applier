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
    assert settings.resolved_database_url == expected_database_url
    assert settings.resolved_playwright_mcp_url == "http://localhost:8931/mcp"


def test_runtime_settings_normalize_playwright_mcp_root_url() -> None:
    url_adapter = TypeAdapter(AnyUrl)
    settings = RuntimeSettings(
        playwright_mcp_url=url_adapter.validate_python("http://127.0.0.1:8931")
    )

    assert settings.resolved_playwright_mcp_url == "http://localhost:8931/mcp"


def test_initialize_runtime_environment_creates_panel_dir_and_sqlite_file(tmp_path: Path) -> None:
    settings = RuntimeSettings(data_dir=tmp_path / "runtime")

    initialize_runtime_environment(settings)

    assert settings.resolved_panel_storage_dir.exists()
    sqlite_path = sqlite_path_from_url(settings.resolved_database_url)
    assert sqlite_path is not None
    assert sqlite_path.exists()
