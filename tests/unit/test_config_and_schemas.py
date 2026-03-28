import json
from pathlib import Path

from job_applier.application.config import UserAgentSettings
from job_applier.application.schemas import JobPostingRead, UserAgentConfigRead
from job_applier.application.snapshotting import build_profile_snapshot
from job_applier.domain import JobPosting, Platform


def test_settings_load_from_env_file_and_snapshot_excludes_api_key(tmp_path: Path) -> None:
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
                "PROFILE__GITHUB_URL=https://github.com/0xthiagomartins",
                "PROFILE__PORTFOLIO_URL=https://thiago.example.com",
                'PROFILE__YEARS_EXPERIENCE_BY_STACK={"python":8,"fastapi":4}',
                "PROFILE__WORK_AUTHORIZED=true",
                "PROFILE__NEEDS_SPONSORSHIP=false",
                "PROFILE__SALARY_EXPECTATION=25000",
                "PROFILE__AVAILABILITY=30 days",
                'PROFILE__DEFAULT_RESPONSES={"work_authorization":"Yes"}',
                'PROFILE__POSITIVE_FILTERS=["python","automation"]',
                'PROFILE__BLACKLIST=["internship"]',
                'SEARCH__KEYWORDS=["python","easy apply"]',
                "SEARCH__LOCATION=Sao Paulo",
                "SEARCH__POSTED_WITHIN_HOURS=24",
                'SEARCH__WORKPLACE_TYPES=["remote","hybrid"]',
                'SEARCH__SENIORITY=["senior"]',
                "SEARCH__EASY_APPLY_ONLY=true",
                "AGENT__SCHEDULE__FREQUENCY=daily",
                "AGENT__SCHEDULE__RUN_AT=23:00",
                "AGENT__SCHEDULE__TIMEZONE=America/Sao_Paulo",
                "AI__API_KEY=sk-test",
                "AI__MODEL=o3-mini",
                "RULESET__VERSION=ruleset-v1",
            ],
        ),
        encoding="utf-8",
    )

    settings = UserAgentSettings.from_env_file(env_file)
    snapshot = build_profile_snapshot(settings)
    payload = json.loads(snapshot.data_json)

    assert settings.profile.email == "thiago@example.com"
    assert settings.agent.auto_connect_with_recruiter is False
    assert settings.agent.schedule.run_at == "23:00"
    assert payload["ai"] == {"model": "o3-mini"}
    assert "api_key" not in payload["ai"]

    safe_schema = UserAgentConfigRead.from_settings_payload(payload)
    assert safe_schema.ruleset.version == "ruleset-v1"


def test_job_posting_schema_reads_from_domain_entity() -> None:
    posting = JobPosting(
        platform=Platform.LINKEDIN,
        url="https://www.linkedin.com/jobs/view/999",
        title="Automation Engineer",
        company_name="Acme",
        description_raw="Automate repetitive job application workflows.",
    )

    schema = JobPostingRead.model_validate(posting)

    assert schema.platform is Platform.LINKEDIN
    assert schema.company_name == "Acme"
