"""Helpers to build immutable snapshots and successful submission records."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from job_applier.application.config import UserAgentSettings
from job_applier.domain.entities import ApplicationSubmission, ProfileSnapshot, utc_now
from job_applier.domain.enums import SubmissionStatus
from job_applier.domain.versioning import Ruleset


@dataclass(frozen=True, slots=True, kw_only=True)
class SuccessfulSubmissionRecord:
    """Immutable bundle used to query snapshot and ruleset by submission."""

    submission: ApplicationSubmission
    snapshot: ProfileSnapshot
    ruleset: Ruleset

    def __post_init__(self) -> None:
        if self.submission.status is not SubmissionStatus.SUBMITTED:
            msg = "submission must be submitted before creating a successful record"
            raise ValueError(msg)
        if self.submission.profile_snapshot_id != self.snapshot.id:
            msg = "submission must reference the snapshot used in the execution"
            raise ValueError(msg)
        if self.submission.ruleset_version != self.ruleset.version:
            msg = "submission ruleset version must match the bundled ruleset"
            raise ValueError(msg)

    @property
    def submission_id(self) -> UUID:
        """Convenience accessor used by persistence layers."""

        return self.submission.id


def build_profile_snapshot(
    settings: UserAgentSettings,
    *,
    created_at: datetime | None = None,
) -> ProfileSnapshot:
    """Create the immutable profile snapshot used by successful applications."""

    serialized_payload = json.dumps(
        settings.to_snapshot_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProfileSnapshot(
        created_at=created_at or utc_now(),
        data_json=serialized_payload,
    )


def create_successful_submission_record(
    submission: ApplicationSubmission,
    *,
    settings: UserAgentSettings,
    ruleset: Ruleset | None = None,
    submitted_at: datetime | None = None,
) -> SuccessfulSubmissionRecord:
    """Attach snapshot and ruleset version to a successful submission."""

    effective_ruleset = ruleset or settings.ruleset.to_domain()
    snapshot = build_profile_snapshot(settings)
    successful_submission = replace(
        submission,
        status=SubmissionStatus.SUBMITTED,
        submitted_at=submitted_at or utc_now(),
        profile_snapshot_id=snapshot.id,
        ruleset_version=effective_ruleset.version,
        ai_model_used=submission.ai_model_used or settings.ai.model,
    )
    return SuccessfulSubmissionRecord(
        submission=successful_submission,
        snapshot=snapshot,
        ruleset=effective_ruleset,
    )
