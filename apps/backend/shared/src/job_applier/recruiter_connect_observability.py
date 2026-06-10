"""Runtime observability helpers for recruiter connect."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from job_applier.cost_observability import record_efficiency_counter
from job_applier.observability import (
    append_timeline_event,
    read_output_json,
    update_summary_snapshot,
)


def record_recruiter_connect_observation(
    *,
    counters: Sequence[str] = (),
    status: str | None = None,
    reason: str | None = None,
    connect_path: str | None = None,
    send_action: str | None = None,
    success_signal: str | None = None,
    message_source: str | None = None,
    note_mode: str | None = None,
    recruiter_name: str | None = None,
    recruiter_profile_url: str | None = None,
    timeline_event: str = "recruiter_connect_observed",
    extra: Mapping[str, object] | None = None,
) -> None:
    """Record one recruiter-connect observation in the run summary and timeline."""

    summary = read_output_json("summary.json")
    recruiter_summary = _coerce_recruiter_connect_summary(summary.get("recruiter_connect"))

    for counter in counters:
        if not counter:
            continue
        recruiter_summary["counters"][counter] = (
            int(recruiter_summary["counters"].get(counter, 0)) + 1
        )
        record_efficiency_counter(group="recruiter_connect", metric=counter)
    if status:
        _increment_bucket(recruiter_summary["status_counts"], status)
        record_efficiency_counter(group="recruiter_connect", metric=f"status_{status}")
    if reason:
        _increment_bucket(recruiter_summary["reason_counts"], reason)
    if connect_path:
        _increment_bucket(recruiter_summary["connect_paths"], connect_path)
    if send_action:
        _increment_bucket(recruiter_summary["send_actions"], send_action)
    if success_signal:
        _increment_bucket(recruiter_summary["success_signals"], success_signal)
    if message_source:
        _increment_bucket(recruiter_summary["message_sources"], message_source)
    if note_mode:
        _increment_bucket(recruiter_summary["note_modes"], note_mode)

    update_summary_snapshot({"recruiter_connect": recruiter_summary})

    payload: dict[str, object] = {}
    if counters:
        payload["counters"] = list(counters)
    if status:
        payload["status"] = status
    if reason:
        payload["reason"] = reason
    if connect_path:
        payload["connect_path"] = connect_path
    if send_action:
        payload["send_action"] = send_action
    if success_signal:
        payload["success_signal"] = success_signal
    if message_source:
        payload["message_source"] = message_source
    if note_mode:
        payload["note_mode"] = note_mode
    if recruiter_name:
        payload["recruiter_name"] = recruiter_name
    if recruiter_profile_url:
        payload["recruiter_profile_url"] = recruiter_profile_url
    if extra:
        payload.update(dict(extra))
    append_timeline_event(timeline_event, payload)


def _increment_bucket(bucket: dict[str, int], key: str) -> None:
    bucket[key] = int(bucket.get(key, 0)) + 1


def _coerce_recruiter_connect_summary(value: object) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    counters_raw = raw.get("counters") if isinstance(raw, Mapping) else {}
    counters = counters_raw if isinstance(counters_raw, Mapping) else {}

    def _as_int_map(payload: object) -> dict[str, int]:
        if not isinstance(payload, Mapping):
            return {}
        return {
            str(key): int(count)
            for key, count in payload.items()
            if isinstance(key, str) and isinstance(count, (int, bool))
        }

    return {
        "counters": {
            "candidate_detected": int(counters.get("candidate_detected", 0)),
            "candidate_not_found": int(counters.get("candidate_not_found", 0)),
            "attempted": int(counters.get("attempted", 0)),
            "profile_opened": int(counters.get("profile_opened", 0)),
        },
        "status_counts": _as_int_map(raw.get("status_counts")),
        "reason_counts": _as_int_map(raw.get("reason_counts")),
        "connect_paths": _as_int_map(raw.get("connect_paths")),
        "send_actions": _as_int_map(raw.get("send_actions")),
        "success_signals": _as_int_map(raw.get("success_signals")),
        "message_sources": _as_int_map(raw.get("message_sources")),
        "note_modes": _as_int_map(raw.get("note_modes")),
    }
