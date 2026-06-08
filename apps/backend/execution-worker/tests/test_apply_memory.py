from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from job_applier.domain.entities import ApplyActionMemory
from job_applier.domain.enums import AnswerSource, FillStrategy, QuestionType
from job_applier.infrastructure.cache import DiskCacheApplyActionMemoryRepository
from job_applier.infrastructure.linkedin.apply_memory import (
    TASK_PRIMARY_ACTION,
    TASK_RESOLVE_SELECT,
    AdaptiveApplyMemory,
    build_field_resolution_task_signature,
    build_step_task_signature,
)
from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAgentAction,
    BrowserAgentElement,
    BrowserAgentSnapshot,
)
from job_applier.infrastructure.linkedin.easy_apply import (
    EasyApplyStep,
    PlaywrightLinkedInEasyApplyExecutor,
    _field_requires_agentic_semantic_recovery,
)
from job_applier.infrastructure.linkedin.question_resolution import (
    EasyApplyField,
    ResolvedFieldValue,
)


def _make_step_field(
    *,
    normalized_key: str,
    question_type: QuestionType,
    control_kind: str,
) -> EasyApplyField:
    return EasyApplyField(
        question_raw=normalized_key,
        normalized_key=normalized_key,
        question_type=question_type,
        control_kind=control_kind,  # type: ignore[arg-type]
        input_type=control_kind,
        required=True,
    )


def _make_snapshot(
    *,
    element_id: str,
    step_index: int = 0,
    total_steps: int = 4,
    priority: bool = True,
    label: str = "Next",
    candidate_label: str | None = None,
) -> BrowserAgentSnapshot:
    return BrowserAgentSnapshot(
        url="https://www.linkedin.com/jobs/view/123/",
        title="Apply to ACME",
        visible_text=f"Apply to ACME {step_index + 1}/{total_steps} pages contact info Next",
        elements=(
            BrowserAgentElement(
                element_id="noise-1",
                tag="button",
                label="Back",
                text="Back",
                role="button",
            ),
            BrowserAgentElement(
                element_id=element_id,
                tag="button",
                label=label,
                text=label,
                role="button",
                is_priority_target=priority,
                candidate_label=candidate_label,
            ),
            BrowserAgentElement(
                element_id="noise-2",
                tag="button",
                label="Save",
                text="Save",
                role="button",
                disabled=True,
            ),
        ),
        active_surface="easy apply modal",
    )


class AdaptiveApplyMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.repository = DiskCacheApplyActionMemoryRepository(Path(self._temp_dir.name))
        self.memory = AdaptiveApplyMemory(self.repository)
        self.step_fields = (
            _make_step_field(
                normalized_key="first_name",
                question_type=QuestionType.FIRST_NAME,
                control_kind="text",
            ),
            _make_step_field(
                normalized_key="email",
                question_type=QuestionType.EMAIL,
                control_kind="select",
            ),
        )
        self.signature = build_step_task_signature(
            task_type=TASK_PRIMARY_ACTION,
            step_index=0,
            total_steps=4,
            surface_text="Apply to ACME 1/4 pages Contact info Next",
            fields=self.step_fields,
        )

    def test_priority_target_memory_replays_across_snapshot_variants(self) -> None:
        initial_snapshot = _make_snapshot(element_id="button-1")
        action = BrowserAgentAction(
            action_type="click",
            element_id="button-1",
            value_source=None,
            value=None,
            action_intent="advance_step",
            key_name=None,
            scroll_target=None,
            scroll_direction=None,
            scroll_amount=0,
            wait_seconds=0,
            reasoning="advance",
        )

        promoted = self.memory.promote_successful_action(
            task_type=TASK_PRIMARY_ACTION,
            signature_payload=self.signature,
            action=action,
            snapshot=initial_snapshot,
        )
        if promoted is None:
            self.fail("expected memory promotion to succeed")
        active_memory = self.memory.find_active_memory(
            task_type=TASK_PRIMARY_ACTION,
            signature_payload=self.signature,
        )
        if active_memory is None:
            self.fail("expected active memory to be available")

        for index in range(10):
            with self.subTest(index=index):
                variant_snapshot = BrowserAgentSnapshot(
                    url=initial_snapshot.url,
                    title=initial_snapshot.title,
                    visible_text=initial_snapshot.visible_text,
                    elements=(
                        BrowserAgentElement(
                            element_id=f"other-{index}",
                            tag="button",
                            label="Back",
                            text="Back",
                            role="button",
                        ),
                        BrowserAgentElement(
                            element_id=f"replayed-{index}",
                            tag="button",
                            label="Continue",
                            text="Continue",
                            role="button",
                            is_priority_target=True,
                        ),
                    ),
                    active_surface=initial_snapshot.active_surface,
                )
                replayed = self.memory.replay_action(
                    memory=active_memory,
                    snapshot=variant_snapshot,
                )
                if replayed is None:
                    self.fail("expected replayed action for priority target memory")
                self.assertEqual(replayed.element_id, f"replayed-{index}")
                self.assertEqual(replayed.action_type, "click")
                self.assertEqual(replayed.action_intent, "advance_step")

    def test_memory_refresh_and_replace_behaviour(self) -> None:
        initial_snapshot = _make_snapshot(
            element_id="button-1",
            priority=False,
            label="Especialista",
        )
        action = BrowserAgentAction(
            action_type="click",
            element_id="button-1",
            value_source=None,
            value=None,
            action_intent="select_option",
            key_name=None,
            scroll_target=None,
            scroll_direction=None,
            scroll_amount=0,
            wait_seconds=0,
            reasoning="pick radio",
        )

        promoted = self.memory.promote_successful_action(
            task_type=TASK_PRIMARY_ACTION,
            signature_payload=self.signature,
            action=action,
            snapshot=initial_snapshot,
        )
        if promoted is None:
            self.fail("expected initial promotion to succeed")
        refreshed = self.memory.record_memory_hit_success(promoted)
        self.assertEqual(refreshed.success_count, 2)
        self.assertEqual(refreshed.failure_count, 0)
        self.assertIsNotNone(refreshed.last_succeeded_at)
        self.assertGreater(refreshed.expires_at, promoted.expires_at)

        degraded = self.memory.record_memory_hit_failure(refreshed)
        self.assertEqual(degraded.success_count, 2)
        self.assertEqual(degraded.failure_count, 1)

        replacement_snapshot = _make_snapshot(
            element_id="button-99",
            priority=False,
            label="Avançado",
            candidate_label="avançado",
        )
        replacement_action = BrowserAgentAction(
            action_type="click",
            element_id="button-99",
            value_source=None,
            value=None,
            action_intent="select_option",
            key_name=None,
            scroll_target=None,
            scroll_direction=None,
            scroll_amount=0,
            wait_seconds=0,
            reasoning="pick better radio",
        )
        replaced = self.memory.promote_successful_action(
            task_type=TASK_PRIMARY_ACTION,
            signature_payload=self.signature,
            action=replacement_action,
            snapshot=replacement_snapshot,
            existing_memory=degraded,
            replace_existing=True,
        )
        if replaced is None:
            self.fail("expected replacement promotion to succeed")
        self.assertEqual(replaced.id, degraded.id)
        self.assertEqual(replaced.failure_count, 0)
        self.assertEqual(replaced.success_count, degraded.success_count + 1)
        replayed = self.memory.replay_action(
            memory=replaced,
            snapshot=BrowserAgentSnapshot(
                url=replacement_snapshot.url,
                title=replacement_snapshot.title,
                visible_text=replacement_snapshot.visible_text,
                elements=(
                    BrowserAgentElement(
                        element_id="first-match",
                        tag="button",
                        label="Avançado",
                        text="Avançado",
                        role="button",
                    ),
                    BrowserAgentElement(
                        element_id="priority-match",
                        tag="button",
                        label="Ignored label",
                        text="Ignored label",
                        role="button",
                        candidate_label="avançado",
                        is_priority_target=True,
                    ),
                ),
                active_surface=replacement_snapshot.active_surface,
            ),
        )
        if replayed is None:
            self.fail("expected replayed action for label/candidate-label memory")
        self.assertEqual(replayed.element_id, "priority-match")

    def test_resolution_memory_replays_select_option(self) -> None:
        field = EasyApplyField(
            question_raw="Como ficou sabendo da nossa vaga?",
            normalized_key="referral_source",
            question_type=QuestionType.FREE_TEXT_GENERIC,
            control_kind="select",
            input_type="select",
            required=True,
            options=("Select an option", "LinkedIn", "Indicação"),
        )
        signature = build_field_resolution_task_signature(
            task_type=TASK_RESOLVE_SELECT,
            field=field,
        )
        promoted = self.memory.promote_successful_resolution(
            task_type=TASK_RESOLVE_SELECT,
            signature_payload=signature,
            field=field,
            resolved_value="LinkedIn",
        )
        if promoted is None:
            self.fail("expected select resolution memory promotion to succeed")

        active_memory = self.memory.find_active_memory(
            task_type=TASK_RESOLVE_SELECT,
            signature_payload=signature,
        )
        if active_memory is None:
            self.fail("expected select resolution memory to be available")

        replayed = self.memory.replay_resolution(memory=active_memory, field=field)
        self.assertEqual(replayed, "LinkedIn")

        refreshed = self.memory.record_memory_hit_success(active_memory)
        self.assertEqual(refreshed.success_count, 2)
        self.assertGreater(refreshed.expires_at, active_memory.expires_at)

    def test_resolution_memory_replays_proficiency_select_across_languages(self) -> None:
        warmup_field = EasyApplyField(
            question_raw="[EN] Qual é o seu nível de confiança ao se comunicar em inglês?",
            normalized_key="english_proficiency",
            question_type=QuestionType.FREE_TEXT_GENERIC,
            control_kind="select",
            input_type="select",
            required=True,
            options=(
                "Select an option",
                "Básico",
                "Intermediário I",
                "Intermediário II",
                "Avançado",
                "Proficiente",
            ),
        )
        replay_field = EasyApplyField(
            question_raw=(
                "[EN] What is your level of confidence when communicating in an "
                "English-speaking work environment?"
            ),
            normalized_key="english_proficiency",
            question_type=QuestionType.FREE_TEXT_GENERIC,
            control_kind="select",
            input_type="select",
            required=True,
            options=(
                "Select an option",
                "Basic",
                "Intermediate I",
                "Intermediate II",
                "Advanced",
                "Proficient",
            ),
        )
        warmup_signature = build_field_resolution_task_signature(
            task_type=TASK_RESOLVE_SELECT,
            field=warmup_field,
        )
        replay_signature = build_field_resolution_task_signature(
            task_type=TASK_RESOLVE_SELECT,
            field=replay_field,
        )

        self.assertEqual(warmup_signature, replay_signature)

        promoted = self.memory.promote_successful_resolution(
            task_type=TASK_RESOLVE_SELECT,
            signature_payload=warmup_signature,
            field=warmup_field,
            resolved_value="Avançado",
        )
        if promoted is None:
            self.fail("expected proficiency select resolution memory promotion to succeed")

        active_memory = self.memory.find_active_memory(
            task_type=TASK_RESOLVE_SELECT,
            signature_payload=replay_signature,
        )
        if active_memory is None:
            self.fail("expected proficiency select resolution memory to be available")

        replayed = self.memory.replay_resolution(memory=active_memory, field=replay_field)
        self.assertEqual(replayed, "Advanced")

    def test_executor_skips_sensitive_resolution_memory_replay_and_promotion(self) -> None:
        field = EasyApplyField(
            question_raw="Gostaria de nos dizer sua identidade de gênero?",
            normalized_key="gostaria_de_nos_dizer_sua_identidade_de_genero",
            question_type=QuestionType.UNKNOWN,
            control_kind="select",
            input_type="select",
            required=False,
            options=("Homem Cisgênero", "Mulher Cisgênero", "Pessoa Não Binária"),
        )
        signature = build_field_resolution_task_signature(
            task_type=TASK_RESOLVE_SELECT,
            field=field,
        )
        promoted = self.memory.promote_successful_resolution(
            task_type=TASK_RESOLVE_SELECT,
            signature_payload=signature,
            field=field,
            resolved_value="Homem Cisgênero",
        )
        if promoted is None:
            self.fail("expected stored memory entry for setup")

        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
        executor._apply_memory = self.memory

        (
            memory_entry,
            task_type,
            signature_payload,
            resolution,
        ) = executor._replay_field_resolution_memory(field)
        self.assertIsNone(memory_entry)
        self.assertIsNone(task_type)
        self.assertIsNone(signature_payload)
        self.assertIsNone(resolution)

        self.assertFalse(
            executor._should_promote_field_resolution_memory(
                field,
                ResolvedFieldValue(
                    value="Homem Cisgênero",
                    answer_source=AnswerSource.AI,
                    fill_strategy=FillStrategy.AUTOFILL_AI,
                ),
            )
        )

    def test_executor_skips_accessibility_resolution_memory_replay_and_promotion(self) -> None:
        field = EasyApplyField(
            question_raw="Você precisa de algum tipo de acessibilidade?",
            normalized_key="accessibility_accommodation",
            question_type=QuestionType.UNKNOWN,
            control_kind="select",
            input_type="select",
            required=True,
            options=(
                "Select an option",
                "Elevador/Rampa",
                "Não necessito de nenhuma acessibilidade",
            ),
        )
        signature = build_field_resolution_task_signature(
            task_type=TASK_RESOLVE_SELECT,
            field=field,
        )
        promoted = self.memory.promote_successful_resolution(
            task_type=TASK_RESOLVE_SELECT,
            signature_payload=signature,
            field=field,
            resolved_value="Não necessito de nenhuma acessibilidade",
        )
        if promoted is None:
            self.fail("expected stored memory entry for setup")

        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
        executor._apply_memory = self.memory

        (
            memory_entry,
            task_type,
            signature_payload,
            resolution,
        ) = executor._replay_field_resolution_memory(field)
        self.assertIsNone(memory_entry)
        self.assertIsNone(task_type)
        self.assertIsNone(signature_payload)
        self.assertIsNone(resolution)

        self.assertFalse(
            executor._should_promote_field_resolution_memory(
                field,
                ResolvedFieldValue(
                    value="Não necessito de nenhuma acessibilidade",
                    answer_source=AnswerSource.AI,
                    fill_strategy=FillStrategy.AUTOFILL_AI,
                ),
            )
        )

    def test_executor_emits_explicit_memory_timeline_events(self) -> None:
        now = datetime.now(tz=UTC)
        stored = self.repository.save(
            ApplyActionMemory(
                id=uuid4(),
                task_type=TASK_PRIMARY_ACTION,
                signature_hash="abc123",
                signature_json=json.dumps({"signature": "v1"}),
                strategy_payload_json=json.dumps(
                    {
                        "action_type": "click",
                        "action_intent": "advance_step",
                        "anchor": {"kind": "priority_target"},
                        "scroll_amount": 0,
                        "scroll_direction": None,
                        "scroll_target": None,
                        "wait_seconds": 0,
                        "key_name": None,
                    }
                ),
                success_count=1,
                failure_count=0,
                created_at=now,
                last_used_at=now,
                last_succeeded_at=now,
                expires_at=now + timedelta(days=30),
            )
        )

        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
        executor._apply_memory = self.memory

        with patch(
            "job_applier.infrastructure.linkedin.easy_apply.append_timeline_event"
        ) as timeline:
            executor._record_apply_memory_success(stored, task_type=TASK_PRIMARY_ACTION)
            executor._record_apply_memory_failure(stored, task_type=TASK_PRIMARY_ACTION)

        event_types = [call.args[0] for call in timeline.call_args_list]
        self.assertEqual(event_types, ["apply_memory_refreshed", "apply_memory_degraded"])
        refresh_payload = timeline.call_args_list[0].args[1]
        degrade_payload = timeline.call_args_list[1].args[1]
        self.assertEqual(refresh_payload["task_type"], TASK_PRIMARY_ACTION)
        self.assertEqual(degrade_payload["task_type"], TASK_PRIMARY_ACTION)
        self.assertIn("success_count", refresh_payload)
        self.assertIn("failure_count", degrade_payload)


class _FakeCheckboxLocator:
    def __init__(
        self,
        *,
        kind: str,
        state: dict[str, object],
        exists: bool = True,
    ) -> None:
        self.kind = kind
        self.state = state
        self.exists = exists
        self.click_count = 0
        self.space_press_count = 0

    @property
    def first(self) -> _FakeCheckboxLocator:
        return self

    async def count(self) -> int:
        return 1 if self.exists else 0

    def locator(self, selector: str) -> _FakeCheckboxLocator:
        if not self.exists:
            return self
        if "@role='checkbox'" in selector:
            return self.state["role_locator"]  # type: ignore[return-value]
        return self.state["missing_locator"]  # type: ignore[return-value]

    async def check(self, timeout: int | None = None) -> None:
        msg = f"{self.kind} cannot be checked directly"
        raise RuntimeError(msg)

    async def uncheck(self, timeout: int | None = None) -> None:
        msg = f"{self.kind} cannot be unchecked directly"
        raise RuntimeError(msg)

    async def set_checked(
        self,
        desired_checked: bool,
        timeout: int | None = None,
        force: bool | None = None,
    ) -> None:
        msg = f"{self.kind} cannot be set_checked directly"
        raise RuntimeError(msg)

    async def get_attribute(self, name: str) -> str | None:
        if self.kind == "input" and name == "id":
            return "checkbox-id"
        if self.kind == "role" and name == "role":
            return "checkbox"
        return None

    async def click(
        self,
        timeout: int | None = None,
        force: bool | None = None,
    ) -> None:
        self.click_count += 1
        if self.kind == "role" and bool(self.state.get("toggle_on_click", True)):
            self.state["aria_checked"] = not bool(self.state["aria_checked"])

    async def focus(self) -> None:
        return None

    async def press(self, key: str, timeout: int | None = None) -> None:
        if self.kind == "role" and key == "Space" and bool(self.state.get("toggle_on_space", True)):
            self.space_press_count += 1
            self.state["aria_checked"] = not bool(self.state["aria_checked"])

    async def bounding_box(self) -> dict[str, float] | None:
        if self.kind == "role":
            return {"x": 10.0, "y": 20.0, "width": 200.0, "height": 24.0}
        return None

    async def evaluate(self, script: str, payload: dict[str, object] | None = None) -> object:
        if (
            self.kind == "input"
            and "desiredChecked" in script
            and bool(self.state.get("toggle_via_evaluate", False))
        ):
            self.state["aria_checked"] = bool((payload or {}).get("desiredChecked"))
            return True
        return False


class _FakeMouse:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state
        self.click_count = 0

    async def click(self, x: float, y: float) -> None:
        self.click_count += 1
        if bool(self.state.get("toggle_on_mouse", True)):
            self.state["aria_checked"] = not bool(self.state["aria_checked"])


class _FakePage:
    def __init__(self, state: dict[str, object]) -> None:
        self.mouse = _FakeMouse(state)

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


class EasyApplyCheckboxStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_checkbox_state_uses_ancestor_role_checkbox_for_hidden_input(self) -> None:
        state: dict[str, object] = {"aria_checked": False}
        missing_locator = _FakeCheckboxLocator(kind="missing", state=state, exists=False)
        role_locator = _FakeCheckboxLocator(kind="role", state=state)
        input_locator = _FakeCheckboxLocator(kind="input", state=state)
        state["role_locator"] = role_locator
        state["missing_locator"] = missing_locator

        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
        cast(Any, executor)._easy_apply_root = AsyncMock(return_value=missing_locator)
        cast(Any, executor)._find_control_locator = AsyncMock(return_value=input_locator)

        field = EasyApplyField(
            question_raw="Consent",
            normalized_key="consent",
            question_type=QuestionType.YES_NO_GENERIC,
            control_kind="checkbox",
            input_type="checkbox",
            required=True,
            dom_ref="job-applier-1",
        )

        async def fake_checkbox_option_is_checked(locator: _FakeCheckboxLocator) -> bool:
            if locator.kind in {"input", "role"}:
                return bool(state["aria_checked"])
            return False

        with patch(
            "job_applier.infrastructure.linkedin.easy_apply._checkbox_option_is_checked",
            side_effect=fake_checkbox_option_is_checked,
        ):
            applied = await PlaywrightLinkedInEasyApplyExecutor._set_checkbox_state(
                executor,
                page=cast(Any, _FakePage(state)),
                root=cast(Any, missing_locator),
                field=field,
                locator=cast(Any, input_locator),
                desired_checked=True,
            )

        self.assertTrue(applied)
        self.assertTrue(bool(state["aria_checked"]))
        self.assertGreaterEqual(role_locator.click_count + role_locator.space_press_count, 1)

    async def test_set_checkbox_state_falls_back_to_dom_toggle_when_surface_clicks_fail(
        self,
    ) -> None:
        state: dict[str, object] = {
            "aria_checked": False,
            "toggle_on_click": False,
            "toggle_on_space": False,
            "toggle_on_mouse": False,
            "toggle_via_evaluate": True,
        }
        missing_locator = _FakeCheckboxLocator(kind="missing", state=state, exists=False)
        role_locator = _FakeCheckboxLocator(kind="role", state=state)
        input_locator = _FakeCheckboxLocator(kind="input", state=state)
        state["role_locator"] = role_locator
        state["missing_locator"] = missing_locator

        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
        cast(Any, executor)._easy_apply_root = AsyncMock(return_value=missing_locator)
        cast(Any, executor)._find_control_locator = AsyncMock(return_value=input_locator)

        field = EasyApplyField(
            question_raw="Consent",
            normalized_key="consent",
            question_type=QuestionType.YES_NO_GENERIC,
            control_kind="checkbox",
            input_type="checkbox",
            required=True,
            dom_ref="job-applier-1",
        )

        async def fake_checkbox_option_is_checked(locator: _FakeCheckboxLocator) -> bool:
            if locator.kind in {"input", "role"}:
                return bool(state["aria_checked"])
            return False

        with patch(
            "job_applier.infrastructure.linkedin.easy_apply._checkbox_option_is_checked",
            side_effect=fake_checkbox_option_is_checked,
        ):
            applied = await PlaywrightLinkedInEasyApplyExecutor._set_checkbox_state(
                executor,
                page=cast(Any, _FakePage(state)),
                root=cast(Any, missing_locator),
                field=field,
                locator=cast(Any, input_locator),
                desired_checked=True,
            )

        self.assertTrue(applied)
        self.assertTrue(bool(state["aria_checked"]))


class EasyApplySemanticAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_field_semantic_acceptance_rejects_same_step_when_feedback_persists(self) -> None:
        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
        field = EasyApplyField(
            question_raw="Enter city or location",
            normalized_key="city",
            question_type=QuestionType.CITY,
            control_kind="text",
            input_type="text",
            required=True,
            prefilled=True,
            current_value="Sao Paulo",
            field_context="Location (city)*",
            dom_ref="job-applier-city",
        )
        current_step = EasyApplyStep(
            step_index=0,
            total_steps=3,
            surface_text="Contact info",
            fields=(
                EasyApplyField(
                    question_raw="Enter city or location",
                    normalized_key="city",
                    question_type=QuestionType.CITY,
                    control_kind="text",
                    input_type="text",
                    required=True,
                    prefilled=True,
                    current_value="Sao Paulo",
                    field_context="Location (city)* This field is required",
                    dom_ref="job-applier-city",
                ),
            ),
        )
        cast(Any, executor)._extract_step = AsyncMock(return_value=current_step)

        accepted = await (
            PlaywrightLinkedInEasyApplyExecutor._field_text_value_semantically_accepted(
                executor,
                page=cast(Any, object()),
                field=field,
                semantic_retry_required=True,
                last_known_step_index=0,
                last_known_total_steps=3,
            )
        )

        self.assertFalse(accepted)

    async def test_field_semantic_acceptance_accepts_when_step_changes(self) -> None:
        executor = object.__new__(PlaywrightLinkedInEasyApplyExecutor)
        field = EasyApplyField(
            question_raw="Enter city or location",
            normalized_key="city",
            question_type=QuestionType.CITY,
            control_kind="text",
            input_type="text",
            required=True,
            dom_ref="job-applier-city",
        )
        advanced_step = EasyApplyStep(
            step_index=1,
            total_steps=3,
            surface_text="Review",
            fields=(),
        )
        cast(Any, executor)._extract_step = AsyncMock(return_value=advanced_step)

        accepted = await (
            PlaywrightLinkedInEasyApplyExecutor._field_text_value_semantically_accepted(
                executor,
                page=cast(Any, object()),
                field=field,
                semantic_retry_required=True,
                last_known_step_index=0,
                last_known_total_steps=3,
            )
        )

        self.assertTrue(accepted)


class EasyApplySemanticRecoveryGuardrailTests(unittest.TestCase):
    def test_prefilled_invalid_text_field_requires_agentic_recovery(self) -> None:
        field = EasyApplyField(
            question_raw="Enter city or location",
            normalized_key="city",
            question_type=QuestionType.CITY,
            control_kind="text",
            input_type="text",
            current_value="Sao Paulo",
            prefilled=True,
            field_context="Location (city)* This field is required",
        )

        self.assertTrue(_field_requires_agentic_semantic_recovery(field))

    def test_prefilled_valid_text_field_does_not_require_agentic_recovery(self) -> None:
        field = EasyApplyField(
            question_raw="Portfolio URL",
            normalized_key="portfolio_url",
            question_type=QuestionType.PORTFOLIO_URL,
            control_kind="text",
            input_type="text",
            current_value="https://example.com",
            prefilled=True,
            field_context="Portfolio URL",
        )

        self.assertFalse(_field_requires_agentic_semantic_recovery(field))

    def test_non_text_field_does_not_require_agentic_recovery(self) -> None:
        field = EasyApplyField(
            question_raw="Email address*",
            normalized_key="email",
            question_type=QuestionType.EMAIL,
            control_kind="select",
            input_type="select",
            current_value="thiago@example.com",
            prefilled=True,
            field_context="This field is required",
            options=("thiago@example.com",),
        )

        self.assertFalse(_field_requires_agentic_semantic_recovery(field))


if __name__ == "__main__":
    unittest.main()
