"""Panel-facing schemas and helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
)

from job_applier.domain.enums import SeniorityLevel, WorkplaceType

MODEL_OPTIONS = ("o3-mini", "gpt-4.1-mini", "gpt-4o-mini")


class PanelModel(BaseModel):
    """Base model used for persisted panel sections."""

    model_config = ConfigDict(frozen=True)


def parse_csv_lines(raw: str) -> tuple[str, ...]:
    """Parse comma-separated or multi-line text into normalized tuples."""

    values: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        candidate = chunk.strip()
        if candidate:
            values.append(candidate)
    return tuple(values)


def parse_mapping_lines(
    raw: str,
    *,
    value_type: type[int] | type[str],
) -> dict[str, int] | dict[str, str]:
    """Parse `key=value` or `key:value` lines into dictionaries."""

    parsed: dict[str, int] | dict[str, str] = {}
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        separator = "=" if "=" in candidate else ":"
        if separator not in candidate:
            msg = "Expected one key/value pair per line using `=` or `:`"
            raise ValueError(msg)
        key, value = (item.strip() for item in candidate.split(separator, maxsplit=1))
        if not key or not value:
            msg = "Each key/value line must have both key and value"
            raise ValueError(msg)
        parsed[key] = value_type(value) if value_type is int else value  # type: ignore[assignment]
    return parsed


def parse_int_mapping_lines(raw: str) -> dict[str, int]:
    """Parse `key=value` lines whose values must be integers."""

    return cast(dict[str, int], parse_mapping_lines(raw, value_type=int))


def parse_text_mapping_lines(raw: str) -> dict[str, str]:
    """Parse `key=value` lines whose values remain as text."""

    return cast(dict[str, str], parse_mapping_lines(raw, value_type=str))


def mapping_to_multiline(value: dict[str, Any]) -> str:
    """Render mapping values into multiline strings for the templates."""

    return "\n".join(f"{key}={item}" for key, item in value.items())


def tuple_to_csv(value: tuple[str, ...]) -> str:
    """Render tuple values as comma-separated strings for templates."""

    return ", ".join(value)


class StoredProfileSection(PanelModel):
    """Persisted user profile section."""

    name: str = ""
    email: EmailStr | None = None
    phone: str = ""
    city: str = ""
    linkedin_url: AnyUrl | None = None
    github_url: AnyUrl | None = None
    portfolio_url: AnyUrl | None = None
    years_experience_by_stack: dict[str, int] = Field(default_factory=dict)
    work_authorized: bool = False
    needs_sponsorship: bool = False
    salary_expectation: int | None = None
    availability: str = ""
    default_responses: dict[str, str] = Field(default_factory=dict)
    cv_path: str | None = None
    cv_filename: str | None = None


class ProfileFormInput(BaseModel):
    """Validated profile payload coming from the panel."""

    name: str = Field(min_length=1)
    email: EmailStr
    phone: str = Field(min_length=1)
    city: str = Field(min_length=1)
    linkedin_url: AnyUrl
    github_url: AnyUrl | None = None
    portfolio_url: AnyUrl | None = None
    years_experience_by_stack: dict[str, int] = Field(default_factory=dict)
    work_authorized: bool
    needs_sponsorship: bool = False
    salary_expectation: int | None = None
    availability: str = Field(min_length=1)
    default_responses: dict[str, str] = Field(default_factory=dict)


class StoredPreferencesSection(PanelModel):
    """Persisted search and preference section."""

    keywords: tuple[str, ...] = ()
    location: str = ""
    posted_within_hours: int = 24
    workplace_types: tuple[WorkplaceType, ...] = ()
    seniority: tuple[SeniorityLevel, ...] = ()
    easy_apply_only: bool = True
    positive_keywords: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()
    auto_connect_with_recruiter: bool = False


class PreferencesFormInput(BaseModel):
    """Validated preferences payload coming from the panel."""

    keywords: tuple[str, ...]
    location: str = Field(min_length=1)
    posted_within_hours: int = Field(ge=1, le=168)
    workplace_types: tuple[WorkplaceType, ...] = ()
    seniority: tuple[SeniorityLevel, ...] = ()
    easy_apply_only: bool = True
    positive_keywords: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()
    auto_connect_with_recruiter: bool = False


class StoredAISection(PanelModel):
    """Persisted AI section."""

    api_key: SecretStr | None = None
    model: str = "o3-mini"

    def masked_key(self) -> str | None:
        """Return a masked representation of the stored key."""

        if self.api_key is None:
            return None
        secret = self.api_key.get_secret_value()
        suffix = secret[-4:] if len(secret) >= 4 else secret
        return f"Configured (ends with {suffix})"


class AIFormInput(BaseModel):
    """Validated AI payload coming from the panel."""

    api_key: SecretStr | None = None
    model: str = "o3-mini"

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """Keep the dropdown limited to supported options for the MVP panel."""

        if value not in MODEL_OPTIONS:
            msg = "Unsupported model option"
            raise ValueError(msg)
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate the basic format of OpenAI API keys when present."""

        if value is None:
            return value
        secret = value.get_secret_value().strip()
        if not secret:
            return None
        if not re.fullmatch(r"sk-[A-Za-z0-9._-]+", secret):
            msg = "API key must look like an OpenAI secret key"
            raise ValueError(msg)
        return SecretStr(secret)


class PanelSettingsDocument(PanelModel):
    """Full persisted panel document."""

    profile: StoredProfileSection = Field(default_factory=StoredProfileSection)
    preferences: StoredPreferencesSection = Field(default_factory=StoredPreferencesSection)
    ai: StoredAISection = Field(default_factory=StoredAISection)


class PanelOverview(BaseModel):
    """Simple derived overview for the panel landing page."""

    profile_ready: bool
    preferences_ready: bool
    ai_ready: bool

    @classmethod
    def from_document(cls, document: PanelSettingsDocument) -> PanelOverview:
        """Compute readiness indicators for the home page."""

        return cls(
            profile_ready=bool(document.profile.name and document.profile.email),
            preferences_ready=bool(document.preferences.keywords and document.preferences.location),
            ai_ready=document.ai.api_key is not None,
        )


def ensure_runtime_dir(path: Path) -> Path:
    """Create a runtime directory when needed."""

    path.mkdir(parents=True, exist_ok=True)
    return path
