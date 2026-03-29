"""LinkedIn authentication and storage-state reuse."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page
from pydantic import SecretStr

from job_applier.infrastructure.linkedin.browser_agent import OpenAIResponsesBrowserAgent
from job_applier.infrastructure.linkedin.playwright_mcp import (
    OpenAIResponsesPlaywrightMcpAgent,
    PlaywrightMcpClient,
    PlaywrightMcpError,
    PlaywrightMcpHttpClient,
    PlaywrightMcpSessionNotFoundError,
    PlaywrightMcpStdioClient,
    is_local_playwright_mcp_url,
)
from job_applier.observability import append_output_jsonl

logger = logging.getLogger(__name__)


class LinkedInAuthError(RuntimeError):
    """Raised when LinkedIn authentication cannot be completed."""


@dataclass(frozen=True, slots=True)
class LinkedInCredentials:
    """Credentials used to authenticate into LinkedIn."""

    email: str
    password: SecretStr


class LinkedInSessionManager:
    """Create authenticated browser contexts and reuse saved storage state."""

    def __init__(
        self,
        *,
        credentials: LinkedInCredentials,
        storage_state_path: Path,
        login_timeout_seconds: int = 120,
        ai_api_key: SecretStr | None = None,
        ai_model: str = "o3-mini",
        playwright_mcp_url: str | None = None,
        playwright_mcp_prefer_stdio_for_local: bool = True,
        playwright_mcp_stdio_command: tuple[str, ...] | None = None,
    ) -> None:
        self._credentials = credentials
        self._storage_state_path = storage_state_path
        self._login_timeout_seconds = login_timeout_seconds
        self._ai_api_key = ai_api_key
        self._ai_model = ai_model
        self._playwright_mcp_url = playwright_mcp_url
        self._playwright_mcp_prefer_stdio_for_local = playwright_mcp_prefer_stdio_for_local
        self._playwright_mcp_stdio_command = playwright_mcp_stdio_command

    async def create_authenticated_context(self, browser: Browser) -> BrowserContext:
        """Return an authenticated context, reusing saved session state when valid."""

        if self._storage_state_path.exists():
            reused_context = await browser.new_context(storage_state=str(self._storage_state_path))
            if await self._is_authenticated(reused_context):
                logger.info(
                    "linkedin_session_reused",
                    extra={"path": str(self._storage_state_path)},
                )
                return reused_context
            await reused_context.close()
            self.clear_saved_state()

        fresh_context = await browser.new_context()
        logged_in_in_context = await self._login(fresh_context)
        if logged_in_in_context:
            await fresh_context.storage_state(path=str(self._storage_state_path))
            logger.info("linkedin_session_saved", extra={"path": str(self._storage_state_path)})
            return fresh_context

        await fresh_context.close()
        replayed_context = await browser.new_context(storage_state=str(self._storage_state_path))
        if not await self._is_authenticated(replayed_context):
            await replayed_context.close()
            msg = "Playwright MCP login finished, but the exported LinkedIn session is not valid."
            raise LinkedInAuthError(msg)
        logger.info("linkedin_session_saved", extra={"path": str(self._storage_state_path)})
        return replayed_context

    def clear_saved_state(self) -> None:
        """Delete the saved storage-state file after an expired session."""

        if self._storage_state_path.exists():
            self._storage_state_path.unlink()

    async def page_requires_login(self, page: Page) -> bool:
        """Return whether the current page still needs the login flow."""

        current_url = page.url.lower()
        if any(
            token in current_url
            for token in (
                "linkedin.com/login",
                "/checkpoint/",
                "/challenge/",
            )
        ):
            return True

        if await page.locator("input[type='password']").count():
            return True
        try:
            body_text = await page.locator("body").inner_text(timeout=2_000)
        except Exception:  # noqa: BLE001
            body_text = ""
        normalized_text = re.sub(r"\s+", " ", body_text).strip().lower()
        if "sign in" in normalized_text and "linkedin" in normalized_text:
            return True
        return False

    async def _is_authenticated(self, context: BrowserContext) -> bool:
        """Check whether a restored context still has a valid LinkedIn session."""

        page = await context.new_page()
        try:
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            return not await self.page_requires_login(page)
        finally:
            await page.close()

    async def _login(self, context: BrowserContext) -> bool:
        """Run the LinkedIn login flow and report whether the direct context is authenticated."""

        if self._playwright_mcp_url:
            await self._login_via_mcp()
            return False

        page = await context.new_page()
        try:
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            ai_api_key = self._resolve_ai_api_key()
            if ai_api_key is None:
                msg = (
                    "OpenAI API key is required for the LinkedIn browser agent login flow. "
                    "Configure it in the panel or set JOB_APPLIER_OPENAI_API_KEY."
                )
                raise LinkedInAuthError(msg)
            browser_agent = OpenAIResponsesBrowserAgent(
                api_key=ai_api_key,
                model=self._ai_model,
            )
            await browser_agent.complete_linkedin_login(
                page=page,
                credentials={
                    "linkedin_email": self._credentials.email,
                    "linkedin_password": self._credentials.password.get_secret_value(),
                },
                page_requires_login=self.page_requires_login,
                timeout_seconds=self._login_timeout_seconds,
            )
            await self._wait_until_authenticated(page)
            return True
        finally:
            await page.close()

    async def _login_via_mcp(self) -> None:
        """Authenticate through the external Playwright MCP server and export storage state."""

        ai_api_key = self._resolve_ai_api_key()
        if ai_api_key is None:
            msg = (
                "OpenAI API key is required for the LinkedIn browser agent login flow. "
                "Configure it in the panel or set JOB_APPLIER_OPENAI_API_KEY."
            )
            raise LinkedInAuthError(msg)
        if self._playwright_mcp_url is None:
            msg = "Playwright MCP URL is required for MCP-backed LinkedIn login."
            raise LinkedInAuthError(msg)

        agent = OpenAIResponsesPlaywrightMcpAgent(
            api_key=ai_api_key,
            model=self._ai_model,
        )

        if self._should_use_playwright_mcp_stdio():
            await self._login_via_mcp_client(
                self._create_playwright_mcp_client(force_stdio=True),
                agent=agent,
                mode="stdio",
            )
            return

        last_error: PlaywrightMcpError | None = None
        for attempt in range(3):
            client = self._create_playwright_mcp_client(force_stdio=False)
            append_output_jsonl(
                "mcp/login-attempts.jsonl",
                {
                    "attempt": attempt + 1,
                    "event": "attempt_started",
                    "base_url": self._playwright_mcp_url,
                    "model": self._ai_model,
                    "mode": "http",
                },
            )
            try:
                await self._run_playwright_mcp_login_attempt(client=client, agent=agent)
                append_output_jsonl(
                    "mcp/login-attempts.jsonl",
                    {
                        "attempt": attempt + 1,
                        "event": "attempt_completed",
                        "storage_state_path": str(self._storage_state_path),
                        "mode": "http",
                    },
                )
                return
            except PlaywrightMcpSessionNotFoundError as exc:
                last_error = exc
                logger.warning(
                    "playwright_mcp_session_restarted",
                    extra={"attempt": attempt + 1},
                )
                append_output_jsonl(
                    "mcp/login-attempts.jsonl",
                    {
                        "attempt": attempt + 1,
                        "event": "session_not_found",
                        "message": str(exc),
                        "mode": "http",
                    },
                )
                if attempt == 2:
                    break
            except PlaywrightMcpError as exc:
                append_output_jsonl(
                    "mcp/login-attempts.jsonl",
                    {
                        "attempt": attempt + 1,
                        "event": "attempt_failed",
                        "message": str(exc),
                        "mode": "http",
                    },
                )
                raise LinkedInAuthError(str(exc)) from exc
            finally:
                await client.close_browser()
                await client.shutdown()

        if (
            last_error is not None
            and self._playwright_mcp_url is not None
            and is_local_playwright_mcp_url(self._playwright_mcp_url)
            and self._playwright_mcp_stdio_command
        ):
            append_output_jsonl(
                "mcp/login-attempts.jsonl",
                {
                    "event": "fallback_to_stdio",
                    "message": str(last_error),
                    "command": list(self._playwright_mcp_stdio_command),
                },
            )
            await self._login_via_mcp_client(
                self._create_playwright_mcp_client(force_stdio=True),
                agent=agent,
                mode="stdio",
            )
            return

        if last_error is not None:
            raise LinkedInAuthError(str(last_error)) from last_error
        raise LinkedInAuthError("Playwright MCP login failed unexpectedly.")

    async def _login_via_mcp_client(
        self,
        client: PlaywrightMcpClient,
        *,
        agent: OpenAIResponsesPlaywrightMcpAgent,
        mode: str,
    ) -> None:
        append_output_jsonl(
            "mcp/login-attempts.jsonl",
            {
                "attempt": 1,
                "event": "attempt_started",
                "base_url": self._playwright_mcp_url,
                "model": self._ai_model,
                "mode": mode,
            },
        )
        try:
            await self._run_playwright_mcp_login_attempt(client=client, agent=agent)
            append_output_jsonl(
                "mcp/login-attempts.jsonl",
                {
                    "attempt": 1,
                    "event": "attempt_completed",
                    "storage_state_path": str(self._storage_state_path),
                    "mode": mode,
                },
            )
            return
        finally:
            await client.close_browser()
            await client.shutdown()

    async def _run_playwright_mcp_login_attempt(
        self,
        *,
        client: PlaywrightMcpClient,
        agent: OpenAIResponsesPlaywrightMcpAgent,
    ) -> None:
        await client.navigate("https://www.linkedin.com/login")
        await agent.complete_linkedin_login(
            client=client,
            credentials={
                "linkedin_email": self._credentials.email,
                "linkedin_password": self._credentials.password.get_secret_value(),
            },
            timeout_seconds=self._login_timeout_seconds,
        )
        self._storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await client.save_storage_state(self._storage_state_path)

    def _create_playwright_mcp_client(self, *, force_stdio: bool) -> PlaywrightMcpClient:
        if force_stdio:
            command = self._playwright_mcp_stdio_command
            if not command:
                msg = "Playwright MCP stdio command is not configured."
                raise LinkedInAuthError(msg)
            return PlaywrightMcpStdioClient(
                command=command,
                timeout_seconds=self._login_timeout_seconds,
            )
        assert self._playwright_mcp_url is not None
        return PlaywrightMcpHttpClient(
            base_url=self._playwright_mcp_url,
            timeout_seconds=self._login_timeout_seconds,
        )

    def _should_use_playwright_mcp_stdio(self) -> bool:
        if not self._playwright_mcp_prefer_stdio_for_local:
            return False
        if self._playwright_mcp_url is None or not self._playwright_mcp_stdio_command:
            return False
        return is_local_playwright_mcp_url(self._playwright_mcp_url)

    async def _wait_until_authenticated(self, page: Page) -> None:
        """Wait for the login flow to complete, including manual captcha handling."""

        attempts = self._login_timeout_seconds
        for _ in range(attempts):
            if not await self.page_requires_login(page):
                return
            await asyncio.sleep(1)

        msg = (
            "LinkedIn login did not finish in time. "
            "If headful mode is enabled, complete any captcha and try again."
        )
        raise LinkedInAuthError(msg)

    def _resolve_ai_api_key(self) -> SecretStr | None:
        """Return the API key used by the browser agent login planner."""

        if self._ai_api_key is not None:
            return self._ai_api_key
        raw_value = os.getenv("JOB_APPLIER_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        if raw_value:
            return SecretStr(raw_value)
        return None
