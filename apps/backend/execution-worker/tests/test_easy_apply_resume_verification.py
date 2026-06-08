from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

from job_applier.domain.entities import ApplicationAnswer
from job_applier.domain.enums import AnswerSource, DebugExecutionStage, FillStrategy, QuestionType
from job_applier.infrastructure.linkedin.easy_apply import (
    EasyApplyStep,
    PlaywrightLinkedInEasyApplyExecutor,
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


if __name__ == "__main__":
    unittest.main()
