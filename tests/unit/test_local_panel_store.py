from pathlib import Path

from pydantic import SecretStr

from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore
from job_applier.settings import RuntimeSettings


def test_local_panel_store_bootstraps_profile_and_copies_cv(tmp_path: Path) -> None:
    source_cv = tmp_path / "Thiago Martins - CV 2026.pdf"
    source_cv.write_bytes(b"fake-cv-content")

    runtime_settings = RuntimeSettings(
        data_dir=tmp_path / "runtime",
        linkedin_email="thiago@example.com",
        openai_api_key=SecretStr("sk-test-12345"),
        bootstrap_profile_cv_path=source_cv,
    )
    store = LocalPanelSettingsStore(
        root_dir=tmp_path / "panel",
        runtime_settings=runtime_settings,
    )

    document = store.load()
    copied_cv_path = Path(document.profile.cv_path or "")

    assert document.profile.name == "Thiago Martins"
    assert document.profile.email == "thiago@example.com"
    assert document.profile.city == "Sao Paulo"
    assert document.profile.cv_filename == "Thiago Martins - CV 2026.pdf"
    assert copied_cv_path.exists()
    assert copied_cv_path.read_bytes() == b"fake-cv-content"
    assert copied_cv_path.parent == tmp_path / "panel" / "cv"
    assert document.preferences.keywords == ("python", "automation")
    assert document.ai.api_key is not None
    assert document.ai.api_key.get_secret_value() == "sk-test-12345"


def test_local_panel_store_backfills_existing_empty_state_with_bootstrap(tmp_path: Path) -> None:
    source_cv = tmp_path / "Thiago Martins - Resume 2026.pdf"
    source_cv.write_bytes(b"resume")

    runtime_settings = RuntimeSettings(
        data_dir=tmp_path / "runtime",
        linkedin_email="thiago@example.com",
        openai_api_key=SecretStr("sk-test-12345"),
        bootstrap_profile_cv_path=source_cv,
    )
    store = LocalPanelSettingsStore(
        root_dir=tmp_path / "panel",
        runtime_settings=runtime_settings,
    )

    store._state_path.parent.mkdir(parents=True, exist_ok=True)
    store._state_path.write_text(
        """
        {
          "profile": {
            "name": "",
            "email": null,
            "phone": "",
            "city": "",
            "linkedin_url": null,
            "github_url": null,
            "portfolio_url": null,
            "years_experience_by_stack": {},
            "work_authorized": false,
            "needs_sponsorship": false,
            "salary_expectation": null,
            "availability": "",
            "default_responses": {},
            "cv_path": null,
            "cv_filename": null
          },
          "preferences": {
            "keywords": [],
            "location": "",
            "posted_within_hours": 24,
            "workplace_types": [],
            "seniority": [],
            "easy_apply_only": true,
            "minimum_score_threshold": 0.55,
            "positive_keywords": [],
            "negative_keywords": [],
            "auto_connect_with_recruiter": false
          },
          "ai": {
            "api_key": null,
            "model": "o3-mini"
          },
          "schedule": {
            "frequency": "daily",
            "run_at": "23:00",
            "timezone": ""
          }
        }
        """,
        encoding="utf-8",
    )

    document = store.load()

    assert document.profile.email == "thiago@example.com"
    assert document.profile.cv_filename == "Thiago Martins - Resume 2026.pdf"
    assert document.preferences.location == "Remote"
    assert document.ai.api_key is not None
    assert document.ai.api_key.get_secret_value() == "sk-test-12345"
    assert document.schedule.timezone == "America/Sao_Paulo"
