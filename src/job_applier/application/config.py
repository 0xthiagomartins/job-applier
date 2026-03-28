"""Settings models for user and agent configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyUrl, BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from job_applier.domain.enums import ScheduleFrequency, SeniorityLevel, WorkplaceType
from job_applier.domain.versioning import Ruleset


class FrozenModel(BaseModel):
    """Base Pydantic model used by immutable configuration payloads."""

    model_config = ConfigDict(frozen=True)


class UserProfileConfig(FrozenModel):
    """Candidate profile data managed by the panel."""

    name: str
    email: EmailStr
    phone: str
    city: str
    linkedin_url: AnyUrl | None = None
    github_url: AnyUrl | None = None
    portfolio_url: AnyUrl | None = None
    years_experience_by_stack: dict[str, int] = Field(default_factory=dict)
    work_authorized: bool
    needs_sponsorship: bool = False
    salary_expectation: int | None = None
    availability: str
    default_responses: dict[str, str] = Field(default_factory=dict)
    cv_path: str | None = None
    cv_filename: str | None = None
    positive_filters: tuple[str, ...] = ()
    blacklist: tuple[str, ...] = ()


class SearchConfig(FrozenModel):
    """Search preferences used by the automation agent."""

    keywords: tuple[str, ...]
    location: str
    posted_within_hours: int = 24
    workplace_types: tuple[WorkplaceType, ...] = ()
    seniority: tuple[SeniorityLevel, ...] = ()
    easy_apply_only: bool = True
    minimum_score_threshold: float = Field(default=0.55, ge=0.0, le=1.0)


class ScheduleConfig(FrozenModel):
    """Execution schedule configuration for the agent."""

    frequency: ScheduleFrequency = ScheduleFrequency.DAILY
    run_at: str = "23:00"
    timezone: str = "UTC"

    @field_validator("run_at")
    @classmethod
    def validate_run_at(cls, value: str) -> str:
        """Ensure the configured time uses `HH:MM` 24-hour format."""

        try:
            hour_text, minute_text = value.split(":", maxsplit=1)
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError as exc:
            msg = "run_at must use HH:MM format"
            raise ValueError(msg) from exc

        if hour not in range(24) or minute not in range(60):
            msg = "run_at must be a valid 24-hour time"
            raise ValueError(msg)
        return f"{hour:02d}:{minute:02d}"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Ensure the configured timezone exists."""

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            msg = "timezone must be a valid IANA timezone"
            raise ValueError(msg) from exc
        return value


class AgentConfig(FrozenModel):
    """Non-sensitive agent runtime preferences."""

    schedule: ScheduleConfig
    auto_connect_with_recruiter: bool = False


class AIConfig(FrozenModel):
    """AI settings used for question classification and autofill support."""

    api_key: SecretStr | None = None
    model: str


class RulesetConfig(FrozenModel):
    """Versioned ruleset configuration for an execution."""

    version: str = "ruleset-v1"
    allow_best_effort_autofill: bool = True
    auto_connect_with_recruiter: bool = False

    def to_domain(self) -> Ruleset:
        """Build the domain ruleset model from configuration data."""

        return Ruleset(
            version=self.version,
            allow_best_effort_autofill=self.allow_best_effort_autofill,
            auto_connect_with_recruiter=self.auto_connect_with_recruiter,
        )


class UserAgentSettings(BaseSettings):
    """Versioned application settings loaded from env, .env or panel data."""

    config_version: str = "config-v1"
    profile: UserProfileConfig
    search: SearchConfig
    agent: AgentConfig
    ai: AIConfig
    ruleset: RulesetConfig = Field(default_factory=RulesetConfig)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
        validate_default=True,
    )

    def to_snapshot_payload(self) -> dict[str, Any]:
        """Return the serializable payload stored in immutable snapshots."""

        return self.model_dump(mode="json", exclude={"ai": {"api_key"}})

    @classmethod
    def from_env_file(cls, env_file: str | Path) -> UserAgentSettings:
        """Load settings from an explicit .env file path."""

        return cls(_env_file=env_file)  # type: ignore[call-arg]
