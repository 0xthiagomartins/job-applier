"""Adaptive structural memory for repeated LinkedIn Easy Apply interactions."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

from job_applier.application.repositories import ApplyActionMemoryRepository
from job_applier.domain.entities import ApplyActionMemory, utc_now
from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserActionType,
    BrowserAgentAction,
    BrowserAgentElement,
    BrowserAgentSnapshot,
    BrowserPressKey,
    BrowserScrollDirection,
    BrowserScrollTarget,
)
from job_applier.infrastructure.linkedin.question_resolution import EasyApplyField, normalize_text

MEMORY_TTL = timedelta(days=30)

TASK_OPEN_EASY_APPLY = "linkedin_open_easy_apply"
TASK_PRIMARY_ACTION = "linkedin_easy_apply_primary_action"
TASK_FINALIZE_CHECKBOX = "linkedin_easy_apply_finalize_checkbox"
TASK_FINALIZE_RADIO = "linkedin_easy_apply_finalize_radio"
TASK_FINALIZE_FIELD = "linkedin_easy_apply_finalize_field_interaction"

PROMOTABLE_TASKS = frozenset(
    {
        TASK_PRIMARY_ACTION,
        TASK_FINALIZE_CHECKBOX,
        TASK_FINALIZE_RADIO,
        TASK_FINALIZE_FIELD,
    }
)


def _canonical_json(payload: MappingLike) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


type MappingLike = dict[str, Any]


def _signature_hash(task_type: str, signature_payload: MappingLike) -> str:
    digest_input = _canonical_json({"task_type": task_type, "signature": signature_payload})
    return sha256(digest_input.encode("utf-8")).hexdigest()


def _option_signature(field: EasyApplyField) -> list[str]:
    return [normalize_text(option)[:120] for option in field.options[:8] if option.strip()]


def build_field_task_signature(
    *,
    task_type: str,
    field: EasyApplyField,
    required_state: str | None = None,
    visible_option_texts: tuple[str, ...] = (),
    validation_message: str | None = None,
) -> MappingLike:
    return {
        "task_type": task_type,
        "question_type": field.question_type.value,
        "normalized_key": normalize_text(field.normalized_key),
        "control_kind": field.control_kind,
        "input_type": normalize_text(field.input_type or ""),
        "required": field.required,
        "option_signature": _option_signature(field),
        "required_state": required_state,
        "visible_option_signature": [
            normalize_text(option)[:120] for option in visible_option_texts[:6] if option.strip()
        ],
        "validation_kind": normalize_text(validation_message or "")[:120],
    }


def build_step_task_signature(
    *,
    task_type: str,
    step_index: int,
    total_steps: int,
    surface_text: str,
    fields: tuple[EasyApplyField, ...],
) -> MappingLike:
    return {
        "task_type": task_type,
        "step_index": step_index,
        "total_steps": total_steps,
        "field_signature": [
            {
                "question_type": field.question_type.value,
                "normalized_key": normalize_text(field.normalized_key),
                "control_kind": field.control_kind,
                "input_type": normalize_text(field.input_type or ""),
                "required": field.required,
                "option_count": len(field.options),
            }
            for field in fields
        ],
        "surface_text_signature": normalize_text(surface_text)[:240],
    }


class AdaptiveApplyMemory:
    """Persist and replay successful Easy Apply interaction strategies."""

    def __init__(self, repository: ApplyActionMemoryRepository) -> None:
        self._repository = repository

    def find_active_memory(
        self,
        *,
        task_type: str,
        signature_payload: MappingLike,
    ) -> ApplyActionMemory | None:
        now = utc_now()
        self._repository.delete_expired(now=now)
        return self._repository.find_active_by_task_signature(
            task_type=task_type,
            signature_hash=_signature_hash(task_type, signature_payload),
            now=now,
        )

    def replay_action(
        self,
        *,
        memory: ApplyActionMemory,
        snapshot: BrowserAgentSnapshot,
    ) -> BrowserAgentAction | None:
        payload = json.loads(memory.strategy_payload_json)
        action_type = cast(
            BrowserActionType,
            str(payload.get("action_type") or "").strip(),
        )
        if action_type not in {"click", "fill", "press", "scroll", "wait", "done", "fail"}:
            return None

        element_id: str | None = None
        if action_type in {"click", "fill"}:
            anchor_payload = payload.get("anchor")
            if not isinstance(anchor_payload, dict):
                return None
            element = self._match_element(snapshot, anchor_payload)
            if element is None:
                return None
            element_id = element.element_id

        value_source = payload.get("value_source")
        if value_source is not None and not isinstance(value_source, str):
            return None

        return BrowserAgentAction(
            action_type=action_type,
            element_id=element_id,
            value_source=value_source,
            value=None,
            action_intent=_optional_string(payload.get("action_intent")),
            key_name=cast(BrowserPressKey | None, _optional_string(payload.get("key_name"))),
            scroll_target=cast(
                BrowserScrollTarget | None,
                _optional_string(payload.get("scroll_target")),
            ),
            scroll_direction=cast(
                BrowserScrollDirection | None,
                _optional_string(payload.get("scroll_direction")),
            ),
            scroll_amount=int(payload.get("scroll_amount") or 0),
            wait_seconds=int(payload.get("wait_seconds") or 0),
            reasoning=(
                "Replayed a locally remembered Easy Apply interaction strategy before "
                "calling OpenAI again."
            ),
        )

    def record_memory_hit_success(self, memory: ApplyActionMemory) -> ApplyActionMemory:
        now = utc_now()
        updated = replace(
            memory,
            success_count=memory.success_count + 1,
            last_used_at=now,
            last_succeeded_at=now,
            expires_at=now + MEMORY_TTL,
        )
        return self._repository.save(updated)

    def record_memory_hit_failure(self, memory: ApplyActionMemory) -> ApplyActionMemory:
        now = utc_now()
        updated = replace(
            memory,
            failure_count=memory.failure_count + 1,
            last_used_at=now,
        )
        return self._repository.save(updated)

    def promote_successful_action(
        self,
        *,
        task_type: str,
        signature_payload: MappingLike,
        action: BrowserAgentAction,
        snapshot: BrowserAgentSnapshot,
        existing_memory: ApplyActionMemory | None = None,
        replace_existing: bool = False,
    ) -> ApplyActionMemory | None:
        if task_type not in PROMOTABLE_TASKS:
            return None

        strategy_payload = self._strategy_payload_from_action(action=action, snapshot=snapshot)
        if strategy_payload is None:
            return None

        now = utc_now()
        self._repository.delete_expired(now=now)
        if (
            existing_memory is not None
            and not replace_existing
            and existing_memory.expires_at > now
        ):
            return existing_memory

        entity = ApplyActionMemory(
            id=existing_memory.id if existing_memory is not None else uuid4(),
            task_type=task_type,
            signature_hash=_signature_hash(task_type, signature_payload),
            signature_json=_canonical_json(signature_payload),
            strategy_payload_json=_canonical_json(strategy_payload),
            success_count=(existing_memory.success_count + 1) if existing_memory else 1,
            failure_count=(
                0 if replace_existing or existing_memory is None else existing_memory.failure_count
            ),
            created_at=existing_memory.created_at if existing_memory else now,
            last_used_at=now,
            last_succeeded_at=now,
            expires_at=now + MEMORY_TTL,
        )
        return self._repository.save(entity)

    def _strategy_payload_from_action(
        self,
        *,
        action: BrowserAgentAction,
        snapshot: BrowserAgentSnapshot,
    ) -> MappingLike | None:
        if action.action_type not in {"click", "press", "fill"}:
            return None
        payload: MappingLike = {
            "action_type": action.action_type,
            "action_intent": action.action_intent,
            "key_name": action.key_name,
            "scroll_target": action.scroll_target,
            "scroll_direction": action.scroll_direction,
            "scroll_amount": action.scroll_amount,
            "wait_seconds": action.wait_seconds,
        }
        if action.action_type == "fill":
            if action.value_source is None or action.value_source == "literal":
                return None
            payload["value_source"] = action.value_source

        if action.action_type in {"click", "fill"}:
            if action.element_id is None:
                return None
            element = next(
                (
                    candidate
                    for candidate in snapshot.elements
                    if candidate.element_id == action.element_id
                ),
                None,
            )
            if element is None:
                return None
            anchor = self._anchor_for_element(element)
            if anchor is None:
                return None
            payload["anchor"] = anchor
        return payload

    def _anchor_for_element(self, element: BrowserAgentElement) -> MappingLike | None:
        if element.is_priority_target:
            return {"kind": "priority_target"}

        for kind, raw_value in (
            ("candidate_label", element.candidate_label),
            ("label", element.label),
            ("text", element.text),
        ):
            normalized_value = normalize_text(raw_value or "")
            if normalized_value:
                return {
                    "kind": kind,
                    "value": normalized_value[:160],
                    "tag": normalize_text(element.tag),
                    "role": normalize_text(element.role or ""),
                    "input_type": normalize_text(element.input_type or ""),
                }
        return None

    def _match_element(
        self,
        snapshot: BrowserAgentSnapshot,
        anchor_payload: MappingLike,
    ) -> BrowserAgentElement | None:
        kind = str(anchor_payload.get("kind") or "").strip()
        if kind == "priority_target":
            return next(
                (
                    element
                    for element in snapshot.elements
                    if element.is_priority_target and not element.disabled
                ),
                None,
            )

        value = normalize_text(str(anchor_payload.get("value") or ""))
        tag = normalize_text(str(anchor_payload.get("tag") or ""))
        role = normalize_text(str(anchor_payload.get("role") or ""))
        input_type = normalize_text(str(anchor_payload.get("input_type") or ""))
        candidates: list[BrowserAgentElement] = []
        for element in snapshot.elements:
            if element.disabled:
                continue
            candidate_value = ""
            if kind == "candidate_label":
                candidate_value = normalize_text(element.candidate_label or "")
            elif kind == "label":
                candidate_value = normalize_text(element.label or "")
            elif kind == "text":
                candidate_value = normalize_text(element.text or "")
            if candidate_value != value:
                continue
            if tag and normalize_text(element.tag) != tag:
                continue
            if role and normalize_text(element.role or "") != role:
                continue
            if input_type and normalize_text(element.input_type or "") != input_type:
                continue
            candidates.append(element)

        if not candidates:
            return None
        priority_candidate = next(
            (element for element in candidates if element.is_priority_target),
            None,
        )
        return priority_candidate or candidates[0]


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
