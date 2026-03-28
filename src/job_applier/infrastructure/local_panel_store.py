"""Local file-backed persistence used by the MVP panel."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from job_applier.application.panel import (
    AIFormInput,
    PanelSettingsDocument,
    PreferencesFormInput,
    ProfileFormInput,
    ScheduleFormInput,
    StoredAISection,
    StoredPreferencesSection,
    StoredProfileSection,
    StoredScheduleSection,
    ensure_runtime_dir,
)


class LocalPanelSettingsStore:
    """Persist panel settings in gitignored local files."""

    def __init__(self, root_dir: Path | None = None) -> None:
        base_dir = root_dir or Path("artifacts/panel")
        self._base_dir = ensure_runtime_dir(base_dir)
        self._cv_dir = ensure_runtime_dir(self._base_dir / "cv")
        self._state_path = self._base_dir / "settings.json"

    def load(self) -> PanelSettingsDocument:
        """Load the persisted panel document or return defaults."""

        if not self._state_path.exists():
            return PanelSettingsDocument()
        payload = self._state_path.read_text(encoding="utf-8")
        return PanelSettingsDocument.model_validate_json(payload)

    def save_profile(
        self,
        profile_input: ProfileFormInput,
        *,
        cv_upload: UploadFile | None = None,
    ) -> PanelSettingsDocument:
        """Persist the profile section and optional CV upload."""

        document = self.load()
        current_profile = document.profile
        cv_path = current_profile.cv_path
        cv_filename = current_profile.cv_filename

        if cv_upload is not None and cv_upload.filename:
            suffix = Path(cv_upload.filename).suffix
            stored_filename = f"{uuid4().hex}{suffix}"
            destination = self._cv_dir / stored_filename
            file_bytes = cv_upload.file.read()
            destination.write_bytes(file_bytes)
            cv_path = str(destination)
            cv_filename = cv_upload.filename

        updated_document = document.model_copy(
            update={
                "profile": StoredProfileSection(
                    **profile_input.model_dump(),
                    cv_path=cv_path,
                    cv_filename=cv_filename,
                ),
            },
        )
        self._write(updated_document)
        return updated_document

    def save_preferences(self, preferences_input: PreferencesFormInput) -> PanelSettingsDocument:
        """Persist search filters and user preferences."""

        document = self.load()
        updated_document = document.model_copy(
            update={
                "preferences": StoredPreferencesSection(**preferences_input.model_dump()),
            },
        )
        self._write(updated_document)
        return updated_document

    def save_ai(self, ai_input: AIFormInput) -> PanelSettingsDocument:
        """Persist AI settings while keeping an existing key when omitted."""

        document = self.load()
        existing_key = document.ai.api_key
        api_key = ai_input.api_key or existing_key
        updated_document = document.model_copy(
            update={
                "ai": StoredAISection(api_key=api_key, model=ai_input.model),
            },
        )
        self._write(updated_document)
        return updated_document

    def save_schedule(self, schedule_input: ScheduleFormInput) -> PanelSettingsDocument:
        """Persist the execution schedule used by the agent."""

        document = self.load()
        updated_document = document.model_copy(
            update={
                "schedule": StoredScheduleSection(**schedule_input.model_dump()),
            },
        )
        self._write(updated_document)
        return updated_document

    def _write(self, document: PanelSettingsDocument) -> None:
        """Write the full panel document atomically."""

        payload = document.model_dump(mode="json")
        temp_path = self._state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self._state_path)
