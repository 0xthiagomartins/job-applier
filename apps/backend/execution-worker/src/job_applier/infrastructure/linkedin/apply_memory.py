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
from job_applier.infrastructure.linkedin.question_resolution import (
    _PLACEHOLDER_OPTION_TOKENS,
    EasyApplyField,
    normalize_text,
)

MEMORY_TTL = timedelta(days=30)

TASK_OPEN_EASY_APPLY = "linkedin_open_easy_apply"
TASK_PRIMARY_ACTION = "linkedin_easy_apply_primary_action"
TASK_FINALIZE_CHECKBOX = "linkedin_easy_apply_finalize_checkbox"
TASK_FINALIZE_RADIO = "linkedin_easy_apply_finalize_radio"
TASK_FINALIZE_FIELD = "linkedin_easy_apply_finalize_field_interaction"
TASK_RESOLVE_CHECKBOX = "linkedin_easy_apply_resolve_checkbox"
TASK_RESOLVE_RADIO = "linkedin_easy_apply_resolve_radio"
TASK_RESOLVE_SELECT = "linkedin_easy_apply_resolve_select"

PROMOTABLE_TASKS = frozenset(
    {
        TASK_PRIMARY_ACTION,
        TASK_FINALIZE_CHECKBOX,
        TASK_FINALIZE_RADIO,
        TASK_FINALIZE_FIELD,
    }
)

PROMOTABLE_RESOLUTION_TASKS = frozenset(
    {
        TASK_RESOLVE_CHECKBOX,
        TASK_RESOLVE_RADIO,
        TASK_RESOLVE_SELECT,
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


def build_field_resolution_task_signature(*, task_type: str, field: EasyApplyField) -> MappingLike:
    return {
        "task_type": task_type,
        "question_type": field.question_type.value,
        "normalized_key": _structural_resolution_key(field),
        "control_kind": field.control_kind,
        "input_type": normalize_text(field.input_type or ""),
        "required": field.required,
        "option_signature": _structural_option_signature(field),
    }


def resolution_task_type_for_field(field: EasyApplyField) -> str | None:
    match field.control_kind:
        case "checkbox":
            return TASK_RESOLVE_CHECKBOX
        case "radio":
            return TASK_RESOLVE_RADIO
        case "select":
            return TASK_RESOLVE_SELECT
        case _:
            return None


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

    def replay_resolution(
        self,
        *,
        memory: ApplyActionMemory,
        field: EasyApplyField,
    ) -> str | None:
        payload = json.loads(memory.strategy_payload_json)
        strategy_kind = str(payload.get("strategy_kind") or "").strip()
        if strategy_kind == "semantic_option":
            semantic_token = _optional_string(payload.get("semantic_token"))
            if semantic_token is None:
                return None
            for option in field.options:
                if _semantic_option_token(field, option) == semantic_token:
                    return option
            return None
        if strategy_kind == "single_non_placeholder_option":
            return _first_meaningful_option(field.options)
        if strategy_kind == "selected_option":
            preferred_value = _optional_string(payload.get("preferred_value"))
            if preferred_value is None:
                return None
            return _pick_matching_option(field.options, preferred=preferred_value)
        if strategy_kind == "checkbox_state":
            desired_checked = payload.get("desired_checked")
            if not isinstance(desired_checked, bool):
                return None
            return "Yes" if desired_checked else "No"
        return None

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

    def promote_successful_resolution(
        self,
        *,
        task_type: str,
        signature_payload: MappingLike,
        field: EasyApplyField,
        resolved_value: str,
        existing_memory: ApplyActionMemory | None = None,
        replace_existing: bool = False,
    ) -> ApplyActionMemory | None:
        if task_type not in PROMOTABLE_RESOLUTION_TASKS:
            return None

        strategy_payload = self._resolution_strategy_payload(
            field=field,
            resolved_value=resolved_value,
        )
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

    def _resolution_strategy_payload(
        self,
        *,
        field: EasyApplyField,
        resolved_value: str,
    ) -> MappingLike | None:
        match field.control_kind:
            case "checkbox":
                normalized_value = normalize_text(resolved_value)
                desired_checked = normalized_value in {"yes", "sim", "true", "1"}
                return {
                    "strategy_kind": "checkbox_state",
                    "desired_checked": desired_checked,
                }
            case "radio" | "select":
                if _has_single_meaningful_option(field.options):
                    return {"strategy_kind": "single_non_placeholder_option"}
                semantic_token = _semantic_option_token(field, resolved_value)
                if semantic_token is not None and semantic_token != "placeholder":
                    return {
                        "strategy_kind": "semantic_option",
                        "semantic_token": semantic_token,
                    }
                preferred_value = _pick_matching_option(field.options, preferred=resolved_value)
                if preferred_value is None:
                    return None
                return {
                    "strategy_kind": "selected_option",
                    "preferred_value": preferred_value,
                }
            case _:
                return None

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


def _pick_matching_option(options: tuple[str, ...], *, preferred: str) -> str | None:
    normalized_preferred = normalize_text(preferred)
    if not normalized_preferred:
        return None
    canonical_preferred = _canonical_binary_token(normalized_preferred)
    for option in options:
        normalized_option = normalize_text(option)
        if normalized_option == normalized_preferred:
            return option
        if (
            canonical_preferred is not None
            and _canonical_binary_token(normalized_option) == canonical_preferred
        ):
            return option
    for option in options:
        normalized_option = normalize_text(option)
        if normalized_preferred in normalized_option or normalized_option in normalized_preferred:
            return option
    return None


def _structural_resolution_key(field: EasyApplyField) -> str:
    normalized_key = normalize_text(field.normalized_key)
    subject = _semantic_subject_key(field)
    if _looks_like_proficiency_ladder_field(field):
        return f"proficiency_ladder:{subject}:{field.control_kind}"
    if _is_binary_like_field(field):
        return f"binary_choice:{subject}:{field.control_kind}"
    if _has_single_meaningful_option(field.options):
        return f"single_choice:{subject}:{field.control_kind}"
    return normalized_key


def _structural_option_signature(field: EasyApplyField) -> list[str]:
    signature: list[str] = []
    for option in field.options[:8]:
        if not option.strip():
            continue
        semantic_token = _semantic_option_token(field, option)
        signature.append(semantic_token or normalize_text(option)[:120])
    return signature


def _semantic_option_token(field: EasyApplyField, option: str) -> str | None:
    normalized_option = normalize_text(option)
    if not normalized_option:
        return None
    if normalized_option in _PLACEHOLDER_OPTION_TOKENS:
        return "placeholder"
    binary_token = _canonical_binary_token(normalized_option)
    if binary_token is not None:
        return f"binary:{binary_token}"
    if _looks_like_proficiency_ladder_field(field):
        proficiency_token = _canonical_proficiency_token(normalized_option)
        if proficiency_token is not None:
            return f"proficiency:{proficiency_token}"
    if _has_single_meaningful_option(field.options):
        return "single_choice"
    return None


def _looks_like_proficiency_ladder_field(field: EasyApplyField) -> bool:
    combined = normalize_text(" ".join((field.question_raw, field.normalized_key, *field.options)))
    if not combined:
        return False
    proficiency_markers = (
        "level",
        "nivel",
        "proficiency",
        "fluency",
        "confidence",
        "confianca",
        "knowledge",
        "conhecimento",
    )
    if any(token in combined for token in proficiency_markers):
        return any(
            token in combined
            for token in (
                "advanced",
                "avancado",
                "intermediate",
                "intermediario",
                "basic",
                "basico",
                "beginner",
                "iniciante",
                "proficient",
                "proficiente",
                "expert",
                "especialista",
                "fluent",
                "fluente",
            )
        )
    return False


def _is_binary_like_field(field: EasyApplyField) -> bool:
    if field.control_kind == "checkbox":
        return True
    meaningful_options = [
        option
        for option in field.options
        if normalize_text(option) not in _PLACEHOLDER_OPTION_TOKENS
    ]
    if not meaningful_options:
        return False
    return all(_canonical_binary_token(option) is not None for option in meaningful_options[:3])


def _has_single_meaningful_option(options: tuple[str, ...]) -> bool:
    return (
        sum(1 for option in options if normalize_text(option) not in _PLACEHOLDER_OPTION_TOKENS)
        == 1
    )


def _first_meaningful_option(options: tuple[str, ...]) -> str | None:
    for option in options:
        if normalize_text(option) not in _PLACEHOLDER_OPTION_TOKENS:
            return option
    return None


def _semantic_subject_key(field: EasyApplyField) -> str:
    combined = normalize_text(" ".join((field.question_raw, field.normalized_key)))
    if any(token in combined for token in ("english", "ingles")):
        return "english"
    if any(token in combined for token in ("spanish", "espanhol", "espanol")):
        return "spanish"
    if any(token in combined for token in ("portuguese", "portugues")):
        return "portuguese"
    if "java" in combined:
        return "java"
    if any(token in combined for token in ("disability", "deficiencia", "pcd")):
        return "disability"
    if any(token in combined for token in ("gender", "genero", "gênero")):
        return "gender"
    if any(token in combined for token in ("race", "raca", "cor/ra")):
        return "race"
    return normalize_text(field.normalized_key)


def _canonical_proficiency_token(value: str) -> str | None:
    normalized = normalize_text(value)
    roman_numeral = None
    if " ii" in f" {normalized} " or " 2" in f" {normalized} ":
        roman_numeral = "2"
    elif " i" in f" {normalized} " or " 1" in f" {normalized} ":
        roman_numeral = "1"

    if any(token in normalized for token in ("basic", "basico", "beginner", "iniciante")):
        return "basic"
    if "intermediate" in normalized or "intermediario" in normalized:
        if roman_numeral == "1":
            return "intermediate_1"
        if roman_numeral == "2":
            return "intermediate_2"
        return "intermediate"
    if any(token in normalized for token in ("advanced", "avancado")):
        return "advanced"
    if any(
        token in normalized
        for token in (
            "proficient",
            "proficiente",
            "expert",
            "especialista",
            "fluent",
            "fluente",
            "native",
        )
    ):
        return "proficient"
    return None


def _canonical_binary_token(value: str) -> str | None:
    normalized = normalize_text(value)
    if normalized in {"yes", "y", "true", "sim", "s"}:
        return "yes"
    if normalized in {"no", "n", "false", "nao"}:
        return "no"
    return None
