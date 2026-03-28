import json
from pathlib import Path
from uuid import uuid4

from job_applier.application.config import UserAgentSettings
from job_applier.application.snapshotting import create_successful_submission_record
from job_applier.domain import ApplicationSubmission
from job_applier.infrastructure import InMemorySuccessfulSubmissionStore


def build_settings(tmp_path: Path) -> UserAgentSettings:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CONFIG_VERSION=config-v1",
                "PROFILE__NAME=Thiago Martins",
                "PROFILE__EMAIL=thiago@example.com",
                "PROFILE__PHONE=+5511999999999",
                "PROFILE__CITY=Sao Paulo",
                "PROFILE__LINKEDIN_URL=https://www.linkedin.com/in/thiago",
                "PROFILE__WORK_AUTHORIZED=true",
                "PROFILE__NEEDS_SPONSORSHIP=false",
                "PROFILE__AVAILABILITY=Immediate",
                'SEARCH__KEYWORDS=["python"]',
                "SEARCH__LOCATION=Remote",
                "AGENT__SCHEDULE__CRON=0 23 * * *",
                "AI__API_KEY=sk-test",
                "AI__MODEL=o3-mini",
                "RULESET__VERSION=ruleset-v2",
            ],
        ),
        encoding="utf-8",
    )
    return UserAgentSettings.from_env_file(env_file)


def test_successful_submission_store_can_query_snapshot_and_ruleset(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    submission = ApplicationSubmission(job_posting_id=uuid4())

    record = create_successful_submission_record(submission, settings=settings)
    store = InMemorySuccessfulSubmissionStore()
    store.save(record)

    stored = store.get(record.submission.id)

    assert stored is not None
    assert stored.ruleset.version == "ruleset-v2"
    assert stored.submission.ruleset_version == "ruleset-v2"
    assert stored.submission.profile_snapshot_id == stored.snapshot.id
    assert json.loads(stored.snapshot.data_json)["config_version"] == "config-v1"
