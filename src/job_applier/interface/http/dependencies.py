"""Shared FastAPI dependencies."""

from functools import lru_cache

from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore
from job_applier.settings import get_runtime_settings


@lru_cache(maxsize=1)
def get_panel_settings_store() -> LocalPanelSettingsStore:
    """Return the panel settings store singleton."""

    settings = get_runtime_settings()
    return LocalPanelSettingsStore(root_dir=settings.resolved_panel_storage_dir)
