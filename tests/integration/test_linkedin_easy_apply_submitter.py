import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import AnyUrl, SecretStr, TypeAdapter

from job_applier.application.config import (
    AgentConfig,
    AIConfig,
    RulesetConfig,
    ScheduleConfig,
    SearchConfig,
    UserAgentSettings,
    UserProfileConfig,
)
from job_applier.application.history import SubmissionHistoryFilters
from job_applier.domain import (
    AnswerSource,
    ApplicationAnswer,
    ArtifactSnapshot,
    ArtifactType,
    ExecutionOrigin,
    FillStrategy,
    QuestionType,
    ScheduleFrequency,
    SeniorityLevel,
    SubmissionStatus,
    WorkplaceType,
)
from job_applier.infrastructure.linkedin.easy_apply import (
    EasyApplyExecutionResult,
    LinkedInEasyApplySubmitter,
)
from job_applier.infrastructure.sqlite import (
    SqliteAnswerRepository,
    SqliteArtifactSnapshotRepository,
    SqliteJobPostingRepository,
    SqliteProfileSnapshotRepository,
    SqliteSubmissionHistoryRepository,
    SqliteSubmissionRepository,
    create_session_factory,
)
from tests.integration.sqlite_helpers import build_posting, upgrade_to_head, utc_dt


def test_easy_apply_submitter_persists_only_successful_submissions(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'easy-apply.db').resolve()}"
    upgrade_to_head(database_url)

    cv_path = tmp_path / "resume.pdf"
    cv_path.write_bytes(b"resume-content")

    session_factory = create_session_factory(database_url)
    posting_repo = SqliteJobPostingRepository(session_factory)
    submission_repo = SqliteSubmissionRepository(session_factory)
    answer_repo = SqliteAnswerRepository(session_factory)
    snapshot_repo = SqliteProfileSnapshotRepository(session_factory)
    artifact_repo = SqliteArtifactSnapshotRepository(session_factory)
    history_repo = SqliteSubmissionHistoryRepository(session_factory)

    posting = posting_repo.save(
        build_posting(
            company_name="Acme",
            title="Automation Engineer",
            external_job_id="job-777",
            captured_at=utc_dt(28, 10),
        ),
    )
    settings = build_user_agent_settings(cv_path)

    class FakeExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, settings, posting, *, origin):
            self.calls += 1
            if self.calls == 1:
                submission_id = uuid4()
                submitted_at = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
                return EasyApplyExecutionResult(
                    submission_id=submission_id,
                    started_at=datetime(2026, 3, 28, 22, 58, tzinfo=UTC),
                    status=SubmissionStatus.SUBMITTED,
                    notes="submitted successfully",
                    answers=(
                        ApplicationAnswer(
                            submission_id=submission_id,
                            step_index=0,
                            question_raw="Are you authorized to work in Brazil?",
                            question_type=QuestionType.WORK_AUTHORIZATION,
                            normalized_key="work_authorization",
                            answer_raw="Yes",
                            answer_source=AnswerSource.PROFILE_SNAPSHOT,
                            fill_strategy=FillStrategy.DETERMINISTIC,
                        ),
                    ),
                    artifacts=(
                        ArtifactSnapshot(
                            submission_id=submission_id,
                            artifact_type=ArtifactType.CV_METADATA,
                            path=str(cv_path),
                            sha256=sha256(cv_path.read_bytes()).hexdigest(),
                            created_at=submitted_at,
                        ),
                    ),
                    submitted_at=submitted_at,
                    cv_version="resume.pdf",
                )
            return EasyApplyExecutionResult(
                submission_id=uuid4(),
                started_at=datetime(2026, 3, 28, 23, 5, tzinfo=UTC),
                status=SubmissionStatus.FAILED,
                notes="validation error: missing answer",
            )

    submitter = LinkedInEasyApplySubmitter(
        executor=FakeExecutor(),
        submission_repository=submission_repo,
        answer_repository=answer_repo,
        profile_snapshot_repository=snapshot_repo,
        artifact_repository=artifact_repo,
    )

    successful_attempt = asyncio.run(
        submitter.submit(settings, posting, origin=ExecutionOrigin.MANUAL),
    )
    failed_attempt = asyncio.run(
        submitter.submit(settings, posting, origin=ExecutionOrigin.MANUAL),
    )

    assert successful_attempt.submission.status is SubmissionStatus.SUBMITTED
    assert successful_attempt.successful_record is not None
    assert failed_attempt.submission.status is SubmissionStatus.FAILED

    persisted_submission = submission_repo.get(successful_attempt.submission.id)
    assert persisted_submission is not None
    assert persisted_submission.profile_snapshot_id is not None
    assert persisted_submission.cv_version == "resume.pdf"
    assert submission_repo.get(failed_attempt.submission.id) is None

    answers = answer_repo.list_for_submission(successful_attempt.submission.id)
    artifacts = artifact_repo.list_for_submission(successful_attempt.submission.id)
    snapshots = snapshot_repo.list()
    history = history_repo.query(SubmissionHistoryFilters())

    assert len(answers) == 1
    assert answers[0].normalized_key == "work_authorization"
    assert len(artifacts) == 1
    assert len(snapshots) == 1
    assert history.total == 1
    assert history.items[0].submission.id == successful_attempt.submission.id


def build_user_agent_settings(cv_path: Path) -> UserAgentSettings:
    url_adapter = TypeAdapter(AnyUrl)
    return UserAgentSettings(
        config_version="config-v1",
        profile=UserProfileConfig(
            name="Thiago Martins",
            email="thiago@example.com",
            phone="+5511999999999",
            city="Sao Paulo",
            linkedin_url=url_adapter.validate_python("https://www.linkedin.com/in/thiago"),
            github_url=url_adapter.validate_python("https://github.com/0xthiagomartins"),
            portfolio_url=url_adapter.validate_python("https://thiago.example.com"),
            years_experience_by_stack={"python": 8},
            work_authorized=True,
            availability="Immediate",
            default_responses={"work_authorization": "Yes"},
            cv_path=str(cv_path),
            cv_filename="resume.pdf",
        ),
        search=SearchConfig(
            keywords=("python", "automation"),
            location="Remote",
            posted_within_hours=24,
            workplace_types=(WorkplaceType.REMOTE,),
            seniority=(SeniorityLevel.SENIOR,),
            easy_apply_only=True,
        ),
        agent=AgentConfig(
            schedule=ScheduleConfig(
                frequency=ScheduleFrequency.DAILY,
                run_at="23:00",
                timezone="UTC",
            ),
            auto_connect_with_recruiter=False,
        ),
        ai=AIConfig(api_key=SecretStr("sk-test"), model="o3-mini"),
        ruleset=RulesetConfig(version="ruleset-v1"),
    )
