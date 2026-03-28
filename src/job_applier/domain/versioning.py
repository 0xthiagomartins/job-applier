"""Versioned domain models related to configuration and rulesets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from job_applier.domain.entities import ensure_non_empty, ensure_utc, utc_now


@dataclass(frozen=True, slots=True, kw_only=True)
class Ruleset:
    """Represents the versioned rules used for a submission run."""

    version: str
    allow_best_effort_autofill: bool = True
    auto_connect_with_recruiter: bool = False
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", ensure_non_empty(self.version, "version"))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, "created_at"))
