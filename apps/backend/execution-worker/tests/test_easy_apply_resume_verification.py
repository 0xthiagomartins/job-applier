from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import TracebackType
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from job_applier.domain.entities import ApplicationAnswer
from job_applier.domain.enums import AnswerSource, DebugExecutionStage, FillStrategy, QuestionType
from job_applier.infrastructure.linkedin.easy_apply import (
    EasyApplyStep,
    PlaywrightLinkedInEasyApplyExecutor,
    ResumeUploadSettleState,
    _evaluate_resume_verification,
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

        self.assertTrue(uploaded)
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

    def test_marks_resume_verified_when_answer_matches_target(self) -> None:
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
            return_value=True
        )
        executor._easy_apply_root = AsyncMock(  # type: ignore[method-assign]
            side_effect=(first_root, second_root)
        )
        executor._resume_picker_selection_matches_requested_cv = AsyncMock(  # type: ignore[method-assign]
            side_effect=(False, True)
        )
        executor._reload_resume_choice_field = AsyncMock(  # type: ignore[method-assign]
            return_value=refreshed_field
        )
        executor._check_radio_option_by_index = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )

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
        executor._reload_resume_choice_field.assert_awaited_once()
        self.assertEqual(executor._check_radio_option_by_index.await_count, 2)


if __name__ == "__main__":
    unittest.main()
