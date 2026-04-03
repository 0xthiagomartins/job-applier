"""LinkedIn Jobs search automation and job capture."""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import SecretStr

from job_applier.application.agent_execution import JobFetcher
from job_applier.application.config import UserAgentSettings
from job_applier.application.repositories import JobPostingRepository
from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import Platform, SeniorityLevel, WorkplaceType
from job_applier.infrastructure.linkedin.auth import (
    LinkedInAuthError,
    LinkedInCredentials,
    LinkedInSessionManager,
)
from job_applier.infrastructure.linkedin.browser_agent import (
    BrowserAutomationError,
    BrowserTaskAssessment,
    OpenAIResponsesBrowserAgent,
)
from job_applier.observability import (
    append_artifact_reference,
    append_output_jsonl,
    append_timeline_event,
    update_progress_snapshot,
    update_summary_snapshot,
)
from job_applier.settings import RuntimeSettings

logger = logging.getLogger(__name__)

LINKEDIN_JOBS_URL = "https://www.linkedin.com/jobs/"
LINKEDIN_JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"
RESULTS_PER_PAGE = 25
RESULTS_PAGE_MAX_SCROLL_ROUNDS = 12
RESULTS_PAGE_STALE_SCROLL_ROUNDS = 2


class LinkedInSearchError(RuntimeError):
    """Raised when LinkedIn search automation cannot continue."""


@dataclass(frozen=True, slots=True)
class LinkedInSearchCriteria:
    """Search parameters loaded from the user configuration."""

    keywords: tuple[str, ...]
    keywords_text: str
    location: str
    posted_within_hours: int
    workplace_types: tuple[WorkplaceType, ...]
    seniority: tuple[SeniorityLevel, ...]
    easy_apply_only: bool
    max_pages: int
    debug_target_job_url: str | None = None
    ai_api_key: SecretStr | None = None
    ai_model: str = "o3-mini"

    def to_log_payload(self) -> dict[str, object]:
        """Return a structured payload for logs."""

        return {
            "keywords": list(self.keywords),
            "location": self.location,
            "posted_within_hours": self.posted_within_hours,
            "workplace_types": [item.value for item in self.workplace_types],
            "seniority": [item.value for item in self.seniority],
            "easy_apply_only": self.easy_apply_only,
            "max_pages": self.max_pages,
            "debug_target_job_url": self.debug_target_job_url,
        }


@dataclass(frozen=True, slots=True)
class LinkedInCollectedJob:
    """Normalized data captured from LinkedIn result pages."""

    external_job_id: str | None
    url: str
    title: str
    company_name: str
    location: str | None
    description_raw: str
    easy_apply: bool
    metadata_text: str = ""
    workplace_type: WorkplaceType | None = None
    seniority: SeniorityLevel | None = None


@dataclass(frozen=True, slots=True)
class LinkedInResultsPageCollection:
    """One hydrated LinkedIn results page after scroll-based collection."""

    listings: tuple[LinkedInCollectedJob, ...]
    rounds: int
    visible_listing_count: int
    stale_rounds: int


class LinkedInJobsClient(Protocol):
    """Boundary used by the job fetcher to capture recent jobs."""

    async def fetch_jobs(self, criteria: LinkedInSearchCriteria) -> list[LinkedInCollectedJob]:
        """Return structured jobs captured from LinkedIn."""


def build_search_criteria(
    settings: UserAgentSettings,
    runtime_settings: RuntimeSettings,
) -> LinkedInSearchCriteria:
    """Build LinkedIn search criteria from the user and runtime settings."""

    return LinkedInSearchCriteria(
        keywords=settings.search.keywords,
        keywords_text=" ".join(settings.search.keywords).strip(),
        location=settings.search.location,
        posted_within_hours=settings.search.posted_within_hours,
        workplace_types=settings.search.workplace_types,
        seniority=settings.search.seniority,
        easy_apply_only=settings.search.easy_apply_only,
        max_pages=runtime_settings.resolved_linkedin_max_search_pages,
        debug_target_job_url=runtime_settings.resolved_linkedin_debug_target_job_url,
        ai_api_key=settings.ai.api_key,
        ai_model=settings.ai.model,
    )


def build_paginated_search_url(url: str, *, page_index: int) -> str:
    """Return the search URL for a given results page."""

    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["start"] = str(page_index * RESULTS_PER_PAGE)
    updated_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=updated_query))


def build_search_results_url(criteria: LinkedInSearchCriteria) -> str:
    """Build the primary LinkedIn results URL used by the search flow."""

    params: dict[str, str] = {
        "keywords": criteria.keywords_text,
        "location": criteria.location,
    }
    if criteria.easy_apply_only:
        params["f_AL"] = "true"
    if criteria.posted_within_hours <= 24:
        params["f_TPR"] = "r86400"
    return f"{LINKEDIN_JOBS_SEARCH_URL}?{urlencode(params)}"


def infer_workplace_type(text: str) -> WorkplaceType | None:
    """Infer the workplace type from listing or detail text."""

    lowered = text.lower()
    if "hybrid" in lowered:
        return WorkplaceType.HYBRID
    if "remote" in lowered:
        return WorkplaceType.REMOTE
    if "on-site" in lowered or "onsite" in lowered or "on site" in lowered:
        return WorkplaceType.ONSITE
    return None


def infer_seniority(text: str) -> SeniorityLevel | None:
    """Infer the seniority level from listing or detail text."""

    lowered = text.lower()
    if "principal" in lowered:
        return SeniorityLevel.PRINCIPAL
    if "staff" in lowered:
        return SeniorityLevel.STAFF
    if "director" in lowered or "executive" in lowered:
        return SeniorityLevel.DIRECTOR
    if "manager" in lowered:
        return SeniorityLevel.MANAGER
    if "mid-senior" in lowered or re.search(r"\bsenior\b", lowered):
        return SeniorityLevel.SENIOR
    if "associate" in lowered or re.search(r"\bmid\b", lowered):
        return SeniorityLevel.MID
    if "junior" in lowered or "entry level" in lowered or "entry-level" in lowered:
        return SeniorityLevel.JUNIOR
    if "intern" in lowered:
        return SeniorityLevel.INTERN
    return None


class LinkedInJobParser:
    """Convert collected LinkedIn payloads into domain job postings."""

    def parse(self, payload: LinkedInCollectedJob) -> JobPosting:
        """Convert one captured LinkedIn job into the domain model."""

        normalized_url = payload.url.split("?", maxsplit=1)[0]
        external_job_id = payload.external_job_id or self._extract_job_id(normalized_url)
        metadata_text = " ".join(
            item for item in (payload.title, payload.company_name, payload.metadata_text) if item
        )
        description = (
            payload.description_raw.strip() or f"{payload.title} at {payload.company_name}"
        )

        return JobPosting(
            platform=Platform.LINKEDIN,
            url=normalized_url,
            external_job_id=external_job_id,
            title=payload.title.strip(),
            company_name=payload.company_name.strip(),
            location=payload.location.strip() if payload.location else None,
            workplace_type=payload.workplace_type or infer_workplace_type(metadata_text),
            seniority=payload.seniority or infer_seniority(metadata_text),
            easy_apply=payload.easy_apply,
            description_raw=description,
            captured_at=datetime.now(UTC),
        )

    def _extract_job_id(self, url: str) -> str | None:
        match = re.search(r"/jobs/view/(\d+)", url)
        return match.group(1) if match else None


class LinkedInJobFetcher(JobFetcher):
    """Fetch recent LinkedIn jobs and persist them via the job repository."""

    def __init__(
        self,
        *,
        client: LinkedInJobsClient,
        runtime_settings: RuntimeSettings,
        job_repository: JobPostingRepository,
        parser: LinkedInJobParser | None = None,
    ) -> None:
        self._client = client
        self._runtime_settings = runtime_settings
        self._job_repository = job_repository
        self._parser = parser or LinkedInJobParser()

    async def fetch(self, settings: UserAgentSettings) -> list[JobPosting]:
        criteria = build_search_criteria(settings, self._runtime_settings)
        logger.info("linkedin_search_started", extra=criteria.to_log_payload())

        collected_jobs = await self._client.fetch_jobs(criteria)
        persisted_jobs: list[JobPosting] = []
        seen_keys: set[str] = set()

        for collected_job in collected_jobs:
            try:
                posting = self._parser.parse(collected_job)
            except ValueError:
                logger.exception(
                    "linkedin_job_parse_failed",
                    extra={
                        "external_job_id": collected_job.external_job_id,
                        "url": collected_job.url,
                    },
                )
                continue
            dedupe_key = posting.external_job_id or posting.url
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            existing = None
            if posting.external_job_id:
                existing = self._job_repository.find_by_external_job_id(
                    platform=posting.platform.value,
                    external_job_id=posting.external_job_id,
                )
            if existing is not None:
                posting = replace(posting, id=existing.id)

            saved_posting = self._job_repository.save(posting)
            persisted_jobs.append(saved_posting)
            logger.info(
                "linkedin_job_captured",
                extra={
                    "job_posting_id": str(saved_posting.id),
                    "external_job_id": saved_posting.external_job_id,
                    "company_name": saved_posting.company_name,
                    "title": saved_posting.title,
                    "easy_apply": saved_posting.easy_apply,
                },
            )

        logger.info("linkedin_search_completed", extra={"jobs_seen": len(persisted_jobs)})
        return persisted_jobs


class PlaywrightLinkedInJobsClient:
    """Use Playwright to search LinkedIn Jobs and capture structured postings."""

    def __init__(self, runtime_settings: RuntimeSettings) -> None:
        self._runtime_settings = runtime_settings
        self._session_manager: LinkedInSessionManager | None = None

    async def fetch_jobs(self, criteria: LinkedInSearchCriteria) -> list[LinkedInCollectedJob]:
        async with async_playwright() as playwright:
            session_manager = self._create_session_manager(criteria)
            browser = await playwright.chromium.launch(
                headless=self._runtime_settings.playwright_headless,
            )
            try:
                for attempt in range(2):
                    context = await session_manager.create_authenticated_context(browser)
                    try:
                        return await self._fetch_jobs_once(context, criteria)
                    except LinkedInAuthError:
                        session_manager.clear_saved_state()
                        if attempt == 1:
                            raise
                    finally:
                        await context.close()
            finally:
                await browser.close()
        msg = "LinkedIn search exhausted all authentication retries."
        raise LinkedInSearchError(msg)

    def _credentials_from_settings(self, runtime_settings: RuntimeSettings) -> LinkedInCredentials:
        if runtime_settings.linkedin_email is None or runtime_settings.linkedin_password is None:
            msg = (
                "LinkedIn credentials are required. "
                "Set JOB_APPLIER_LINKEDIN_EMAIL and JOB_APPLIER_LINKEDIN_PASSWORD in your .env."
            )
            raise LinkedInAuthError(msg)
        return LinkedInCredentials(
            email=runtime_settings.linkedin_email,
            password=runtime_settings.linkedin_password,
        )

    def _create_session_manager(
        self,
        criteria: LinkedInSearchCriteria | None = None,
    ) -> LinkedInSessionManager:
        self._session_manager = LinkedInSessionManager(
            credentials=self._credentials_from_settings(self._runtime_settings),
            storage_state_path=self._runtime_settings.resolved_linkedin_storage_state_path,
            login_timeout_seconds=self._runtime_settings.linkedin_login_timeout_seconds,
            ai_api_key=(
                criteria.ai_api_key
                if criteria is not None
                else self._runtime_settings.openai_api_key
            ),
            ai_model=criteria.ai_model if criteria is not None else "o3-mini",
            playwright_mcp_url=(
                self._runtime_settings.resolved_playwright_mcp_url
                if self._runtime_settings.playwright_mcp_url is not None
                else None
            ),
            playwright_mcp_prefer_stdio_for_local=(
                self._runtime_settings.playwright_mcp_prefer_stdio_for_local
            ),
            playwright_mcp_stdio_command=(
                self._runtime_settings.resolved_playwright_mcp_stdio_command
            ),
            openai_responses_max_retries=(
                self._runtime_settings.resolved_openai_responses_max_retries
            ),
            openai_responses_retry_max_delay_seconds=(
                self._runtime_settings.openai_responses_retry_max_delay_seconds
            ),
        )
        return self._session_manager

    def _get_session_manager(self) -> LinkedInSessionManager:
        if self._session_manager is None:
            return self._create_session_manager()
        return self._session_manager

    async def _fetch_jobs_once(
        self,
        context: BrowserContext,
        criteria: LinkedInSearchCriteria,
    ) -> list[LinkedInCollectedJob]:
        run_dir = self._build_run_dir()
        if criteria.debug_target_job_url:
            return [await self._fetch_debug_target_job(context, criteria, run_dir=run_dir)]

        page = await context.new_page()
        page.set_default_timeout(self._runtime_settings.linkedin_default_timeout_ms)

        try:
            await self._open_search(page, criteria, run_dir=run_dir)
            search_url = page.url
            jobs: list[LinkedInCollectedJob] = []
            seen_job_urls: set[str] = set()

            for page_index in range(criteria.max_pages):
                if page_index > 0:
                    await self._pause_before_navigation(
                        page,
                        reason="search_results_pagination",
                    )
                    target_page_url = build_paginated_search_url(
                        search_url,
                        page_index=page_index,
                    )
                    await self._goto_paginated_results_page(
                        page,
                        target_page_url=target_page_url,
                        page_index=page_index + 1,
                    )
                await self._ensure_authenticated_page(page)
                await self._capture_screenshot(page, run_dir / f"results-page-{page_index + 1}.png")
                page_collection = await self._collect_listing_cards_from_results_page(
                    page,
                    page_index=page_index + 1,
                )
                listings = list(page_collection.listings)
                duplicate_listing_count = sum(
                    1 for listing in listings if listing.url in seen_job_urls
                )
                new_listing_count = len(listings) - duplicate_listing_count
                append_timeline_event(
                    "linkedin_search_results_page_loaded",
                    {
                        "page_index": page_index + 1,
                        "listing_count": len(listings),
                        "visible_listing_count": page_collection.visible_listing_count,
                        "collection_rounds": page_collection.rounds,
                        "duplicate_listing_count": duplicate_listing_count,
                        "new_listing_count": new_listing_count,
                        "url": page.url,
                    },
                )
                update_progress_snapshot(
                    {
                        "current_stage": "search_results_loaded",
                        "current_job": None,
                        "search_page_index": page_index + 1,
                        "search_listing_count": len(listings),
                        "search_visible_listing_count": page_collection.visible_listing_count,
                        "search_new_listing_count": new_listing_count,
                    },
                )
                if not listings:
                    break

                for listing in listings:
                    if listing.url in seen_job_urls:
                        continue
                    seen_job_urls.add(listing.url)
                    update_progress_snapshot(
                        {
                            "current_stage": "job_detail_loading",
                            "current_job": {
                                "external_job_id": listing.external_job_id,
                                "title": listing.title,
                                "company_name": listing.company_name,
                                "url": listing.url,
                            },
                        },
                    )
                    append_timeline_event(
                        "linkedin_job_detail_loading",
                        {
                            "external_job_id": listing.external_job_id,
                            "title": listing.title,
                            "company_name": listing.company_name,
                            "url": listing.url,
                        },
                    )
                    detailed_listing = await self._load_job_details(context, listing)
                    jobs.append(detailed_listing)
                    update_progress_snapshot(
                        {
                            "current_stage": "job_detail_loaded",
                            "current_job": {
                                "external_job_id": detailed_listing.external_job_id,
                                "title": detailed_listing.title,
                                "company_name": detailed_listing.company_name,
                                "url": detailed_listing.url,
                            },
                            "jobs_seen": len(jobs),
                        },
                    )
                    update_summary_snapshot({"jobs_seen": len(jobs)})
            else:
                append_output_jsonl(
                    "run.log",
                    {
                        "source": "linkedin_search",
                        "kind": "search_pagination_limit_reached",
                        "max_pages": criteria.max_pages,
                        "jobs_collected": len(jobs),
                        "unique_job_urls": len(seen_job_urls),
                    },
                )
                append_timeline_event(
                    "linkedin_search_pagination_limit_reached",
                    {
                        "max_pages": criteria.max_pages,
                        "jobs_collected": len(jobs),
                        "unique_job_urls": len(seen_job_urls),
                    },
                )

            return jobs
        finally:
            await page.close()

    async def _fetch_debug_target_job(
        self,
        context: BrowserContext,
        criteria: LinkedInSearchCriteria,
        *,
        run_dir: Path,
    ) -> LinkedInCollectedJob:
        target_url = criteria.debug_target_job_url
        if target_url is None:
            msg = "LinkedIn debug target job URL is required for direct job debugging."
            raise LinkedInSearchError(msg)

        append_output_jsonl(
            "run.log",
            {
                "source": "linkedin_search",
                "kind": "debug_target_job_started",
                "target_job_url": target_url,
            },
        )
        append_timeline_event(
            "linkedin_debug_target_job_started",
            {
                "target_job_url": target_url,
            },
        )
        update_progress_snapshot(
            {
                "current_stage": "job_detail_loading",
                "current_job": {
                    "external_job_id": self._extract_job_id_from_url(target_url),
                    "title": "LinkedIn debug target",
                    "company_name": "LinkedIn debug target",
                    "url": target_url,
                },
                "search_target_url": target_url,
                "search_page_index": 1,
            },
        )
        detailed_listing = await self._load_job_details(
            context,
            LinkedInCollectedJob(
                external_job_id=self._extract_job_id_from_url(target_url),
                url=target_url,
                title="LinkedIn debug target",
                company_name="LinkedIn debug target",
                location=None,
                description_raw="",
                easy_apply=True,
            ),
        )
        update_progress_snapshot(
            {
                "current_stage": "job_detail_loaded",
                "current_job": {
                    "external_job_id": detailed_listing.external_job_id,
                    "title": detailed_listing.title,
                    "company_name": detailed_listing.company_name,
                    "url": detailed_listing.url,
                },
                "jobs_seen": 1,
            },
        )
        update_summary_snapshot({"jobs_seen": 1})
        await self._capture_debug_target_artifact(context, target_url=target_url, run_dir=run_dir)
        append_timeline_event(
            "linkedin_debug_target_job_loaded",
            {
                "external_job_id": detailed_listing.external_job_id,
                "title": detailed_listing.title,
                "company_name": detailed_listing.company_name,
                "url": detailed_listing.url,
                "easy_apply": detailed_listing.easy_apply,
            },
        )
        return detailed_listing

    async def _open_search(
        self,
        page: Page,
        criteria: LinkedInSearchCriteria,
        *,
        run_dir: Path,
    ) -> None:
        direct_results_url = build_search_results_url(criteria)
        update_progress_snapshot(
            {
                "current_stage": "search_results_entry",
                "current_job": None,
                "current_step": None,
                "search_target_url": direct_results_url,
            },
        )
        append_output_jsonl(
            "run.log",
            {
                "source": "linkedin_search",
                "kind": "search_entry_started",
                "direct_results_url": direct_results_url,
            },
        )
        await self._pause_before_navigation(page, reason="search_results_entry")
        await page.goto(direct_results_url, wait_until="domcontentloaded")
        await self._ensure_authenticated_page(page)
        if await self._wait_for_extractable_search_cards(page):
            append_output_jsonl(
                "run.log",
                {
                    "source": "linkedin_search",
                    "kind": "search_entry_ready_via_cards",
                    "url": page.url,
                },
            )
            await self._capture_screenshot(page, run_dir / "jobs-search-entry.png")
            return
        try:
            await self._wait_for_search_surface(page, criteria=criteria)
            append_output_jsonl(
                "run.log",
                {
                    "source": "linkedin_search",
                    "kind": "search_entry_ready_via_assessment",
                    "url": page.url,
                },
            )
            await self._capture_screenshot(page, run_dir / "jobs-search-entry.png")
            return
        except LinkedInSearchError:
            append_output_jsonl(
                "run.log",
                {
                    "source": "linkedin_search",
                    "kind": "search_entry_fallback_to_browser_agent",
                    "url": page.url,
                },
            )
            pass

        await self._complete_search_with_browser_agent(page, criteria=criteria)
        await self._wait_for_search_surface(page, criteria=criteria)
        append_output_jsonl(
            "run.log",
            {
                "source": "linkedin_search",
                "kind": "search_results_ready_after_browser_agent",
                "url": page.url,
            },
        )
        await self._capture_screenshot(page, run_dir / "search-results-ready.png")
        return

    async def _goto_paginated_results_page(
        self,
        page: Page,
        *,
        target_page_url: str,
        page_index: int,
    ) -> None:
        timeout_ms = max(self._runtime_settings.linkedin_default_timeout_ms, 30_000)

        for attempt_index in range(2):
            try:
                await page.goto(
                    target_page_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                return
            except PlaywrightTimeoutError:
                append_output_jsonl(
                    "run.log",
                    {
                        "source": "linkedin_search",
                        "kind": "search_pagination_navigation_timeout",
                        "page_index": page_index,
                        "attempt_index": attempt_index,
                        "target_page_url": target_page_url,
                        "current_url": page.url,
                        "timeout_ms": timeout_ms,
                    },
                )
                if await self._results_page_navigation_succeeded(
                    page,
                    target_page_url=target_page_url,
                ):
                    return
                if attempt_index >= 1:
                    raise
                await page.wait_for_timeout(1_250)

    async def _results_page_navigation_succeeded(
        self,
        page: Page,
        *,
        target_page_url: str,
    ) -> bool:
        current_params = dict(parse_qsl(urlparse(page.url).query, keep_blank_values=True))
        target_params = dict(parse_qsl(urlparse(target_page_url).query, keep_blank_values=True))
        if current_params.get("start", "0") != target_params.get("start", "0"):
            return False
        return await self._wait_for_extractable_search_cards(page, attempts=2)

    async def _complete_search_with_browser_agent(
        self,
        page: Page,
        *,
        criteria: LinkedInSearchCriteria,
    ) -> None:
        ai_api_key = criteria.ai_api_key or self._runtime_settings.openai_api_key
        if ai_api_key is None:
            msg = (
                "LinkedIn search reached an unexpected page and needs the browser agent fallback. "
                "Configure the OpenAI API key in the panel or set JOB_APPLIER_OPENAI_API_KEY."
            )
            raise LinkedInSearchError(msg)

        browser_agent = OpenAIResponsesBrowserAgent(
            api_key=ai_api_key,
            model=criteria.ai_model,
            stall_threshold=self._runtime_settings.resolved_browser_agent_stall_threshold,
            min_action_delay_ms=self._runtime_settings.linkedin_min_action_delay_ms,
            max_action_delay_ms=self._runtime_settings.linkedin_max_action_delay_ms,
            openai_max_retries=self._runtime_settings.resolved_openai_responses_max_retries,
            openai_retry_max_delay_seconds=(
                self._runtime_settings.openai_responses_retry_max_delay_seconds
            ),
        )

        async def search_results_ready(candidate_page: Page) -> bool:
            return await self._search_results_ready(candidate_page, criteria=criteria)

        try:
            await browser_agent.complete_browser_task(
                page=page,
                available_values={
                    "search_keywords": criteria.keywords_text,
                    "search_location": criteria.location,
                },
                goal=(
                    "Reach the LinkedIn jobs search results page for the current user. "
                    "The results should reflect the requested keywords and location, "
                    "and the base filters must be last 24 hours and Easy Apply only."
                ),
                timeout_seconds=max(30, self._runtime_settings.linkedin_login_timeout_seconds),
                task_name="linkedin_job_search",
                is_complete=search_results_ready,
                extra_rules=(
                    (
                        "If the page is still loading, skeletons are visible, or the search box "
                        "has not rendered yet, prefer wait over guessing."
                    ),
                    (
                        "Use search_keywords for the job title or skills query field and "
                        "search_location for the location field when those inputs exist."
                    ),
                    (
                        "If the results page already shows job cards or a no-results state, "
                        "choose done."
                    ),
                    ("If a search submit action is visible after filling the query, click it."),
                    (
                        "If an Easy Apply or Past 24 hours filter is visible and inactive, "
                        "activate it before declaring done."
                    ),
                ),
            )
        except BrowserAutomationError as exc:
            raise LinkedInSearchError(str(exc)) from exc

    async def _fill_input(self, page: Page, *, patterns: tuple[str, ...], value: str) -> None:
        for pattern in patterns:
            for locator in (
                page.get_by_role("combobox", name=re.compile(pattern, re.I)),
                page.get_by_role("textbox", name=re.compile(pattern, re.I)),
                page.get_by_label(re.compile(pattern, re.I)),
            ):
                if await locator.count():
                    await locator.first.fill(value)
                    return
        msg = f"Could not find LinkedIn search input for patterns: {patterns!r}"
        raise LinkedInSearchError(msg)

    async def _apply_filters(self, page: Page, criteria: LinkedInSearchCriteria) -> None:
        logger.info("linkedin_filters_applied", extra=criteria.to_log_payload())

        if criteria.easy_apply_only:
            await self._toggle_filter(page, button_name=r"Easy Apply")

        if criteria.posted_within_hours <= 24:
            await self._select_filter_options(
                page,
                button_name=r"Date posted",
                option_names=("Past 24 hours",),
            )

        if criteria.workplace_types:
            await self._select_filter_options(
                page,
                button_name=r"On-site/remote",
                option_names=tuple(
                    _workplace_option_name(item) for item in criteria.workplace_types
                ),
            )

        if criteria.seniority:
            await self._select_filter_options(
                page,
                button_name=r"Experience level",
                option_names=tuple(_seniority_option_name(item) for item in criteria.seniority),
            )

    async def _toggle_filter(self, page: Page, *, button_name: str) -> None:
        locator = page.get_by_role("button", name=re.compile(button_name, re.I))
        if await locator.count():
            await locator.first.click()
            await page.wait_for_load_state("domcontentloaded")

    async def _select_filter_options(
        self,
        page: Page,
        *,
        button_name: str,
        option_names: tuple[str, ...],
    ) -> None:
        button = page.get_by_role("button", name=re.compile(button_name, re.I))
        if not await button.count():
            return
        await button.first.click()

        for option_name in option_names:
            option = page.get_by_label(re.compile(re.escape(option_name), re.I))
            if await option.count():
                await option.first.check()
                continue

            checkbox = page.get_by_role("checkbox", name=re.compile(re.escape(option_name), re.I))
            if await checkbox.count():
                await checkbox.first.check()

        for submit in (
            page.get_by_role("button", name=re.compile(r"Show results", re.I)),
            page.get_by_role("button", name=re.compile(r"Apply", re.I)),
        ):
            if await submit.count():
                await submit.first.click()
                await page.wait_for_load_state("domcontentloaded")
                return

    async def _wait_for_search_surface(
        self,
        page: Page,
        *,
        criteria: LinkedInSearchCriteria,
    ) -> BrowserTaskAssessment:
        current_url = getattr(page, "url", "")
        last_assessment: BrowserTaskAssessment | None = None
        for attempt in range(5):
            if await self._wait_for_extractable_search_cards(page, attempts=1):
                append_output_jsonl(
                    "run.log",
                    {
                        "source": "linkedin_search",
                        "kind": "search_surface_cards_visible",
                        "attempt": attempt + 1,
                        "url": current_url,
                    },
                )
                return BrowserTaskAssessment(
                    status="complete",
                    confidence=0.99,
                    summary="LinkedIn job cards are already visible on the results page.",
                    evidence=("job_cards_visible",),
                )
            assessment = await self._assess_search_surface(page, criteria=criteria)
            last_assessment = assessment
            append_output_jsonl(
                "run.log",
                {
                    "source": "linkedin_search",
                    "kind": "search_surface_assessment",
                    "attempt": attempt + 1,
                    "status": assessment.status,
                    "confidence": assessment.confidence,
                    "summary": assessment.summary,
                    "evidence": list(assessment.evidence),
                    "url": current_url,
                },
            )
            if assessment.status == "complete":
                return assessment
            if assessment.status == "blocked":
                msg = assessment.summary or "LinkedIn search is blocked on the current screen."
                raise LinkedInSearchError(msg)
            if attempt < 4:
                await page.wait_for_timeout(750 + (attempt * 450))
        msg = (
            last_assessment.summary
            if last_assessment is not None and last_assessment.summary
            else "LinkedIn search did not settle into a results surface."
        )
        raise LinkedInSearchError(msg)

    async def _search_results_ready(
        self,
        page: Page,
        *,
        criteria: LinkedInSearchCriteria,
    ) -> bool:
        if await self._wait_for_extractable_search_cards(page, attempts=1):
            return True
        assessment = await self._assess_search_surface(page, criteria=criteria)
        return assessment.status == "complete"

    async def _wait_for_extractable_search_cards(
        self,
        page: Page,
        *,
        attempts: int = 3,
    ) -> bool:
        current_url = getattr(page, "url", "")
        for attempt in range(max(1, attempts)):
            card_count = await self._count_extractable_search_cards(page)
            append_output_jsonl(
                "run.log",
                {
                    "source": "linkedin_search",
                    "kind": "search_cards_probe",
                    "attempt": attempt + 1,
                    "card_count": card_count,
                    "url": current_url,
                },
            )
            if card_count > 0:
                return True
            if attempt == 0:
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:  # noqa: BLE001
                    pass
            if attempt < attempts - 1:
                await page.wait_for_timeout(900 + (attempt * 350))
        return False

    async def _has_extractable_search_cards(self, page: Page) -> bool:
        return await self._count_extractable_search_cards(page) > 0

    async def _count_extractable_search_cards(self, page: Page) -> int:
        return await page.locator("a[href*='/jobs/view/']").count()

    async def _assess_search_surface(
        self,
        page: Page,
        *,
        criteria: LinkedInSearchCriteria,
    ) -> BrowserTaskAssessment:
        ai_api_key = criteria.ai_api_key or self._runtime_settings.openai_api_key
        if ai_api_key is None:
            msg = (
                "LinkedIn search needs the browser agent state assessor. "
                "Configure the OpenAI API key in the panel or set JOB_APPLIER_OPENAI_API_KEY."
            )
            raise LinkedInSearchError(msg)
        browser_agent = OpenAIResponsesBrowserAgent(
            api_key=ai_api_key,
            model=criteria.ai_model,
            stall_threshold=self._runtime_settings.resolved_browser_agent_stall_threshold,
            min_action_delay_ms=self._runtime_settings.linkedin_min_action_delay_ms,
            max_action_delay_ms=self._runtime_settings.linkedin_max_action_delay_ms,
            openai_max_retries=self._runtime_settings.resolved_openai_responses_max_retries,
            openai_retry_max_delay_seconds=(
                self._runtime_settings.openai_responses_retry_max_delay_seconds
            ),
        )
        try:
            return await browser_agent.assess_browser_task(
                page=page,
                goal=(
                    "Determine whether the current LinkedIn page is already a jobs search results "
                    "surface for the requested query and filters."
                ),
                task_name="linkedin_job_search_state",
                extra_rules=(
                    (
                        "Use complete only when the page already shows a jobs results surface, "
                        "including either job result cards or a clear empty-results state."
                    ),
                    (
                        "Use blocked when the page is an unrelated destination, an error, or a "
                        "state that prevents search results from appearing."
                    ),
                    "Use pending while filters are still applying or the results page is loading.",
                ),
            )
        except BrowserAutomationError as exc:
            raise LinkedInSearchError(str(exc)) from exc

    async def _extract_listing_cards(self, page: Page) -> list[LinkedInCollectedJob]:
        payloads = await page.locator("a[href*='/jobs/view/']").evaluate_all(
            """
            (nodes) => {
              const seen = new Set();
              const items = [];

              for (const node of nodes) {
                const href = node.href ? node.href.split("?")[0] : null;
                if (!href || seen.has(href)) {
                  continue;
                }
                seen.add(href);

                const container = node.closest("li") || node.closest("div");
                const lines = (container?.innerText || node.innerText || "")
                  .split("\\n")
                  .map((item) => item.trim())
                  .filter(Boolean);

                const title = lines[0] || node.textContent?.trim() || "";
                const companyName = lines[1] || "";
                const location = lines[2] || null;
                const externalMatch = href.match(/\\/jobs\\/view\\/(\\d+)/);

                items.push({
                  external_job_id: externalMatch ? externalMatch[1] : null,
                  url: href,
                  title,
                  company_name: companyName,
                  location,
                  easy_apply: lines.some((line) => /easy apply/i.test(line)),
                  metadata_text: lines.join(" | "),
                  description_raw: lines.join("\\n"),
                });
              }

              return items;
            }
            """,
        )
        return [LinkedInCollectedJob(**payload) for payload in payloads]

    async def _collect_listing_cards_from_results_page(
        self,
        page: Page,
        *,
        page_index: int,
    ) -> LinkedInResultsPageCollection:
        collected_by_url: dict[str, LinkedInCollectedJob] = {}
        stale_rounds = 0
        last_visible_listing_count = 0
        completed_rounds = 0

        for round_index in range(RESULTS_PAGE_MAX_SCROLL_ROUNDS):
            visible_listings = await self._extract_listing_cards(page)
            last_visible_listing_count = len(visible_listings)
            new_listing_count = 0
            duplicate_listing_count = 0

            for listing in visible_listings:
                if listing.url in collected_by_url:
                    duplicate_listing_count += 1
                    continue
                collected_by_url[listing.url] = listing
                new_listing_count += 1

            scroll_state = await self._scroll_results_surface(page)
            completed_rounds = round_index + 1
            append_output_jsonl(
                "run.log",
                {
                    "source": "linkedin_search",
                    "kind": "results_page_collection_round",
                    "page_index": page_index,
                    "round_index": completed_rounds,
                    "url": page.url,
                    "visible_listing_count": last_visible_listing_count,
                    "unique_listing_count": len(collected_by_url),
                    "new_listing_count": new_listing_count,
                    "duplicate_listing_count": duplicate_listing_count,
                    **scroll_state,
                },
            )

            if new_listing_count == 0:
                stale_rounds += 1
            else:
                stale_rounds = 0

            scrolled = bool(scroll_state.get("scrolled", False))
            if stale_rounds >= RESULTS_PAGE_STALE_SCROLL_ROUNDS:
                break
            if not scrolled:
                break
            await page.wait_for_timeout(650 + min(round_index, 4) * 120)

        return LinkedInResultsPageCollection(
            listings=tuple(collected_by_url.values()),
            rounds=completed_rounds,
            visible_listing_count=last_visible_listing_count,
            stale_rounds=stale_rounds,
        )

    async def _scroll_results_surface(self, page: Page) -> dict[str, object]:
        payload = await page.evaluate(
            """
            () => {
              const jobSelector = "a[href*='/jobs/view/']";
              const isScrollable = (node) => {
                if (!node || node.nodeType !== 1) {
                  return false;
                }
                const style = window.getComputedStyle(node);
                const overflowY = style.overflowY || style.overflow;
                return (
                  ["auto", "scroll", "overlay"].includes(overflowY)
                  && node.scrollHeight - node.clientHeight > 24
                );
              };
              const scoreContainer = (node) => node.querySelectorAll(jobSelector).length;
              const anchors = Array.from(document.querySelectorAll(jobSelector));
              let best = null;

              for (const anchor of anchors) {
                let current = anchor.parentElement;
                let depth = 0;
                while (current && current !== document.body && depth < 8) {
                  if (isScrollable(current)) {
                    const score = scoreContainer(current);
                    const area = current.clientWidth * current.clientHeight;
                    if (
                      !best
                      || score > best.score
                      || (score === best.score && area < best.area)
                    ) {
                      best = { node: current, score, area };
                    }
                  }
                  current = current.parentElement;
                  depth += 1;
                }
              }

              if (best && best.node) {
                const node = best.node;
                const before = node.scrollTop;
                const step = Math.max(240, Math.round(node.clientHeight * 0.8));
                node.scrollBy({ top: step, behavior: "instant" });
                const after = node.scrollTop;
                return {
                  scroll_mode: "container",
                  scrolled: after > before + 1,
                  can_continue: after + node.clientHeight < node.scrollHeight - 8,
                  scroll_top_before: before,
                  scroll_top_after: after,
                  scroll_height: node.scrollHeight,
                  client_height: node.clientHeight,
                  container_job_anchor_count: scoreContainer(node),
                };
              }

              const root = document.scrollingElement || document.documentElement;
              const before = root.scrollTop || window.scrollY || 0;
              const step = Math.max(320, Math.round(window.innerHeight * 0.8));
              window.scrollBy({ top: step, behavior: "instant" });
              const after = root.scrollTop || window.scrollY || 0;
              const viewportHeight = window.innerHeight || root.clientHeight || 0;
              return {
                scroll_mode: "page",
                scrolled: after > before + 1,
                can_continue: after + viewportHeight < root.scrollHeight - 8,
                scroll_top_before: before,
                scroll_top_after: after,
                scroll_height: root.scrollHeight,
                client_height: viewportHeight,
                container_job_anchor_count: anchors.length,
              };
            }
            """
        )
        if not isinstance(payload, dict):
            return {
                "scroll_mode": "unknown",
                "scrolled": False,
                "can_continue": False,
            }
        return payload

    async def _load_job_details(
        self,
        context: BrowserContext,
        listing: LinkedInCollectedJob,
    ) -> LinkedInCollectedJob:
        detail_page = await context.new_page()
        try:
            await self._pause_before_navigation(detail_page, reason="job_detail_open")
            await detail_page.goto(listing.url, wait_until="domcontentloaded")
            await self._ensure_authenticated_page(detail_page)
            detail_payload = await detail_page.evaluate(
                """
                () => {
                  const firstText = (selectors) => {
                    for (const selector of selectors) {
                      const element = document.querySelector(selector);
                      if (element && element.innerText.trim()) {
                        return element.innerText.trim();
                      }
                    }
                    return "";
                  };

                  const description = firstText([
                    ".jobs-description-content__text",
                    "#job-details",
                    "[data-job-detail-container] .jobs-box__html-content",
                    "main",
                  ]);
                  const title = firstText([
                    ".job-details-jobs-unified-top-card__job-title",
                    ".jobs-unified-top-card h1",
                    ".jobs-unified-top-card__job-title",
                    "h1",
                  ]);
                  const companyName = firstText([
                    ".job-details-jobs-unified-top-card__company-name a",
                    ".job-details-jobs-unified-top-card__company-name",
                    ".jobs-unified-top-card__company-name a",
                    ".jobs-unified-top-card__company-name",
                  ]);
                  const location = firstText([
                    ".job-details-jobs-unified-top-card__primary-description-container",
                    ".jobs-unified-top-card__primary-description",
                    ".jobs-unified-top-card__bullet",
                  ]);
                  const bodyText = document.body ? document.body.innerText : "";

                  const documentTitle = (document.title || "").split("|")[0].trim();

                  return {
                    title: title || documentTitle,
                    company_name: companyName,
                    location,
                    description_raw: description,
                    metadata_text: bodyText,
                    easy_apply: /easy apply/i.test(bodyText),
                  };
                }
                """,
            )
            append_timeline_event(
                "linkedin_job_detail_loaded",
                {
                    "external_job_id": listing.external_job_id,
                    "title": listing.title,
                    "company_name": listing.company_name,
                    "url": listing.url,
                    "description_length": len(str(detail_payload["description_raw"] or "")),
                },
            )
        finally:
            await detail_page.close()

        detail_text = str(detail_payload["metadata_text"])
        description_raw = str(detail_payload["description_raw"])
        return LinkedInCollectedJob(
            external_job_id=listing.external_job_id,
            url=listing.url,
            title=str(detail_payload.get("title") or "").strip() or listing.title,
            company_name=str(detail_payload.get("company_name") or "").strip()
            or listing.company_name,
            location=str(detail_payload.get("location") or "").strip() or listing.location,
            description_raw=description_raw or listing.description_raw,
            easy_apply=listing.easy_apply or bool(detail_payload["easy_apply"]),
            metadata_text=f"{listing.metadata_text} {detail_text}".strip(),
            workplace_type=infer_workplace_type(detail_text) or listing.workplace_type,
            seniority=infer_seniority(detail_text) or listing.seniority,
        )

    async def _capture_debug_target_artifact(
        self,
        context: BrowserContext,
        *,
        target_url: str,
        run_dir: Path,
    ) -> None:
        detail_page = await context.new_page()
        try:
            await self._pause_before_navigation(detail_page, reason="debug_target_job_artifact")
            await detail_page.goto(target_url, wait_until="domcontentloaded")
            await self._ensure_authenticated_page(detail_page)
            await self._capture_screenshot(detail_page, run_dir / "debug-target-job.png")
        finally:
            await detail_page.close()

    def _extract_job_id_from_url(self, url: str) -> str | None:
        match = re.search(r"/jobs/view/(\d+)", url)
        return match.group(1) if match else None

    async def _ensure_authenticated_page(self, page: Page) -> None:
        session_manager = self._get_session_manager()
        if await session_manager.page_requires_login(page):
            raise LinkedInAuthError("LinkedIn session expired during search execution.")

    async def _capture_screenshot(self, page: Page, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True)
        append_artifact_reference(
            artifact_type="screenshot",
            label=path.stem,
            path=path,
        )

    def _build_run_dir(self) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = self._runtime_settings.resolved_linkedin_artifacts_dir / timestamp
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    async def _pause_before_navigation(self, page: Page, *, reason: str) -> None:
        delay_ms = random.randint(
            self._runtime_settings.linkedin_min_navigation_delay_ms,
            self._runtime_settings.linkedin_max_navigation_delay_ms,
        )
        logger.info("linkedin_navigation_delay", extra={"reason": reason, "delay_ms": delay_ms})
        await page.wait_for_timeout(delay_ms)


def _workplace_option_name(workplace_type: WorkplaceType) -> str:
    mapping = {
        WorkplaceType.REMOTE: "Remote",
        WorkplaceType.HYBRID: "Hybrid",
        WorkplaceType.ONSITE: "On-site",
    }
    return mapping[workplace_type]


def _seniority_option_name(level: SeniorityLevel) -> str:
    mapping = {
        SeniorityLevel.INTERN: "Internship",
        SeniorityLevel.JUNIOR: "Entry level",
        SeniorityLevel.MID: "Associate",
        SeniorityLevel.SENIOR: "Mid-Senior level",
        SeniorityLevel.STAFF: "Mid-Senior level",
        SeniorityLevel.PRINCIPAL: "Director",
        SeniorityLevel.MANAGER: "Director",
        SeniorityLevel.DIRECTOR: "Director",
    }
    return mapping[level]
