from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import TracebackType
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from job_applier.domain.entities import ApplicationAnswer, ExecutionEvent
from job_applier.domain.enums import AnswerSource, DebugExecutionStage, FillStrategy, QuestionType
from job_applier.infrastructure.linkedin.easy_apply import (
    EasyApplyStep,
    LinkedInEasyApplyError,
    PlaywrightLinkedInEasyApplyExecutor,
    ResumeUploadSettleState,
    _evaluate_resume_verification,
    _radio_option_input_id,
    _radio_option_is_checked,
)
from job_applier.infrastructure.linkedin.question_resolution import EasyApplyField
from job_applier.settings import RuntimeSettings


def _resume_field(
    *,
    current_value: str,
    options: tuple[str, ...],
) -> EasyApplyField:
    return EasyApplyField(
        question_raw="Resume*",
        normalized_key="resume_upload",
        question_type=QuestionType.RESUME_UPLOAD,
        control_kind="radio",
        input_type="radio",
        required=True,
        prefilled=bool(current_value),
        current_value=current_value,
        options=options,
    )


class ResumeVerificationTests(unittest.TestCase):
    def test_resume_upload_prefers_official_trigger_before_page_file_input(self) -> None:
        class _ChooserContext:
            def __init__(self, chooser: AsyncMock) -> None:
                self._chooser = chooser

            async def __aenter__(self) -> object:
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                future.set_result(self._chooser)
                return type("_ChooserInfo", (), {"value": future})()

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                tb: TracebackType | None,
            ) -> bool:
                return False

        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        upload_trigger = AsyncMock()
        chooser = AsyncMock()
        page = AsyncMock()
        page.expect_file_chooser = Mock(return_value=_ChooserContext(chooser))
        executor._locate_resume_upload_trigger = AsyncMock(  # type: ignore[method-assign]
            return_value=upload_trigger
        )
        executor._locate_resume_file_input = AsyncMock(  # type: ignore[method-assign]
            return_value=AsyncMock()
        )
        executor._await_resume_upload_settlement = AsyncMock(  # type: ignore[method-assign]
            return_value=ResumeUploadSettleState(target_visible=True)
        )

        uploaded = asyncio.run(
            executor._upload_resume_from_choice_step(
                page=page,
                root=AsyncMock(),
                submission_cv_path=Path(__file__),
                target_cv_name="target-resume.pdf",
            )
        )

        assert uploaded is not None
        self.assertTrue(uploaded.target_visible)
        upload_trigger.click.assert_awaited_once()
        chooser.set_files.assert_awaited_once()
        executor._locate_resume_file_input.assert_not_awaited()

    def test_flags_visible_target_when_another_resume_is_selected(self) -> None:
        step = EasyApplyStep(
            step_index=1,
            total_steps=4,
            fields=(
                _resume_field(
                    current_value="PDF afc9942c-full-stack-developer-tailored.pdf 6/8/2026",
                    options=(
                        "PDF afc9942c-full-stack-developer-tailored.pdf 6/8/2026",
                        "PDF e8ceaf02-principal-back-end-engineer-tailored.pdf 6/8/2026",
                    ),
                ),
            ),
            surface_text="Resume step",
        )

        verification = _evaluate_resume_verification(
            step,
            (),
            target_cv_name="e8ceaf02-principal-back-end-engineer-tailored.pdf",
        )

        self.assertFalse(verification.verified)
        self.assertTrue(verification.option_visible)
        self.assertEqual(
            verification.selected_value,
            "pdf afc9942c-full-stack-developer-tailored.pdf 6/8/2026",
        )
        self.assertEqual(verification.reason, "picker_selected_different_resume")

    def test_resume_verification_does_not_trust_answer_when_picker_is_stale(self) -> None:
        step = EasyApplyStep(
            step_index=1,
            total_steps=4,
            fields=(
                _resume_field(
                    current_value="PDF afc9942c-full-stack-developer-tailored.pdf 6/8/2026",
                    options=("PDF afc9942c-full-stack-developer-tailored.pdf 6/8/2026",),
                ),
            ),
            surface_text="Resume step",
        )
        answers = (
            ApplicationAnswer(
                submission_id=uuid4(),
                step_index=1,
                question_raw="Resume*",
                question_type=QuestionType.RESUME_UPLOAD,
                normalized_key="resume_upload",
                answer_raw="e8ceaf02-principal-back-end-engineer-tailored.pdf",
                answer_source=AnswerSource.PROFILE_SNAPSHOT,
                fill_strategy=FillStrategy.DETERMINISTIC,
                ambiguity_flag=False,
            ),
        )

        verification = _evaluate_resume_verification(
            step,
            answers,
            target_cv_name="e8ceaf02-principal-back-end-engineer-tailored.pdf",
        )

        self.assertFalse(verification.verified)
        self.assertEqual(verification.reason, "picker_missing_target_resume")

    def test_resume_verification_marks_picker_match_as_verified(self) -> None:
        step = EasyApplyStep(
            step_index=1,
            total_steps=4,
            fields=(
                _resume_field(
                    current_value="PDF e8ceaf02-principal-back-end-engineer-tailored.pdf 6/8/2026",
                    options=("PDF e8ceaf02-principal-back-end-engineer-tailored.pdf 6/8/2026",),
                ),
            ),
            surface_text="Resume step",
        )

        verification = _evaluate_resume_verification(
            step,
            (),
            target_cv_name="e8ceaf02-principal-back-end-engineer-tailored.pdf",
        )

        self.assertTrue(verification.verified)
        self.assertEqual(verification.reason, "verified")

    def test_resume_verification_accepts_truncated_target_with_unique_prefix(self) -> None:
        step = EasyApplyStep(
            step_index=1,
            total_steps=4,
            fields=(
                _resume_field(
                    current_value="PDF fb71e2ab-fullstack-engineer-react-node-tailor… 6/10/2026",
                    options=("PDF fb71e2ab-fullstack-engineer-react-node-tailor… 6/10/2026",),
                ),
            ),
            surface_text="Resume step",
        )

        verification = _evaluate_resume_verification(
            step,
            (),
            target_cv_name="fb71e2ab-fullstack-engineer-react-node-tailored.pdf",
        )

        self.assertTrue(verification.verified)
        self.assertEqual(verification.reason, "verified")

    def test_review_repair_surfaces_stale_resume_after_verified_picker_selection(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        executor._extract_easy_apply_review_sections = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "resume": {
                    "edit_ref": "resume",
                    "body_text": "PDF afc9942c-full-stack-developer-tailored.pdf 6/8/2026",
                }
            }
        )

        step = EasyApplyStep(
            step_index=3,
            total_steps=4,
            fields=(),
            surface_text="Review your application",
        )

        repair_reason = asyncio.run(
            executor._maybe_repair_easy_apply_review(
                None,  # type: ignore[arg-type]
                step=step,
                settings=object(),  # type: ignore[arg-type]
                execution_id=uuid4(),
                submission_id=uuid4(),
                execution_events=[],
                submission_cv_path=Path("9fcb3469-principal-back-end-engineer-tailored.pdf"),
                resume_review_repair_attempted=True,
                resume_review_verified_selection=True,
            )
        )

        self.assertEqual(repair_reason, "resume_preview_stale_after_verified_selection")

    def test_resume_upload_wait_polls_until_target_becomes_visible(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        executor._inspect_resume_upload_settlement = AsyncMock(  # type: ignore[method-assign]
            side_effect=(
                ResumeUploadSettleState(),
                ResumeUploadSettleState(uploading=True),
                ResumeUploadSettleState(target_visible=True),
            )
        )

        page = AsyncMock()
        settled = asyncio.run(
            executor._await_resume_upload_settlement(
                page,
                target_cv_name="9fcb3469-principal-back-end-engineer-tailored.pdf",
                timeout_ms=1_000,
                poll_interval_ms=1,
            )
        )

        self.assertTrue(settled.target_visible)
        self.assertEqual(executor._inspect_resume_upload_settlement.await_count, 3)

    def test_resume_upload_wait_returns_last_state_on_timeout(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        executor._inspect_resume_upload_settlement = AsyncMock(  # type: ignore[method-assign]
            return_value=ResumeUploadSettleState(uploading=True, status_text="Uploading")
        )

        page = AsyncMock()
        settled = asyncio.run(
            executor._await_resume_upload_settlement(
                page,
                target_cv_name="9fcb3469-principal-back-end-engineer-tailored.pdf",
                timeout_ms=5,
                poll_interval_ms=1,
            )
        )

        self.assertTrue(settled.uploading)
        self.assertFalse(settled.settled)

    def test_reload_resume_verification_state_uses_live_refreshed_field(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        stale_field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=("PDF stale-resume.pdf 6/8/2026",),
        )
        refreshed_field = _resume_field(
            current_value="PDF target-resume.pdf 6/9/2026",
            options=("PDF target-resume.pdf 6/9/2026",),
        )
        executor._easy_apply_root = AsyncMock(return_value=AsyncMock())  # type: ignore[method-assign]
        executor._resume_picker_selection_matches_requested_cv = AsyncMock(  # type: ignore[method-assign]
            return_value=False
        )
        executor._resume_picker_target_option_is_selected = AsyncMock(  # type: ignore[method-assign]
            return_value=False
        )
        executor._reload_resume_choice_field = AsyncMock(  # type: ignore[method-assign]
            return_value=refreshed_field
        )

        state = asyncio.run(
            executor._reload_resume_verification_state(
                page=AsyncMock(),
                field=stale_field,
                target_cv_name="target-resume.pdf",
                step_index=2,
                total_steps=5,
            )
        )

        self.assertTrue(state.verified)
        self.assertEqual(state.reason, "verified")

    def test_reload_resume_verification_state_prefers_live_picker_selection(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        stale_field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=("PDF stale-resume.pdf 6/8/2026",),
        )
        executor._easy_apply_root = AsyncMock(return_value=AsyncMock())  # type: ignore[method-assign]
        executor._resume_picker_selection_matches_requested_cv = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        executor._reload_resume_choice_field = AsyncMock(  # type: ignore[method-assign]
            return_value=stale_field
        )

        state = asyncio.run(
            executor._reload_resume_verification_state(
                page=AsyncMock(),
                field=stale_field,
                target_cv_name="target-resume.pdf",
                step_index=2,
                total_steps=5,
            )
        )

        self.assertTrue(state.verified)
        self.assertEqual(state.reason, "verified")
        self.assertEqual(state.selected_value, "target-resume.pdf")
        executor._reload_resume_choice_field.assert_not_awaited()

    def test_reload_resume_verification_state_accepts_live_checked_target_option(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        refreshed_field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=(
                "PDF stale-resume.pdf 6/8/2026",
                "PDF target-resume.pdf 6/9/2026",
            ),
        )
        executor._easy_apply_root = AsyncMock(return_value=AsyncMock())  # type: ignore[method-assign]
        executor._resume_picker_selection_matches_requested_cv = AsyncMock(  # type: ignore[method-assign]
            return_value=False
        )
        executor._resume_picker_target_option_is_selected = AsyncMock(  # type: ignore[method-assign]
            side_effect=(False, True)
        )
        executor._reload_resume_choice_field = AsyncMock(  # type: ignore[method-assign]
            return_value=refreshed_field
        )

        state = asyncio.run(
            executor._reload_resume_verification_state(
                page=AsyncMock(),
                field=_resume_field(
                    current_value="PDF stale-resume.pdf 6/8/2026",
                    options=("PDF stale-resume.pdf 6/8/2026",),
                ),
                target_cv_name="target-resume.pdf",
                step_index=2,
                total_steps=5,
            )
        )

        self.assertTrue(state.verified)
        self.assertEqual(state.reason, "verified")

    def test_resume_submit_footer_label_only_returns_submit_cta(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        step = EasyApplyStep(
            step_index=4,
            total_steps=5,
            fields=(),
            surface_text="Final step",
        )
        executor._easy_apply_root = AsyncMock(return_value=AsyncMock())  # type: ignore[method-assign]
        executor._locate_easy_apply_footer_primary_button = AsyncMock(  # type: ignore[method-assign]
            side_effect=((AsyncMock(), "Submit application"), (AsyncMock(), "Review"))
        )

        submit_label = asyncio.run(
            executor._resume_submit_footer_label(
                AsyncMock(),
                step=step,
            )
        )
        review_label = asyncio.run(
            executor._resume_submit_footer_label(
                AsyncMock(),
                step=step,
            )
        )

        self.assertEqual(submit_label, "Submit application")
        self.assertIsNone(review_label)

    def test_resume_choice_reloads_picker_after_upload_to_select_new_target(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        stale_field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=("PDF stale-resume.pdf 6/8/2026",),
        )
        refreshed_field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=(
                "PDF stale-resume.pdf 6/8/2026",
                "PDF target-resume.pdf 6/8/2026",
            ),
        )
        first_root = AsyncMock()
        second_root = AsyncMock()
        executor._upload_resume_from_choice_step = AsyncMock(  # type: ignore[method-assign]
            return_value=ResumeUploadSettleState(success_feedback=True)
        )
        executor._easy_apply_root = AsyncMock(  # type: ignore[method-assign]
            side_effect=(first_root, second_root)
        )
        executor._resume_picker_selection_matches_requested_cv = AsyncMock(  # type: ignore[method-assign]
            side_effect=(False, True)
        )
        executor._reload_resume_choice_field_until_target_available = AsyncMock(  # type: ignore[method-assign]
            return_value=refreshed_field
        )
        executor._select_resume_option_by_target_name = AsyncMock(  # type: ignore[method-assign]
            return_value=False
        )
        executor._check_radio_option_by_index = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        executor._complete_radio_interaction = AsyncMock(return_value=True)  # type: ignore[method-assign]

        applied = asyncio.run(
            executor._apply_resume_choice_field(
                page=AsyncMock(),
                root=AsyncMock(),
                field=stale_field,
                settings=object(),  # type: ignore[arg-type]
                submission_cv_path=Path("target-resume.pdf"),
                step_index=1,
                total_steps=4,
            )
        )

        self.assertEqual(applied, "target-resume.pdf")
        self.assertEqual(executor._resume_picker_selection_matches_requested_cv.await_count, 2)
        executor._reload_resume_choice_field_until_target_available.assert_awaited_once()
        self.assertEqual(executor._check_radio_option_by_index.await_count, 2)

    def test_resume_choice_accepts_live_checked_target_after_upload(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        stale_field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=("PDF stale-resume.pdf 6/8/2026",),
        )
        refreshed_field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=(
                "PDF stale-resume.pdf 6/8/2026",
                "PDF target-resume.pdf 6/8/2026",
            ),
        )
        first_root = AsyncMock()
        second_root = AsyncMock()
        executor._upload_resume_from_choice_step = AsyncMock(  # type: ignore[method-assign]
            return_value=ResumeUploadSettleState(success_feedback=True)
        )
        executor._easy_apply_root = AsyncMock(  # type: ignore[method-assign]
            side_effect=(first_root, second_root)
        )
        executor._resume_picker_selection_matches_requested_cv = AsyncMock(  # type: ignore[method-assign]
            return_value=False
        )
        executor._resume_picker_target_option_is_selected = AsyncMock(  # type: ignore[method-assign]
            side_effect=(False, True)
        )
        executor._reload_resume_choice_field_until_target_available = AsyncMock(  # type: ignore[method-assign]
            return_value=refreshed_field
        )
        executor._select_resume_option_by_target_name = AsyncMock(  # type: ignore[method-assign]
            return_value=False
        )
        executor._check_radio_option_by_index = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        executor._complete_radio_interaction = AsyncMock(return_value=True)  # type: ignore[method-assign]

        applied = asyncio.run(
            executor._apply_resume_choice_field(
                page=AsyncMock(),
                root=AsyncMock(),
                field=stale_field,
                settings=object(),  # type: ignore[arg-type]
                submission_cv_path=Path("target-resume.pdf"),
                step_index=1,
                total_steps=4,
            )
        )

        self.assertEqual(applied, "target-resume.pdf")
        executor._reload_resume_choice_field_until_target_available.assert_awaited_once()
        executor._complete_radio_interaction.assert_not_awaited()

    def test_select_resume_option_by_target_name_accepts_live_checked_target(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        root = AsyncMock()
        option_locator = AsyncMock()
        executor._resolve_resume_option_locator_by_name = AsyncMock(  # type: ignore[method-assign]
            return_value=option_locator
        )
        executor._resume_picker_selection_matches_requested_cv = AsyncMock(  # type: ignore[method-assign]
            return_value=False
        )
        executor._activate_radio_option = AsyncMock(return_value=True)  # type: ignore[method-assign]
        executor._easy_apply_root = AsyncMock(return_value=AsyncMock())  # type: ignore[method-assign]
        executor._resume_picker_target_option_is_selected = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )

        selected = asyncio.run(
            executor._select_resume_option_by_target_name(
                page=AsyncMock(),
                root=root,
                field=_resume_field(
                    current_value="PDF stale-resume.pdf 6/8/2026",
                    options=(
                        "PDF stale-resume.pdf 6/8/2026",
                        "PDF target-resume.pdf 6/8/2026",
                    ),
                ),
                target_cv_name="target-resume.pdf",
                force_activate=True,
            )
        )

        self.assertTrue(selected)
        executor._activate_radio_option.assert_awaited_once()

    def test_resume_choice_skips_agentic_fallback_when_target_never_materializes(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        stale_field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=("PDF stale-resume.pdf 6/8/2026",),
        )
        executor._upload_resume_from_choice_step = AsyncMock(  # type: ignore[method-assign]
            return_value=ResumeUploadSettleState(success_feedback=True)
        )
        executor._easy_apply_root = AsyncMock(return_value=AsyncMock())  # type: ignore[method-assign]
        executor._resume_picker_selection_matches_requested_cv = AsyncMock(  # type: ignore[method-assign]
            return_value=False
        )
        executor._reload_resume_choice_field_until_target_available = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )
        executor._complete_radio_interaction = AsyncMock(return_value=True)  # type: ignore[method-assign]

        applied = asyncio.run(
            executor._apply_resume_choice_field(
                page=AsyncMock(),
                root=AsyncMock(),
                field=stale_field,
                settings=object(),  # type: ignore[arg-type]
                submission_cv_path=Path("target-resume.pdf"),
                step_index=1,
                total_steps=4,
            )
        )

        self.assertIsNone(applied)
        executor._complete_radio_interaction.assert_not_awaited()

    def test_resume_choice_timeout_raises_easy_apply_error(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                    "linkedin_field_interaction_timeout_seconds": 0.001,
                }
            )
        )
        field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=("PDF stale-resume.pdf 6/8/2026",),
        )

        async def _stall(**_: object) -> str | None:
            await asyncio.sleep(0.01)
            return None

        executor._apply_resume_choice_field = _stall  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            LinkedInEasyApplyError,
            (
                "Timed out while the browser agent was trying to finalize the "
                "LinkedIn Easy Apply resume chooser."
            ),
        ):
            asyncio.run(
                executor._apply_resume_choice_field_with_timeout(
                    page=AsyncMock(),
                    root=AsyncMock(),
                    field=field,
                    settings=object(),  # type: ignore[arg-type]
                    submission_cv_path=Path("target-resume.pdf"),
                    execution_events=[],
                    execution_id=uuid4(),
                    submission_id=uuid4(),
                    step_index=1,
                    total_steps=4,
                )
            )

    def test_resume_choice_timeout_records_diagnostic_event(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                    "linkedin_field_interaction_timeout_seconds": 0.001,
                }
            )
        )
        field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=("PDF stale-resume.pdf 6/8/2026",),
        )
        execution_events: list[ExecutionEvent] = []

        async def _stall(**_: object) -> str | None:
            await asyncio.sleep(0.01)
            return None

        executor._apply_resume_choice_field = _stall  # type: ignore[method-assign]

        with self.assertRaises(LinkedInEasyApplyError):
            asyncio.run(
                executor._apply_resume_choice_field_with_timeout(
                    page=AsyncMock(),
                    root=AsyncMock(),
                    field=field,
                    settings=object(),  # type: ignore[arg-type]
                    submission_cv_path=Path("target-resume.pdf"),
                    execution_events=execution_events,
                    execution_id=uuid4(),
                    submission_id=uuid4(),
                    step_index=2,
                    total_steps=4,
                )
            )

        self.assertEqual(
            json.loads(execution_events[-1].payload_json)["stage"],
            "easy_apply_resume_reassert_timeout",
        )

    def test_activate_radio_option_uses_js_fallback_before_label_lookup(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        executor._resolve_radio_input_from_locator = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )
        locator = AsyncMock()
        locator.check.side_effect = Exception("no direct check")
        locator.click.side_effect = Exception("no direct click")
        locator.evaluate.return_value = True
        locator.get_attribute.side_effect = AssertionError("should not read id")

        with patch(
            "job_applier.infrastructure.linkedin.easy_apply._radio_option_is_checked",
            new=AsyncMock(return_value=True),
        ):
            activated = asyncio.run(
                executor._activate_radio_option(
                    AsyncMock(),
                    locator,
                )
            )

        self.assertTrue(activated)
        locator.get_attribute.assert_not_called()

    def test_radio_option_input_id_returns_none_when_nested_lookup_stalls(self) -> None:
        locator = AsyncMock()
        locator.get_attribute.return_value = None

        async def _stall(*_: object, **__: object) -> None:
            await asyncio.sleep(2)
            return None

        locator.evaluate.side_effect = _stall

        input_id = asyncio.run(_radio_option_input_id(locator))

        self.assertIsNone(input_id)
        locator.get_attribute.assert_awaited_once()
        locator.evaluate.assert_awaited_once()

    def test_radio_option_is_checked_returns_false_when_locator_stalls(self) -> None:
        locator = AsyncMock()

        async def _stall(*_: object, **__: object) -> None:
            await asyncio.sleep(2)
            return None

        locator.is_checked.side_effect = _stall
        locator.evaluate.side_effect = _stall

        checked = asyncio.run(_radio_option_is_checked(locator))

        self.assertFalse(checked)
        locator.is_checked.assert_awaited_once()
        locator.evaluate.assert_awaited_once()

    def test_check_radio_option_revalidates_after_activation(self) -> None:
        executor = PlaywrightLinkedInEasyApplyExecutor(
            RuntimeSettings().model_copy(
                update={
                    "resolved_agent_debug_stage": DebugExecutionStage.FULL,
                    "agent_test_mode": True,
                }
            )
        )
        field = _resume_field(
            current_value="PDF stale-resume.pdf 6/8/2026",
            options=("PDF target-resume.pdf 6/9/2026",),
        )
        executor._resolve_radio_option_locator = AsyncMock(  # type: ignore[method-assign]
            return_value=AsyncMock()
        )
        executor._activate_radio_option = AsyncMock(return_value=True)  # type: ignore[method-assign]
        executor._radio_option_is_selected = AsyncMock(return_value=False)  # type: ignore[method-assign]
        executor._click_radio_text_target = AsyncMock(return_value=False)  # type: ignore[method-assign]
        executor._force_radio_option_via_dom = AsyncMock(return_value=False)  # type: ignore[method-assign]

        checked = asyncio.run(
            executor._check_radio_option_by_index(
                AsyncMock(),
                field,
                option_index=0,
                force_activate=True,
            )
        )

        self.assertFalse(checked)
        executor._activate_radio_option.assert_awaited_once()
        self.assertGreaterEqual(executor._radio_option_is_selected.await_count, 1)


if __name__ == "__main__":
    unittest.main()
