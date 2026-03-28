import json
from datetime import datetime
from uuid import uuid4

import pytest

from job_applier.domain import (
    AnswerSource,
    ApplicationAnswer,
    ApplicationSubmission,
    ExecutionOrigin,
    FillStrategy,
    JobPosting,
    Platform,
    QuestionType,
    SubmissionStatus,
)


def test_job_posting_generates_description_hash_and_requires_utc() -> None:
    posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/123",
        title="Python Engineer",
        company_name="Acme",
        description_raw="Build automation workflows.",
    )

    assert len(posting.description_hash) == 64
    assert posting.captured_at.tzinfo is not None
    assert posting.easy_apply is True

    with pytest.raises(ValueError, match="captured_at must be timezone-aware"):
        JobPosting(
            platform=Platform.LINKEDIN,
            url="https://www.linkedin.com/jobs/view/124",
            title="Python Engineer",
            company_name="Acme",
            description_raw="Build automation workflows.",
            captured_at=datetime(2026, 3, 28, 23, 0, 0),
        )


def test_submitted_submission_requires_snapshot_and_ruleset() -> None:
    with pytest.raises(ValueError, match="submitted submissions require"):
        ApplicationSubmission(
            job_posting_id=uuid4(),
            status=SubmissionStatus.SUBMITTED,
            execution_origin=ExecutionOrigin.SCHEDULED,
        )


def test_best_effort_answers_must_be_marked_as_ambiguous() -> None:
    with pytest.raises(ValueError, match="best-effort answers must be marked as ambiguous"):
        ApplicationAnswer(
            submission_id=uuid4(),
            step_index=1,
            question_raw="What is your work authorization?",
            question_type=QuestionType.WORK_AUTHORIZATION,
            normalized_key="work_authorization",
            answer_raw="Yes",
            answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
            fill_strategy=FillStrategy.BEST_EFFORT,
        )

    answer = ApplicationAnswer(
        submission_id=uuid4(),
        step_index=1,
        question_raw="What is your work authorization?",
        question_type=QuestionType.WORK_AUTHORIZATION,
        normalized_key="work_authorization",
        answer_raw="Yes",
        answer_source=AnswerSource.BEST_EFFORT_AUTOFILL,
        fill_strategy=FillStrategy.BEST_EFFORT,
        ambiguity_flag=True,
    )

    assert answer.ambiguity_flag is True
    assert json.loads(json.dumps({"answer": answer.answer_raw})) == {"answer": "Yes"}
