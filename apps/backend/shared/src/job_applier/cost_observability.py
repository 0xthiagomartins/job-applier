"""Per-run cost and efficiency observability helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from job_applier.observability import (
    append_timeline_event,
    read_output_json,
    update_summary_snapshot,
)


def record_openai_usage(
    *,
    category: str,
    model: str,
    latency_ms: int,
    response_payload: Mapping[str, object] | None = None,
    status: str = "ok",
    error_status: int | None = None,
    error_message: str | None = None,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Record one OpenAI API interaction in the current run summary."""

    summary = read_output_json("summary.json")
    cost = _coerce_cost_summary(summary.get("cost"))
    usage = extract_openai_usage(response_payload)

    openai_summary = cost["openai"]
    openai_summary["calls_total"] += 1
    openai_summary["latency_ms_total"] += max(0, latency_ms)
    if status == "rate_limited":
        openai_summary["rate_limit_count"] += 1
    if status != "ok":
        openai_summary["failure_count"] += 1
    openai_summary["tokens"]["input"] += usage["input"]
    openai_summary["tokens"]["output"] += usage["output"]
    openai_summary["tokens"]["total"] += usage["total"]

    category_summary = openai_summary["by_category"].setdefault(
        category,
        {
            "calls": 0,
            "latency_ms_total": 0,
            "models": {},
            "tokens": {
                "input": 0,
                "output": 0,
                "total": 0,
            },
            "rate_limit_count": 0,
            "failure_count": 0,
        },
    )
    category_summary["calls"] += 1
    category_summary["latency_ms_total"] += max(0, latency_ms)
    category_summary["tokens"]["input"] += usage["input"]
    category_summary["tokens"]["output"] += usage["output"]
    category_summary["tokens"]["total"] += usage["total"]
    if status == "rate_limited":
        category_summary["rate_limit_count"] += 1
    if status != "ok":
        category_summary["failure_count"] += 1
    category_summary["models"][model] = category_summary["models"].get(model, 0) + 1

    update_summary_snapshot({"cost": cost})
    timeline_payload: dict[str, object] = {
        "category": category,
        "model": model,
        "status": status,
        "latency_ms": max(0, latency_ms),
        "tokens": usage,
    }
    if error_status is not None:
        timeline_payload["error_status"] = error_status
    if error_message:
        timeline_payload["error_message"] = error_message
    if extra:
        timeline_payload.update(dict(extra))
    append_timeline_event("openai_cost_recorded", timeline_payload)


def record_efficiency_counter(
    *,
    group: str,
    metric: str,
    delta: int = 1,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Increment one efficiency counter in the current run summary."""

    if delta == 0:
        return
    summary = read_output_json("summary.json")
    cost = _coerce_cost_summary(summary.get("cost"))
    efficiency_group = cost["efficiency"].setdefault(group, {})
    efficiency_group[metric] = int(efficiency_group.get(metric, 0)) + int(delta)
    update_summary_snapshot({"cost": cost})

    timeline_payload: dict[str, object] = {
        "group": group,
        "metric": metric,
        "delta": int(delta),
    }
    if extra:
        timeline_payload.update(dict(extra))
    append_timeline_event("cost_efficiency_recorded", timeline_payload)


def extract_openai_usage(response_payload: Mapping[str, object] | None) -> dict[str, int]:
    """Extract token usage from an OpenAI Responses API payload when available."""

    if response_payload is None:
        return {"input": 0, "output": 0, "total": 0}

    usage_payload = response_payload.get("usage")
    if not isinstance(usage_payload, Mapping):
        return {"input": 0, "output": 0, "total": 0}

    input_tokens = _coerce_int(usage_payload.get("input_tokens"))
    output_tokens = _coerce_int(usage_payload.get("output_tokens"))
    total_tokens = _coerce_int(usage_payload.get("total_tokens"))
    if total_tokens <= 0:
        total_tokens = max(0, input_tokens) + max(0, output_tokens)
    return {
        "input": max(0, input_tokens),
        "output": max(0, output_tokens),
        "total": max(0, total_tokens),
    }


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _coerce_cost_summary(value: object) -> dict[str, Any]:
    raw = _as_mapping(value)
    openai_raw = _as_mapping(raw.get("openai"))
    tokens_raw = _as_mapping(openai_raw.get("tokens"))
    by_category_raw = _as_mapping(openai_raw.get("by_category"))
    efficiency_raw = _as_mapping(raw.get("efficiency"))

    by_category: dict[str, dict[str, Any]] = {}
    for key, payload in by_category_raw.items():
        if not isinstance(key, str) or not isinstance(payload, Mapping):
            continue
        payload_tokens = _as_mapping(payload.get("tokens"))
        payload_models = _as_mapping(payload.get("models"))
        by_category[key] = {
            "calls": _coerce_int(payload.get("calls")),
            "latency_ms_total": _coerce_int(payload.get("latency_ms_total")),
            "models": {
                str(model): _coerce_int(count)
                for model, count in payload_models.items()
                if isinstance(model, str)
            },
            "tokens": {
                "input": _coerce_int(payload_tokens.get("input")),
                "output": _coerce_int(payload_tokens.get("output")),
                "total": _coerce_int(payload_tokens.get("total")),
            },
            "rate_limit_count": _coerce_int(payload.get("rate_limit_count")),
            "failure_count": _coerce_int(payload.get("failure_count")),
        }

    efficiency: dict[str, dict[str, int]] = {}
    for key, payload in efficiency_raw.items():
        if not isinstance(key, str) or not isinstance(payload, Mapping):
            continue
        efficiency[key] = {
            str(metric): _coerce_int(count)
            for metric, count in payload.items()
            if isinstance(metric, str)
        }

    return {
        "openai": {
            "calls_total": _coerce_int(openai_raw.get("calls_total")),
            "rate_limit_count": _coerce_int(openai_raw.get("rate_limit_count")),
            "failure_count": _coerce_int(openai_raw.get("failure_count")),
            "latency_ms_total": _coerce_int(openai_raw.get("latency_ms_total")),
            "tokens": {
                "input": _coerce_int(tokens_raw.get("input")),
                "output": _coerce_int(tokens_raw.get("output")),
                "total": _coerce_int(tokens_raw.get("total")),
            },
            "by_category": by_category,
        },
        "efficiency": efficiency,
    }


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
