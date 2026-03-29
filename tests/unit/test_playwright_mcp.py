from pathlib import Path

import pytest
from pydantic import SecretStr

from job_applier.infrastructure.linkedin.auth import LinkedInCredentials, LinkedInSessionManager
from job_applier.infrastructure.linkedin.playwright_mcp import (
    PlaywrightMcpHttpClient,
    PlaywrightMcpSessionNotFoundError,
    PlaywrightMcpStdioClient,
    extract_mcp_text_content,
    is_local_playwright_mcp_url,
    normalize_playwright_mcp_url,
    parse_mcp_response_body,
    parse_playwright_mcp_action,
)


def test_normalize_playwright_mcp_url_appends_mcp_path() -> None:
    assert normalize_playwright_mcp_url("http://localhost:8931") == "http://localhost:8931/mcp"
    assert normalize_playwright_mcp_url("http://127.0.0.1:8931") == "http://localhost:8931/mcp"
    assert (
        normalize_playwright_mcp_url("http://localhost:8931/custom")
        == "http://localhost:8931/custom/mcp"
    )
    assert normalize_playwright_mcp_url("http://localhost:8931/mcp") == "http://localhost:8931/mcp"


def test_parse_mcp_response_body_supports_json_and_sse() -> None:
    json_body = '{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"ok"}]}}'
    sse_body = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"ok"}]}}\n\n'
    )

    assert parse_mcp_response_body(json_body)["jsonrpc"] == "2.0"
    assert parse_mcp_response_body(sse_body)["jsonrpc"] == "2.0"


def test_parse_playwright_mcp_action_accepts_type_with_secret_value_source() -> None:
    action = parse_playwright_mcp_action(
        {
            "action_type": "type",
            "ref": "e17",
            "element": "Email field",
            "value_source": "linkedin_email",
            "value": None,
            "wait_seconds": 0,
            "reasoning": "The login form needs the email first.",
        }
    )

    assert action.action_type == "type"
    assert action.ref == "e17"
    assert action.value_source == "linkedin_email"


def test_is_local_playwright_mcp_url_detects_local_hosts() -> None:
    assert is_local_playwright_mcp_url("http://localhost:8931/mcp") is True
    assert is_local_playwright_mcp_url("http://127.0.0.1:8931") is True
    assert is_local_playwright_mcp_url("https://example.com/mcp") is False


def test_extract_mcp_text_content_joins_text_entries() -> None:
    result = {
        "content": [
            {"type": "text", "text": "Title"},
            {"type": "text", "text": "Body"},
        ]
    }

    assert extract_mcp_text_content(result) == "Title Body"


def test_linkedin_session_manager_can_bootstrap_login_through_playwright_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeClient:
        def __init__(self, *, base_url: str, timeout_seconds: int) -> None:
            assert base_url == "http://localhost:8931/mcp"
            assert timeout_seconds == 90

        async def navigate(self, url: str) -> None:
            events.append(f"navigate:{url}")

        async def save_storage_state(self, path: Path) -> None:
            events.append(f"save:{path.name}")
            path.write_text("{}", encoding="utf-8")

        async def close_browser(self) -> None:
            events.append("close_browser")

        async def shutdown(self) -> None:
            events.append("shutdown")

    class FakeAgent:
        def __init__(self, *, api_key: SecretStr, model: str) -> None:
            assert api_key.get_secret_value() == "sk-test"
            assert model == "o3-mini"

        async def complete_linkedin_login(
            self,
            *,
            client: PlaywrightMcpHttpClient,
            credentials: dict[str, str],
            timeout_seconds: int,
        ) -> None:
            events.append("agent_login")
            assert credentials["linkedin_email"] == "thiago@example.com"
            assert credentials["linkedin_password"] == "linkedin-secret"
            assert timeout_seconds == 90

    monkeypatch.setattr(
        "job_applier.infrastructure.linkedin.auth.PlaywrightMcpHttpClient",
        FakeClient,
    )
    monkeypatch.setattr(
        "job_applier.infrastructure.linkedin.auth.OpenAIResponsesPlaywrightMcpAgent",
        FakeAgent,
    )

    manager = LinkedInSessionManager(
        credentials=LinkedInCredentials(
            email="thiago@example.com",
            password=SecretStr("linkedin-secret"),
        ),
        storage_state_path=tmp_path / "storage-state.json",
        login_timeout_seconds=90,
        ai_api_key=SecretStr("sk-test"),
        ai_model="o3-mini",
        playwright_mcp_url="http://localhost:8931/mcp",
    )

    import asyncio

    asyncio.run(manager._login_via_mcp())

    assert events == [
        "navigate:https://www.linkedin.com/login",
        "agent_login",
        "save:storage-state.json",
        "close_browser",
        "shutdown",
    ]


def test_linkedin_session_manager_retries_when_playwright_mcp_loses_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    login_attempts = {"count": 0}

    class FakeClient:
        def __init__(self, *, base_url: str, timeout_seconds: int) -> None:
            assert base_url == "http://localhost:8931/mcp"
            assert timeout_seconds == 90
            login_attempts["count"] += 1
            self._attempt = login_attempts["count"]

        async def navigate(self, url: str) -> None:
            events.append(f"navigate:{self._attempt}:{url}")

        async def save_storage_state(self, path: Path) -> None:
            events.append(f"save:{self._attempt}:{path.name}")
            path.write_text("{}", encoding="utf-8")

        async def close_browser(self) -> None:
            events.append(f"close_browser:{self._attempt}")

        async def shutdown(self) -> None:
            events.append(f"shutdown:{self._attempt}")

    class FakeAgent:
        def __init__(self, *, api_key: SecretStr, model: str) -> None:
            assert api_key.get_secret_value() == "sk-test"
            assert model == "o3-mini"

        async def complete_linkedin_login(
            self,
            *,
            client: PlaywrightMcpHttpClient,
            credentials: dict[str, str],
            timeout_seconds: int,
        ) -> None:
            assert credentials["linkedin_email"] == "thiago@example.com"
            assert credentials["linkedin_password"] == "linkedin-secret"
            assert timeout_seconds == 90
            if login_attempts["count"] == 1:
                raise PlaywrightMcpSessionNotFoundError("Session not found")
            events.append("agent_login_success")

    monkeypatch.setattr(
        "job_applier.infrastructure.linkedin.auth.PlaywrightMcpHttpClient",
        FakeClient,
    )
    monkeypatch.setattr(
        "job_applier.infrastructure.linkedin.auth.OpenAIResponsesPlaywrightMcpAgent",
        FakeAgent,
    )

    manager = LinkedInSessionManager(
        credentials=LinkedInCredentials(
            email="thiago@example.com",
            password=SecretStr("linkedin-secret"),
        ),
        storage_state_path=tmp_path / "storage-state.json",
        login_timeout_seconds=90,
        ai_api_key=SecretStr("sk-test"),
        ai_model="o3-mini",
        playwright_mcp_url="http://localhost:8931/mcp",
    )

    import asyncio

    asyncio.run(manager._login_via_mcp())

    assert events == [
        "navigate:1:https://www.linkedin.com/login",
        "close_browser:1",
        "shutdown:1",
        "navigate:2:https://www.linkedin.com/login",
        "agent_login_success",
        "save:2:storage-state.json",
        "close_browser:2",
        "shutdown:2",
    ]


def test_linkedin_session_manager_prefers_stdio_for_local_playwright_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeStdioClient:
        def __init__(self, *, command: tuple[str, ...], timeout_seconds: int) -> None:
            assert command == ("npx", "-y", "@playwright/mcp@latest")
            assert timeout_seconds == 90

        async def navigate(self, url: str) -> None:
            events.append(f"navigate:{url}")

        async def save_storage_state(self, path: Path) -> None:
            events.append(f"save:{path.name}")
            path.write_text("{}", encoding="utf-8")

        async def close_browser(self) -> None:
            events.append("close_browser")

        async def shutdown(self) -> None:
            events.append("shutdown")

    class FakeAgent:
        def __init__(self, *, api_key: SecretStr, model: str) -> None:
            assert api_key.get_secret_value() == "sk-test"
            assert model == "o3-mini"

        async def complete_linkedin_login(
            self,
            *,
            client: PlaywrightMcpStdioClient,
            credentials: dict[str, str],
            timeout_seconds: int,
        ) -> None:
            del client, credentials, timeout_seconds
            events.append("agent_login")

    monkeypatch.setattr(
        "job_applier.infrastructure.linkedin.auth.PlaywrightMcpStdioClient",
        FakeStdioClient,
    )
    monkeypatch.setattr(
        "job_applier.infrastructure.linkedin.auth.OpenAIResponsesPlaywrightMcpAgent",
        FakeAgent,
    )

    manager = LinkedInSessionManager(
        credentials=LinkedInCredentials(
            email="thiago@example.com",
            password=SecretStr("linkedin-secret"),
        ),
        storage_state_path=tmp_path / "storage-state.json",
        login_timeout_seconds=90,
        ai_api_key=SecretStr("sk-test"),
        ai_model="o3-mini",
        playwright_mcp_url="http://localhost:8931/mcp",
        playwright_mcp_prefer_stdio_for_local=True,
        playwright_mcp_stdio_command=("npx", "-y", "@playwright/mcp@latest"),
    )

    import asyncio

    asyncio.run(manager._login_via_mcp())

    assert events == [
        "navigate:https://www.linkedin.com/login",
        "agent_login",
        "save:storage-state.json",
        "close_browser",
        "shutdown",
    ]
