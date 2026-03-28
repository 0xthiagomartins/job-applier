"""Local file-backed persistence used by the MVP panel."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from pydantic import SecretStr

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
from job_applier.settings import RuntimeSettings


class LocalPanelSettingsStore:
    """Persist panel settings in gitignored local files."""

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        runtime_settings: RuntimeSettings | None = None,
    ) -> None:
        base_dir = root_dir or Path("artifacts/panel")
        self._base_dir = ensure_runtime_dir(base_dir)
        self._cv_dir = ensure_runtime_dir(self._base_dir / "cv")
        self._state_path = self._base_dir / "settings.json"
        self._runtime_settings = runtime_settings

    def load(self) -> PanelSettingsDocument:
        """Load the persisted panel document or return defaults."""

        if not self._state_path.exists():
            bootstrap_document = self._build_bootstrap_document()
            if bootstrap_document is not None:
                self._write(bootstrap_document)
                return bootstrap_document
            return PanelSettingsDocument()
        payload = self._state_path.read_text(encoding="utf-8")
        document = PanelSettingsDocument.model_validate_json(payload)
        merged_document = self._merge_bootstrap_document(document)
        if merged_document is not None:
            self._write(merged_document)
            return merged_document
        return document

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
        if document.ai.api_key is not None:
            payload["ai"]["api_key"] = document.ai.api_key.get_secret_value()
        temp_path = self._state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self._state_path)

    def _build_bootstrap_document(self) -> PanelSettingsDocument | None:
        """Build a local bootstrap document when the panel state is still empty."""

        settings = self._runtime_settings
        if settings is None or not settings.bootstrap_panel_on_empty_state:
            return None

        cv_path = self._resolve_bootstrap_cv_path(settings)
        copied_cv_path = self._copy_bootstrap_cv(cv_path) if cv_path else None
        email = settings.bootstrap_profile_email or settings.linkedin_email
        inferred_name = _infer_name(
            explicit_name=settings.bootstrap_profile_name,
            email=email,
            cv_path=cv_path,
        )

        return PanelSettingsDocument(
            profile=StoredProfileSection(
                name=inferred_name,
                email=email,
                phone=settings.bootstrap_profile_phone or "",
                city=settings.bootstrap_profile_city,
                linkedin_url=settings.bootstrap_profile_linkedin_url,
                github_url=settings.bootstrap_profile_github_url,
                portfolio_url=settings.bootstrap_profile_portfolio_url,
                years_experience_by_stack={},
                work_authorized=settings.bootstrap_profile_work_authorized,
                needs_sponsorship=settings.bootstrap_profile_needs_sponsorship,
                availability=settings.bootstrap_profile_availability,
                default_responses={
                    "work_authorization": "Yes",
                    "visa_sponsorship": "No",
                },
                cv_path=str(copied_cv_path) if copied_cv_path else None,
                cv_filename=cv_path.name if cv_path else None,
            ),
            preferences=StoredPreferencesSection(
                keywords=("python", "automation"),
                location="Remote",
                posted_within_hours=24,
                easy_apply_only=True,
                positive_keywords=("python",),
                negative_keywords=("internship",),
                auto_connect_with_recruiter=False,
            ),
            ai=StoredAISection(
                api_key=self._resolve_bootstrap_ai_key(settings),
                model="o3-mini",
            ),
            schedule=StoredScheduleSection(
                frequency=StoredScheduleSection().frequency,
                run_at="23:00",
                timezone="America/Sao_Paulo",
            ),
        )

    def _merge_bootstrap_document(
        self,
        document: PanelSettingsDocument,
    ) -> PanelSettingsDocument | None:
        """Fill still-empty onboarding fields from the local bootstrap defaults."""

        bootstrap_document = self._build_bootstrap_document()
        if bootstrap_document is None:
            return None

        merged_document = document.model_copy(
            update={
                "profile": document.profile.model_copy(
                    update={
                        "name": document.profile.name or bootstrap_document.profile.name,
                        "email": document.profile.email or bootstrap_document.profile.email,
                        "phone": document.profile.phone or bootstrap_document.profile.phone,
                        "city": document.profile.city or bootstrap_document.profile.city,
                        "linkedin_url": (
                            document.profile.linkedin_url or bootstrap_document.profile.linkedin_url
                        ),
                        "github_url": (
                            document.profile.github_url or bootstrap_document.profile.github_url
                        ),
                        "portfolio_url": (
                            document.profile.portfolio_url
                            or bootstrap_document.profile.portfolio_url
                        ),
                        "availability": (
                            document.profile.availability or bootstrap_document.profile.availability
                        ),
                        "default_responses": (
                            document.profile.default_responses
                            or bootstrap_document.profile.default_responses
                        ),
                        "cv_path": document.profile.cv_path or bootstrap_document.profile.cv_path,
                        "cv_filename": (
                            document.profile.cv_filename or bootstrap_document.profile.cv_filename
                        ),
                    },
                ),
                "preferences": document.preferences.model_copy(
                    update={
                        "keywords": document.preferences.keywords
                        or bootstrap_document.preferences.keywords,
                        "location": document.preferences.location
                        or bootstrap_document.preferences.location,
                        "positive_keywords": (
                            document.preferences.positive_keywords
                            or bootstrap_document.preferences.positive_keywords
                        ),
                        "negative_keywords": (
                            document.preferences.negative_keywords
                            or bootstrap_document.preferences.negative_keywords
                        ),
                    },
                ),
                "ai": document.ai.model_copy(
                    update={
                        "api_key": document.ai.api_key or bootstrap_document.ai.api_key,
                        "model": document.ai.model or bootstrap_document.ai.model,
                    },
                ),
                "schedule": document.schedule.model_copy(
                    update={
                        "timezone": document.schedule.timezone
                        or bootstrap_document.schedule.timezone,
                    },
                ),
            },
        )
        if merged_document.model_dump(mode="json") == document.model_dump(mode="json"):
            return None
        return merged_document

    def _resolve_bootstrap_cv_path(self, settings: RuntimeSettings) -> Path | None:
        """Return the bootstrap CV file, preferring explicit env configuration."""

        explicit = settings.bootstrap_profile_cv_path
        if explicit and explicit.exists() and explicit.is_file():
            return explicit
        return _detect_default_cv_path()

    def _copy_bootstrap_cv(self, source_path: Path) -> Path:
        """Copy a detected CV into the managed runtime directory."""

        destination_name = f"bootstrap-{source_path.name}"
        destination = self._cv_dir / destination_name
        if not destination.exists():
            shutil.copy2(source_path, destination)
        return destination

    def _resolve_bootstrap_ai_key(self, settings: RuntimeSettings) -> SecretStr | None:
        """Return the AI key used for local bootstrap, checking common env names."""

        if settings.openai_api_key is not None:
            return settings.openai_api_key
        raw_value = os.getenv("OPENAI_API_KEY") or os.getenv("JOB_APPLIER_OPENAI_API_KEY")
        if raw_value:
            return SecretStr(raw_value)
        return None


def _detect_default_cv_path() -> Path | None:
    """Try to find a likely CV/resume file inside the user's Documents folder."""

    documents_dir = Path.home() / "Documents"
    if not documents_dir.exists():
        return None

    supported_suffixes = {".pdf", ".doc", ".docx"}
    candidates = [
        path
        for path in documents_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in supported_suffixes
    ]
    ranked = [(score, path) for path in candidates if (score := _score_cv_candidate(path)) > 0]
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1].stat().st_mtime), reverse=True)
    return ranked[0][1]


def _score_cv_candidate(path: Path) -> int:
    """Score one Documents file as a probable CV/resume candidate."""

    name = path.name.lower()
    score = 0
    if "thiago martins" in name:
        score += 10
    if "cv" in name:
        score += 7
    if "resume" in name:
        score += 6
    if "curriculo" in name or "curriculum" in name:
        score += 5
    if "2026" in name:
        score += 9
    if re.search(r"\b20\d{2}\b", name):
        score += 2
    return score


def _infer_name(
    *,
    explicit_name: str | None,
    email: str | None,
    cv_path: Path | None,
) -> str:
    """Infer a friendly profile name from local bootstrap sources."""

    if explicit_name and explicit_name.strip():
        return explicit_name.strip()
    if cv_path is not None:
        stem = cv_path.stem
        stem = re.sub(
            r"\s*-\s*(cv|resume|curriculo|curriculum)\s*20\d{2}\s*$", "", stem, flags=re.I
        )
        stem = re.sub(r"\s*-\s*(cv|resume|curriculo|curriculum)\s*$", "", stem, flags=re.I)
        cleaned = stem.strip()
        if cleaned:
            return cleaned
    if email and "@" in email:
        local_part = email.split("@", maxsplit=1)[0]
        return local_part.replace(".", " ").replace("_", " ").title()
    return "Candidate"
