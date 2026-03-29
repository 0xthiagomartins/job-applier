import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from job_applier.infrastructure import LocalPanelSettingsStore
from job_applier.interface.http.dependencies import get_panel_settings_store
from job_applier.main import app


@pytest.fixture
def panel_store_override(tmp_path: Path) -> Generator[LocalPanelSettingsStore]:
    store = LocalPanelSettingsStore(root_dir=tmp_path / "panel")

    async def override() -> LocalPanelSettingsStore:
        return store

    app.dependency_overrides[get_panel_settings_store] = override
    yield store
    app.dependency_overrides.clear()


def test_panel_state_endpoint_is_accessible(
    panel_store_override: LocalPanelSettingsStore,
) -> None:
    async def exercise() -> int:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/panel/state")
            assert response.json()["ai"]["model"] == "o3-mini"
            return response.status_code

    assert asyncio.run(exercise()) == 200


def test_profile_roundtrip_supports_upload(
    panel_store_override: LocalPanelSettingsStore,
) -> None:
    async def exercise() -> tuple[int, dict[str, Any]]:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/panel/profile",
                data={
                    "name": "Thiago Martins",
                    "email": "thiago@example.com",
                    "phone": "+5511999999999",
                    "city": "Sao Paulo",
                    "linkedin_url": "https://www.linkedin.com/in/thiago",
                    "github_url": "https://github.com/0xthiagomartins",
                    "portfolio_url": "https://thiago.example.com",
                    "years_experience_by_stack": "python=8\nfastapi=4",
                    "work_authorized": "true",
                    "availability": "30 days",
                    "default_responses": "work_authorization=Yes",
                },
                files={"cv_file": ("resume.pdf", b"fake-pdf-content", "application/pdf")},
            )
            read_response = await client.get("/api/panel/profile")
            return response.status_code, cast(dict[str, Any], read_response.json())

    status_code, payload = asyncio.run(exercise())

    assert status_code == 200
    assert payload["profile"]["name"] == "Thiago Martins"
    assert payload["profile"]["cv_filename"] == "resume.pdf"
    assert payload["cv_uploaded"] is True


def test_preferences_and_ai_roundtrip(
    panel_store_override: LocalPanelSettingsStore,
) -> None:
    async def exercise() -> tuple[int, int, int, dict[str, Any]]:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            preferences_response = await client.put(
                "/api/panel/preferences",
                data={
                    "keywords": "python, automation",
                    "location": "Remote",
                    "posted_within_hours": "24",
                    "workplace_types": ["remote", "hybrid"],
                    "seniority": ["senior"],
                    "easy_apply_only": "true",
                    "minimum_score_threshold": "0.7",
                    "positive_keywords": "fastapi, agentic",
                    "negative_keywords": "internship",
                    "auto_connect_with_recruiter": "true",
                },
            )
            ai_response = await client.put(
                "/api/panel/ai",
                data={"api_key": "sk-test-12345", "model": "o3-mini"},
            )
            schedule_response = await client.put(
                "/api/panel/schedule",
                data={"frequency": "daily", "run_at": "23:00", "timezone": "America/Sao_Paulo"},
            )
            state_response = await client.get("/api/panel/state")
            return (
                preferences_response.status_code,
                ai_response.status_code,
                schedule_response.status_code,
                cast(dict[str, Any], state_response.json()),
            )

    preferences_status, ai_status, schedule_status, payload = asyncio.run(exercise())

    assert preferences_status == 200
    assert ai_status == 200
    assert schedule_status == 200
    assert payload["preferences"]["auto_connect_with_recruiter"] is True
    assert payload["preferences"]["keywords"] == ["python", "automation"]
    assert payload["preferences"]["minimum_score_threshold"] == 0.7
    assert payload["schedule"]["run_at"] == "23:00"
    assert payload["schedule"]["timezone"] == "America/Sao_Paulo"
    assert payload["computed"]["next_execution_at"].endswith("+00:00")
    assert payload["ai"]["has_api_key"] is True
    assert "America/Sao_Paulo" in payload["options"]["timezones"]
