"""Shared FastAPI dependencies."""

from functools import lru_cache

from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore


@lru_cache(maxsize=1)
def get_panel_settings_store() -> LocalPanelSettingsStore:
    """Return the panel settings store singleton."""

    return LocalPanelSettingsStore()
