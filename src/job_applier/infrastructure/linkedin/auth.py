"""LinkedIn authentication and storage-state reuse."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page
from pydantic import SecretStr

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
    ) -> None:
        self._credentials = credentials
        self._storage_state_path = storage_state_path
        self._login_timeout_seconds = login_timeout_seconds

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
        await self._login(fresh_context)
        await fresh_context.storage_state(path=str(self._storage_state_path))
        logger.info("linkedin_session_saved", extra={"path": str(self._storage_state_path)})
        return fresh_context

    def clear_saved_state(self) -> None:
        """Delete the saved storage-state file after an expired session."""

        if self._storage_state_path.exists():
            self._storage_state_path.unlink()

    async def page_requires_login(self, page: Page) -> bool:
        """Return whether the current page still needs the login flow."""

        current_url = page.url.lower()
        if "linkedin.com/login" in current_url or "/checkpoint/" in current_url:
            return True

        for locator in (
            page.get_by_label(re.compile(r"email|phone", re.I)),
            page.get_by_label(re.compile(r"password", re.I)),
            page.get_by_role("button", name=re.compile(r"sign in", re.I)),
        ):
            if await locator.count():
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

    async def _login(self, context: BrowserContext) -> None:
        """Run the LinkedIn login flow and allow manual intervention when needed."""

        page = await context.new_page()
        try:
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            await page.get_by_label(re.compile(r"email|phone", re.I)).fill(self._credentials.email)
            await page.get_by_label(re.compile(r"password", re.I)).fill(
                self._credentials.password.get_secret_value(),
            )
            await page.get_by_role("button", name=re.compile(r"sign in", re.I)).click()
            await self._wait_until_authenticated(page)
        finally:
            await page.close()

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
