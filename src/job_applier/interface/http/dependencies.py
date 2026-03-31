"""Shared FastAPI dependencies."""

from functools import lru_cache

from job_applier.application.agent_execution import AgentExecutionOrchestrator
from job_applier.application.agent_scheduler import AgentScheduler
from job_applier.application.job_scoring import RuleBasedJobScorer
from job_applier.infrastructure import (
    InMemorySuccessfulSubmissionStore,
    LocalExecutionStore,
    LocalPanelSettingsStore,
    MirroredExecutionStore,
)
from job_applier.infrastructure.linkedin import (
    LinkedInEasyApplySubmitter,
    LinkedInJobFetcher,
    PlaywrightLinkedInEasyApplyExecutor,
    PlaywrightLinkedInJobsClient,
)
from job_applier.infrastructure.sqlite import (
    SqliteAnswerRepository,
    SqliteArtifactSnapshotRepository,
    SqliteExecutionEventRepository,
    SqliteJobPostingRepository,
    SqliteProfileSnapshotRepository,
    SqliteRecruiterInteractionRepository,
    SqliteSubmissionHistoryRepository,
    SqliteSubmissionRepository,
    create_session_factory,
)
from job_applier.infrastructure.sqlite.database import SessionFactory
from job_applier.settings import get_runtime_settings


@lru_cache(maxsize=1)
def get_panel_settings_store() -> LocalPanelSettingsStore:
    """Return the panel settings store singleton."""

    settings = get_runtime_settings()
    return LocalPanelSettingsStore(
        root_dir=settings.resolved_panel_storage_dir,
        runtime_settings=settings,
    )


@lru_cache(maxsize=1)
def get_execution_store() -> LocalExecutionStore:
    """Return the local execution store singleton."""

    settings = get_runtime_settings()
    return MirroredExecutionStore(
        event_repository=get_execution_event_repository(),
        root_dir=settings.data_dir / "executions",
    )


@lru_cache(maxsize=1)
def get_database_session_factory() -> SessionFactory:
    """Return the shared SQLAlchemy session factory."""

    settings = get_runtime_settings()
    return create_session_factory(settings.resolved_database_url)


@lru_cache(maxsize=1)
def get_job_posting_repository() -> SqliteJobPostingRepository:
    """Return the SQLite-backed job posting repository."""

    return SqliteJobPostingRepository(get_database_session_factory())


@lru_cache(maxsize=1)
def get_submission_repository() -> SqliteSubmissionRepository:
    """Return the SQLite-backed submission repository."""

    return SqliteSubmissionRepository(get_database_session_factory())


@lru_cache(maxsize=1)
def get_answer_repository() -> SqliteAnswerRepository:
    """Return the SQLite-backed answer repository."""

    return SqliteAnswerRepository(get_database_session_factory())


@lru_cache(maxsize=1)
def get_profile_snapshot_repository() -> SqliteProfileSnapshotRepository:
    """Return the SQLite-backed profile snapshot repository."""

    return SqliteProfileSnapshotRepository(get_database_session_factory())


@lru_cache(maxsize=1)
def get_recruiter_repository() -> SqliteRecruiterInteractionRepository:
    """Return the SQLite-backed recruiter interaction repository."""

    return SqliteRecruiterInteractionRepository(get_database_session_factory())


@lru_cache(maxsize=1)
def get_artifact_repository() -> SqliteArtifactSnapshotRepository:
    """Return the SQLite-backed artifact repository."""

    return SqliteArtifactSnapshotRepository(get_database_session_factory())


@lru_cache(maxsize=1)
def get_execution_event_repository() -> SqliteExecutionEventRepository:
    """Return the SQLite-backed execution event repository."""

    return SqliteExecutionEventRepository(get_database_session_factory())


@lru_cache(maxsize=1)
def get_submission_history_repository() -> SqliteSubmissionHistoryRepository:
    """Return the SQLite-backed history read model."""

    return SqliteSubmissionHistoryRepository(get_database_session_factory())


@lru_cache(maxsize=1)
def get_linkedin_jobs_client() -> PlaywrightLinkedInJobsClient:
    """Return the Playwright LinkedIn search client."""

    return PlaywrightLinkedInJobsClient(get_runtime_settings())


@lru_cache(maxsize=1)
def get_linkedin_easy_apply_executor() -> PlaywrightLinkedInEasyApplyExecutor:
    """Return the Playwright Easy Apply executor."""

    return PlaywrightLinkedInEasyApplyExecutor(
        get_runtime_settings(),
        execution_event_repository=get_execution_event_repository(),
    )


@lru_cache(maxsize=1)
def get_job_fetcher() -> LinkedInJobFetcher:
    """Return the job fetcher used by orchestrated executions."""

    return LinkedInJobFetcher(
        client=get_linkedin_jobs_client(),
        runtime_settings=get_runtime_settings(),
        job_repository=get_job_posting_repository(),
    )


@lru_cache(maxsize=1)
def get_job_scorer() -> RuleBasedJobScorer:
    """Return the deterministic job scorer used by executions."""

    return RuleBasedJobScorer()


@lru_cache(maxsize=1)
def get_job_submitter() -> LinkedInEasyApplySubmitter:
    """Return the LinkedIn Easy Apply submitter used by executions."""

    return LinkedInEasyApplySubmitter(
        executor=get_linkedin_easy_apply_executor(),
        submission_repository=get_submission_repository(),
        answer_repository=get_answer_repository(),
        profile_snapshot_repository=get_profile_snapshot_repository(),
        recruiter_repository=get_recruiter_repository(),
        artifact_repository=get_artifact_repository(),
        execution_event_repository=get_execution_event_repository(),
    )


@lru_cache(maxsize=1)
def get_successful_submission_store() -> InMemorySuccessfulSubmissionStore:
    """Return the in-memory successful submission store singleton."""

    return InMemorySuccessfulSubmissionStore()


@lru_cache(maxsize=1)
def get_agent_orchestrator() -> AgentExecutionOrchestrator:
    """Return the execution orchestrator singleton."""

    settings = get_runtime_settings()
    return AgentExecutionOrchestrator(
        panel_store=get_panel_settings_store(),
        execution_store=get_execution_store(),
        successful_submission_store=get_successful_submission_store(),
        submission_repository=get_submission_repository(),
        job_fetcher=get_job_fetcher(),
        job_scorer=get_job_scorer(),
        job_submitter=get_job_submitter(),
        output_dir=settings.output_dir,
        max_selected_jobs_per_run=settings.resolved_agent_max_selected_jobs_per_run,
    )


@lru_cache(maxsize=1)
def get_agent_scheduler() -> AgentScheduler:
    """Return the scheduler singleton."""

    settings = get_runtime_settings()
    return AgentScheduler(
        panel_store=get_panel_settings_store(),
        orchestrator=get_agent_orchestrator(),
        poll_interval_seconds=settings.scheduler_poll_interval_seconds,
    )
