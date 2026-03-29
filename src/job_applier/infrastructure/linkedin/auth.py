"""LinkedIn authentication and storage-state reuse."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Locator, Page
from pydantic import SecretStr

logger = logging.getLogger(__name__)

EMAIL_INPUT_SELECTOR = ",".join(
    (
        "input[name='session_key']",
        "input#username",
        "input[type='email']",
        "input[autocomplete*='username']",
    ),
)
PASSWORD_INPUT_SELECTOR = ",".join(
    (
        "input[name='session_password']",
        "input#password",
        "input[type='password']",
        "input[autocomplete='current-password']",
    ),
)
SUBMIT_BUTTON_SELECTOR = ",".join(
    (
        "button[type='submit']",
        "button[data-litms-control-urn*='login-submit']",
    ),
)


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

        if await self._find_email_input(page).count():
            return True
        if await self._find_password_input(page).count():
            return True
        if await self._find_submit_button(page).count():
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
            await self._find_email_input(page).fill(self._credentials.email)
            await self._find_password_input(page).fill(
                self._credentials.password.get_secret_value(),
            )
            await self._find_submit_button(page).click()
            await self._wait_until_authenticated(page)
        finally:
            await page.close()

    def _find_email_input(self, page: Page) -> Locator:
        """Return the login email/phone input using deterministic selectors."""

        return page.locator(EMAIL_INPUT_SELECTOR).first

    def _find_password_input(self, page: Page) -> Locator:
        """Return the login password input using deterministic selectors."""

        return page.locator(PASSWORD_INPUT_SELECTOR).first

    def _find_submit_button(self, page: Page) -> Locator:
        """Return the login submit button using deterministic selectors."""

        return page.locator(SUBMIT_BUTTON_SELECTOR).first

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
