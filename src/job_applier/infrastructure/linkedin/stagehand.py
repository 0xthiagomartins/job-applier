"""Optional Stagehand-powered semantic extraction for volatile LinkedIn job pages."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright
from pydantic import SecretStr
from stagehand import AsyncStagehand, StagehandError
from stagehand.types.session_start_params import (
    Browser as StagehandBrowser,
)
from stagehand.types.session_start_params import (
    BrowserLaunchOptions as StagehandBrowserLaunchOptions,
)

from job_applier.observability import append_output_jsonl
from job_applier.settings import RuntimeSettings

logger = logging.getLogger(__name__)

_JOB_DETAIL_EXTRACT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": ["string", "null"]},
        "company_name": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]},
        "description_raw": {"type": ["string", "null"]},
        "easy_apply_visible": {"type": ["boolean", "null"]},
        "page_summary": {"type": ["string", "null"]},
    },
    "required": [
        "title",
        "company_name",
        "location",
        "description_raw",
        "easy_apply_visible",
        "page_summary",
    ],
}

_SEARCH_RESULTS_EXTRACT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "page_summary": {"type": ["string", "null"]},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "url": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "company_name": {"type": ["string", "null"]},
                    "location": {"type": ["string", "null"]},
                    "easy_apply_visible": {"type": ["boolean", "null"]},
                },
                "required": [
                    "url",
                    "title",
                    "company_name",
                    "location",
                    "easy_apply_visible",
                ],
            },
        },
    },
    "required": ["page_summary", "results"],
}

_SEARCH_SURFACE_EXTRACT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "page_summary": {"type": ["string", "null"]},
        "results_ready": {"type": ["boolean", "null"]},
        "loading": {"type": ["boolean", "null"]},
        "empty_state_visible": {"type": ["boolean", "null"]},
        "blocked": {"type": ["boolean", "null"]},
        "blocker_summary": {"type": ["string", "null"]},
        "easy_apply_filter_active": {"type": ["boolean", "null"]},
        "posted_within_24h_active": {"type": ["boolean", "null"]},
    },
    "required": [
        "page_summary",
        "results_ready",
        "loading",
        "empty_state_visible",
        "blocked",
        "blocker_summary",
        "easy_apply_filter_active",
        "posted_within_24h_active",
    ],
}


class StagehandLinkedInError(RuntimeError):
    """Raised when the Stagehand semantic extractor cannot complete one job page."""


@dataclass(frozen=True, slots=True)
class StagehandObservedAction:
    """One structured action returned by Stagehand observe()."""

    description: str
    selector: str
    method: str | None
    arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StagehandJobDetailExtraction:
    """Structured semantic job detail extracted through Stagehand."""

    title: str | None
    company_name: str | None
    location: str | None
    description_raw: str | None
    easy_apply_visible: bool | None
    page_summary: str | None
    observed_apply_actions: tuple[StagehandObservedAction, ...]

    def to_detail_payload(self) -> dict[str, object]:
        """Convert the extraction into the search payload format used by the parser."""

        return {
            "structured_title": self.title or "",
            "structured_company_name": self.company_name or "",
            "location": self.location or "",
            "description_raw": self.description_raw or "",
            "easy_apply": bool(self.easy_apply_visible),
            "metadata_text": self.page_summary or "",
            "stagehand_apply_actions": [
                {
                    "description": item.description,
                    "selector": item.selector,
                    "method": item.method,
                    "arguments": list(item.arguments),
                }
                for item in self.observed_apply_actions
            ],
        }


@dataclass(frozen=True, slots=True)
class StagehandSearchResultCardExtraction:
    """One LinkedIn search result card extracted semantically through Stagehand."""

    url: str
    title: str | None
    company_name: str | None
    location: str | None
    easy_apply_visible: bool | None

    def to_listing_patch(self) -> dict[str, object]:
        """Return a conservative patch that can enrich an existing listing card."""

        return {
            "url": self.url,
            "title": self.title or "",
            "company_name": self.company_name or "",
            "location": self.location or "",
            "easy_apply": bool(self.easy_apply_visible),
        }


@dataclass(frozen=True, slots=True)
class StagehandSearchSurfaceExtraction:
    """Semantic interpretation of the current LinkedIn search results surface."""

    page_summary: str | None
    results_ready: bool | None
    loading: bool | None
    empty_state_visible: bool | None
    blocked: bool | None
    blocker_summary: str | None
    easy_apply_filter_active: bool | None
    posted_within_24h_active: bool | None


class StagehandLinkedInJobDetailExtractor:
    """Use Stagehand observe/extract to repair noisy LinkedIn job detail parsing."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model_name: str,
        runtime_settings: RuntimeSettings,
    ) -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._runtime_settings = runtime_settings

    async def extract_job_detail(
        self,
        *,
        url: str,
        storage_state_path: Path,
        chrome_path: str | None = None,
    ) -> StagehandJobDetailExtraction:
        """Return a semantic view of one LinkedIn job page using Stagehand."""

        with _stagehand_environment(self._runtime_settings.resolved_stagehand_cache_dir):
            client = AsyncStagehand(
                server="local",
                model_api_key=self._api_key.get_secret_value(),
                local_headless=self._runtime_settings.playwright_headless,
                local_chrome_path=chrome_path
                or _stringify_path(self._runtime_settings.stagehand_local_chrome_path),
                local_shutdown_on_close=True,
            )
            session = None
            browser = None
            playwright_manager = None
            try:
                browser_launch_options: StagehandBrowserLaunchOptions = {
                    "headless": self._runtime_settings.playwright_headless,
                }
                if chrome_path:
                    browser_launch_options["executable_path"] = chrome_path
                elif self._runtime_settings.stagehand_local_chrome_path is not None:
                    browser_launch_options["executable_path"] = str(
                        self._runtime_settings.stagehand_local_chrome_path
                    )
                browser_config: StagehandBrowser = {
                    "type": "local",
                    "launch_options": browser_launch_options,
                }
                session = await client.sessions.start(
                    model_name=self._model_name,
                    browser=browser_config,
                    self_heal=True,
                    verbose=0,
                    system_prompt=(
                        "You are reading a LinkedIn job page for an automation system. "
                        "Focus only on the main job detail surface and the main application "
                        "entry action. Ignore premium banners, ads, sidebars, recruiter promos, "
                        "share/save/follow controls, and unrelated navigation."
                    ),
                )
                cdp_url = session.data.cdp_url
                if not cdp_url:
                    msg = "Stagehand did not return a CDP URL for the local browser session."
                    raise StagehandLinkedInError(msg)

                playwright_manager = await async_playwright().start()
                browser = await playwright_manager.chromium.connect_over_cdp(cdp_url)
                context = _resolve_cdp_context(browser.contexts)
                page = await _resolve_cdp_page(context)
                await _restore_storage_state(
                    context=context,
                    page=page,
                    storage_state_path=storage_state_path,
                )
                await session.navigate(
                    page=page,
                    url=url,
                    options={
                        "wait_until": "domcontentloaded",
                        "timeout": float(self._runtime_settings.linkedin_default_timeout_ms),
                    },
                )
                await page.wait_for_timeout(1_250)
                observed_apply_actions = await self._observe_apply_actions(
                    session=session,
                    page=page,
                )
                extraction = await self._extract_semantic_job_detail(session=session, page=page)
                result = StagehandJobDetailExtraction(
                    title=_clean_text(extraction.get("title")),
                    company_name=_clean_text(extraction.get("company_name")),
                    location=_clean_text(extraction.get("location")),
                    description_raw=_clean_text(extraction.get("description_raw")),
                    easy_apply_visible=_coerce_bool(extraction.get("easy_apply_visible")),
                    page_summary=_clean_text(extraction.get("page_summary")),
                    observed_apply_actions=observed_apply_actions,
                )
                append_output_jsonl(
                    "stagehand/job-detail.jsonl",
                    {
                        "kind": "extraction_completed",
                        "url": url,
                        "model_name": self._model_name,
                        "title": result.title,
                        "company_name": result.company_name,
                        "location": result.location,
                        "easy_apply_visible": result.easy_apply_visible,
                        "observed_apply_actions": [
                            {
                                "description": item.description,
                                "selector": item.selector,
                                "method": item.method,
                                "arguments": list(item.arguments),
                            }
                            for item in result.observed_apply_actions
                        ],
                    },
                )
                return result
            except StagehandLinkedInError:
                raise
            except StagehandError as exc:
                append_output_jsonl(
                    "stagehand/job-detail.jsonl",
                    {
                        "kind": "stagehand_error",
                        "url": url,
                        "message": str(exc),
                    },
                )
                raise StagehandLinkedInError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                append_output_jsonl(
                    "stagehand/job-detail.jsonl",
                    {
                        "kind": "unexpected_error",
                        "url": url,
                        "message": str(exc),
                    },
                )
                raise StagehandLinkedInError(str(exc)) from exc
            finally:
                if session is not None:
                    try:
                        await session.end()
                    except Exception:  # noqa: BLE001
                        logger.debug("stagehand_session_end_failed", exc_info=True)
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("stagehand_browser_close_failed", exc_info=True)
                if playwright_manager is not None:
                    try:
                        await playwright_manager.stop()
                    except Exception:  # noqa: BLE001
                        logger.debug("stagehand_playwright_stop_failed", exc_info=True)
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    logger.debug("stagehand_client_close_failed", exc_info=True)

    async def extract_search_results_page(
        self,
        *,
        url: str,
        storage_state_path: Path,
        chrome_path: str | None = None,
    ) -> tuple[StagehandSearchResultCardExtraction, ...]:
        """Return visible LinkedIn search result cards with cleaner semantics."""

        with _stagehand_environment(self._runtime_settings.resolved_stagehand_cache_dir):
            client = AsyncStagehand(
                server="local",
                model_api_key=self._api_key.get_secret_value(),
                local_headless=self._runtime_settings.playwright_headless,
                local_chrome_path=chrome_path
                or _stringify_path(self._runtime_settings.stagehand_local_chrome_path),
                local_shutdown_on_close=True,
            )
            session = None
            browser = None
            playwright_manager = None
            try:
                browser_launch_options: StagehandBrowserLaunchOptions = {
                    "headless": self._runtime_settings.playwright_headless,
                }
                if chrome_path:
                    browser_launch_options["executable_path"] = chrome_path
                elif self._runtime_settings.stagehand_local_chrome_path is not None:
                    browser_launch_options["executable_path"] = str(
                        self._runtime_settings.stagehand_local_chrome_path
                    )
                browser_config: StagehandBrowser = {
                    "type": "local",
                    "launch_options": browser_launch_options,
                }
                session = await client.sessions.start(
                    model_name=self._model_name,
                    browser=browser_config,
                    self_heal=True,
                    verbose=0,
                    system_prompt=(
                        "You are reading a LinkedIn jobs search results page for an automation "
                        "system. Focus only on the main search results list. LinkedIn often shows "
                        "a split layout where the selected job detail is on the right and the "
                        "actual result cards are in a list on the left. Treat only that results "
                        "list as the source of truth for job cards. Ignore the selected job detail "
                        "pane, premium upsells, job recommendations outside the list, sidebars, "
                        "banners, ads, recruiter promos, and global navigation."
                    ),
                )
                cdp_url = session.data.cdp_url
                if not cdp_url:
                    msg = "Stagehand did not return a CDP URL for the local browser session."
                    raise StagehandLinkedInError(msg)

                playwright_manager = await async_playwright().start()
                browser = await playwright_manager.chromium.connect_over_cdp(cdp_url)
                context = _resolve_cdp_context(browser.contexts)
                page = await _resolve_cdp_page(context)
                await _restore_storage_state(
                    context=context,
                    page=page,
                    storage_state_path=storage_state_path,
                )
                await session.navigate(
                    page=page,
                    url=url,
                    options={
                        "wait_until": "domcontentloaded",
                        "timeout": float(self._runtime_settings.linkedin_default_timeout_ms),
                    },
                )
                await page.wait_for_timeout(1_250)
                extraction = await self._extract_search_results_page(session=session, page=page)
                raw_results = extraction.get("results")
                results = raw_results if isinstance(raw_results, list) else []
                if not results:
                    extraction = await self._extract_search_results_page_retry(
                        session=session,
                        page=page,
                    )
                    raw_results = extraction.get("results")
                    results = raw_results if isinstance(raw_results, list) else []
                cards = tuple(
                    StagehandSearchResultCardExtraction(
                        url=normalized_url,
                        title=_clean_card_label(item.get("title")),
                        company_name=_clean_card_label(item.get("company_name")),
                        location=_clean_card_label(item.get("location")),
                        easy_apply_visible=_coerce_bool(item.get("easy_apply_visible")),
                    )
                    for item in results
                    if isinstance(item, dict)
                    and (normalized_url := _normalize_linkedin_job_url(item.get("url"))) is not None
                )
                if not cards:
                    observed_actions = await self._observe_search_result_actions(
                        session=session,
                        page=page,
                    )
                    cards = await self._extract_cards_from_observed_actions(
                        page=page,
                        actions=observed_actions,
                    )
                append_output_jsonl(
                    "stagehand/search-results.jsonl",
                    {
                        "kind": "extraction_completed",
                        "url": url,
                        "model_name": self._model_name,
                        "page_summary": _clean_text(extraction.get("page_summary")),
                        "card_count": len(cards),
                        "cards": [
                            {
                                "url": item.url,
                                "title": item.title,
                                "company_name": item.company_name,
                                "location": item.location,
                                "easy_apply_visible": item.easy_apply_visible,
                            }
                            for item in cards
                        ],
                    },
                )
                return cards
            except StagehandLinkedInError:
                raise
            except StagehandError as exc:
                append_output_jsonl(
                    "stagehand/search-results.jsonl",
                    {
                        "kind": "stagehand_error",
                        "url": url,
                        "message": str(exc),
                    },
                )
                raise StagehandLinkedInError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                append_output_jsonl(
                    "stagehand/search-results.jsonl",
                    {
                        "kind": "unexpected_error",
                        "url": url,
                        "message": str(exc),
                    },
                )
                raise StagehandLinkedInError(str(exc)) from exc
            finally:
                if session is not None:
                    try:
                        await session.end()
                    except Exception:  # noqa: BLE001
                        logger.debug("stagehand_session_end_failed", exc_info=True)
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("stagehand_browser_close_failed", exc_info=True)
                if playwright_manager is not None:
                    try:
                        await playwright_manager.stop()
                    except Exception:  # noqa: BLE001
                        logger.debug("stagehand_playwright_stop_failed", exc_info=True)
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    logger.debug("stagehand_client_close_failed", exc_info=True)

    async def extract_search_surface_state(
        self,
        *,
        url: str,
        storage_state_path: Path,
        chrome_path: str | None = None,
    ) -> StagehandSearchSurfaceExtraction:
        """Return a semantic assessment of the current LinkedIn search surface."""

        with _stagehand_environment(self._runtime_settings.resolved_stagehand_cache_dir):
            client = AsyncStagehand(
                server="local",
                model_api_key=self._api_key.get_secret_value(),
                local_headless=self._runtime_settings.playwright_headless,
                local_chrome_path=chrome_path
                or _stringify_path(self._runtime_settings.stagehand_local_chrome_path),
                local_shutdown_on_close=True,
            )
            session = None
            browser = None
            playwright_manager = None
            try:
                browser_launch_options: StagehandBrowserLaunchOptions = {
                    "headless": self._runtime_settings.playwright_headless,
                }
                if chrome_path:
                    browser_launch_options["executable_path"] = chrome_path
                elif self._runtime_settings.stagehand_local_chrome_path is not None:
                    browser_launch_options["executable_path"] = str(
                        self._runtime_settings.stagehand_local_chrome_path
                    )
                browser_config: StagehandBrowser = {
                    "type": "local",
                    "launch_options": browser_launch_options,
                }
                session = await client.sessions.start(
                    model_name=self._model_name,
                    browser=browser_config,
                    self_heal=True,
                    verbose=0,
                    system_prompt=(
                        "You are reading a LinkedIn jobs search page for an automation system. "
                        "Focus only on the main jobs search surface. Ignore ads, premium "
                        "upsells, global navigation, messaging overlays, and sidebars unless "
                        "they block the search results."
                    ),
                )
                cdp_url = session.data.cdp_url
                if not cdp_url:
                    msg = "Stagehand did not return a CDP URL for the local browser session."
                    raise StagehandLinkedInError(msg)

                playwright_manager = await async_playwright().start()
                browser = await playwright_manager.chromium.connect_over_cdp(cdp_url)
                context = _resolve_cdp_context(browser.contexts)
                page = await _resolve_cdp_page(context)
                await _restore_storage_state(
                    context=context,
                    page=page,
                    storage_state_path=storage_state_path,
                )
                await session.navigate(
                    page=page,
                    url=url,
                    options={
                        "wait_until": "domcontentloaded",
                        "timeout": float(self._runtime_settings.linkedin_default_timeout_ms),
                    },
                )
                await page.wait_for_timeout(1_250)
                extraction = await self._extract_search_surface_state(session=session, page=page)
                result = StagehandSearchSurfaceExtraction(
                    page_summary=_clean_text(extraction.get("page_summary")),
                    results_ready=_coerce_bool(extraction.get("results_ready")),
                    loading=_coerce_bool(extraction.get("loading")),
                    empty_state_visible=_coerce_bool(extraction.get("empty_state_visible")),
                    blocked=_coerce_bool(extraction.get("blocked")),
                    blocker_summary=_clean_text(extraction.get("blocker_summary")),
                    easy_apply_filter_active=_coerce_bool(
                        extraction.get("easy_apply_filter_active")
                    ),
                    posted_within_24h_active=_coerce_bool(
                        extraction.get("posted_within_24h_active")
                    ),
                )
                append_output_jsonl(
                    "stagehand/search-surface.jsonl",
                    {
                        "kind": "extraction_completed",
                        "url": url,
                        "model_name": self._model_name,
                        "page_summary": result.page_summary,
                        "results_ready": result.results_ready,
                        "loading": result.loading,
                        "empty_state_visible": result.empty_state_visible,
                        "blocked": result.blocked,
                        "blocker_summary": result.blocker_summary,
                        "easy_apply_filter_active": result.easy_apply_filter_active,
                        "posted_within_24h_active": result.posted_within_24h_active,
                    },
                )
                return result
            except StagehandLinkedInError:
                raise
            except StagehandError as exc:
                append_output_jsonl(
                    "stagehand/search-surface.jsonl",
                    {
                        "kind": "stagehand_error",
                        "url": url,
                        "message": str(exc),
                    },
                )
                raise StagehandLinkedInError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                append_output_jsonl(
                    "stagehand/search-surface.jsonl",
                    {
                        "kind": "unexpected_error",
                        "url": url,
                        "message": str(exc),
                    },
                )
                raise StagehandLinkedInError(str(exc)) from exc
            finally:
                if session is not None:
                    try:
                        await session.end()
                    except Exception:  # noqa: BLE001
                        logger.debug("stagehand_session_end_failed", exc_info=True)
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("stagehand_browser_close_failed", exc_info=True)
                if playwright_manager is not None:
                    try:
                        await playwright_manager.stop()
                    except Exception:  # noqa: BLE001
                        logger.debug("stagehand_playwright_stop_failed", exc_info=True)
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    logger.debug("stagehand_client_close_failed", exc_info=True)

    async def _observe_apply_actions(
        self,
        *,
        session: Any,
        page: Page,
    ) -> tuple[StagehandObservedAction, ...]:
        response = await session.observe(
            page=page,
            instruction=(
                "Find the click actions on this LinkedIn job page that would start the main "
                "job application flow for the candidate. Prefer Easy Apply or the primary "
                "Apply action. Ignore Save, Share, Follow, recruiter messaging, and secondary "
                "navigation."
            ),
            options={"timeout": float(self._runtime_settings.linkedin_default_timeout_ms)},
        )
        actions = tuple(
            StagehandObservedAction(
                description=_clean_text(item.description) or "",
                selector=_clean_text(item.selector) or "",
                method=_clean_text(item.method),
                arguments=tuple(str(argument) for argument in (item.arguments or [])),
            )
            for item in response.data.result
            if _clean_text(item.selector)
        )
        append_output_jsonl(
            "stagehand/job-detail.jsonl",
            {
                "kind": "observe_completed",
                "action_count": len(actions),
                "actions": [
                    {
                        "description": item.description,
                        "selector": item.selector,
                        "method": item.method,
                        "arguments": list(item.arguments),
                    }
                    for item in actions
                ],
            },
        )
        return actions

    async def _extract_semantic_job_detail(
        self,
        *,
        session: Any,
        page: Page,
    ) -> dict[str, object]:
        response = await session.extract(
            page=page,
            instruction=(
                "Extract the main LinkedIn job details from the primary job detail surface. "
                "Ignore premium upsells, recruiter promos, suggested jobs, sidebars, save/share "
                "controls, and unrelated page chrome."
            ),
            schema=_JOB_DETAIL_EXTRACT_SCHEMA,
            options={"timeout": float(self._runtime_settings.linkedin_default_timeout_ms)},
        )
        result = response.data.result
        if not isinstance(result, dict):
            msg = "Stagehand returned an invalid job-detail extraction payload."
            raise StagehandLinkedInError(msg)
        return result

    async def _extract_search_results_page(
        self,
        *,
        session: Any,
        page: Page,
    ) -> dict[str, object]:
        response = await session.extract(
            page=page,
            instruction=(
                "Extract the visible job result cards from the main LinkedIn jobs search results "
                "list. The selected job detail pane on the right is not part of the results list "
                "and must be ignored. Return only real visible job cards that a candidate could "
                "open from the results list, with their canonical job URL when visible. If job "
                "cards are visible in the left-side list, do not return an empty list."
            ),
            schema=_SEARCH_RESULTS_EXTRACT_SCHEMA,
            options={"timeout": float(self._runtime_settings.linkedin_default_timeout_ms)},
        )
        result = response.data.result
        if not isinstance(result, dict):
            msg = "Stagehand returned an invalid search-results extraction payload."
            raise StagehandLinkedInError(msg)
        return result

    async def _extract_search_results_page_retry(
        self,
        *,
        session: Any,
        page: Page,
    ) -> dict[str, object]:
        response = await session.extract(
            page=page,
            instruction=(
                "Retry with a narrower focus. Extract up to the first 10 visible job rows from the "
                "left-side LinkedIn results list only. Ignore the currently selected job detail on "
                "the right, all banners, ads, recommendations, and any controls that are not part "
                "of a visible job row. For each visible row, capture the canonical LinkedIn job "
                "URL, title, company, location, and whether Easy Apply is visibly indicated. If a "
                "field is unclear, return null for that field, but do not drop the row."
            ),
            schema=_SEARCH_RESULTS_EXTRACT_SCHEMA,
            options={"timeout": float(self._runtime_settings.linkedin_default_timeout_ms)},
        )
        result = response.data.result
        if not isinstance(result, dict):
            msg = "Stagehand returned an invalid retry search-results extraction payload."
            raise StagehandLinkedInError(msg)
        append_output_jsonl(
            "stagehand/search-results.jsonl",
            {
                "kind": "retry_completed",
                "page_summary": _clean_text(result.get("page_summary")),
            },
        )
        return result

    async def _observe_search_result_actions(
        self,
        *,
        session: Any,
        page: Page,
    ) -> tuple[StagehandObservedAction, ...]:
        response = await session.observe(
            page=page,
            instruction=(
                "Find the visible click actions that would open individual job result rows from "
                "the main left-side LinkedIn results list. Ignore the selected job detail pane, "
                "global navigation, filters, save buttons, share buttons, and recruiter promos."
            ),
            options={"timeout": float(self._runtime_settings.linkedin_default_timeout_ms)},
        )
        actions = tuple(
            StagehandObservedAction(
                description=_clean_text(item.description) or "",
                selector=_clean_text(item.selector) or "",
                method=_clean_text(item.method),
                arguments=tuple(str(argument) for argument in (item.arguments or [])),
            )
            for item in response.data.result
            if _clean_text(item.selector)
        )
        append_output_jsonl(
            "stagehand/search-results.jsonl",
            {
                "kind": "observe_completed",
                "action_count": len(actions),
                "actions": [
                    {
                        "description": item.description,
                        "selector": item.selector,
                        "method": item.method,
                        "arguments": list(item.arguments),
                    }
                    for item in actions
                ],
            },
        )
        return actions

    async def _extract_cards_from_observed_actions(
        self,
        *,
        page: Page,
        actions: tuple[StagehandObservedAction, ...],
    ) -> tuple[StagehandSearchResultCardExtraction, ...]:
        selectors = [action.selector for action in actions if action.selector]
        if not selectors:
            return ()
        unique_cards: dict[str, StagehandSearchResultCardExtraction] = {}

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                payload = await locator.evaluate(
                    """
                    (node) => {
                      const cleanText = (value) => (value || "").replace(/\\s+/g, " ").trim();
                      const firstTextWithin = (container, selectors) => {
                        if (!container) return "";
                        for (const selector of selectors) {
                          const element = container.querySelector(selector);
                          const text = cleanText(element?.innerText || element?.textContent || "");
                          if (text) return text;
                        }
                        return "";
                      };
                      const anchor = (
                        node.closest("a[href*='/jobs/view/']")
                        || node.querySelector?.("a[href*='/jobs/view/']")
                        || (node.matches?.("a[href*='/jobs/view/']") ? node : null)
                      );
                      if (!anchor || !anchor.href) return null;
                      const url = anchor.href.split("?")[0];
                      if (!url.includes("/jobs/view/")) return null;
                      const container = (
                        anchor.closest(".job-card-container")
                        || anchor.closest("[data-occludable-job-id]")
                        || anchor.closest("[data-job-id]")
                        || anchor.closest("li")
                        || anchor.closest("div")
                      );
                      const lines = cleanText(container?.innerText || anchor.innerText || "")
                        .split(/\\n+/)
                        .map((item) => cleanText(item))
                        .filter(Boolean);
                      const title = cleanText(
                        firstTextWithin(container, [
                          ".job-card-list__title",
                          ".artdeco-entity-lockup__title",
                          ".job-card-container__link",
                          "strong",
                        ]) || anchor.innerText || lines[0] || ""
                      );
                      const companyName = cleanText(
                        firstTextWithin(container, [
                          ".artdeco-entity-lockup__subtitle span",
                          ".artdeco-entity-lockup__subtitle",
                          ".job-card-container__primary-description",
                          ".job-card-container__company-name",
                        ]) || lines[1] || ""
                      );
                      const location = cleanText(
                        firstTextWithin(container, [
                          ".job-card-container__metadata-item",
                          ".artdeco-entity-lockup__caption",
                          ".job-card-container__footer-item",
                        ]) || lines[2] || ""
                      );
                      return {
                        url,
                        title: title || null,
                        company_name: companyName || null,
                        location: location || null,
                        easy_apply_visible: lines.some((line) => /easy apply/i.test(line)),
                      };
                    }
                    """
                )
            except Exception:  # noqa: BLE001
                logger.debug("stagehand_search_results_observe_hydration_failed", exc_info=True)
                continue
            if not isinstance(payload, dict):
                continue
            normalized_url = _normalize_linkedin_job_url(payload.get("url"))
            if normalized_url is None or normalized_url in unique_cards:
                continue
            unique_cards[normalized_url] = StagehandSearchResultCardExtraction(
                url=normalized_url,
                title=_clean_card_label(payload.get("title")),
                company_name=_clean_card_label(payload.get("company_name")),
                location=_clean_card_label(payload.get("location")),
                easy_apply_visible=_coerce_bool(payload.get("easy_apply_visible")),
            )

        return tuple(unique_cards.values())

    async def _extract_search_surface_state(
        self,
        *,
        session: Any,
        page: Page,
    ) -> dict[str, object]:
        response = await session.extract(
            page=page,
            instruction=(
                "Assess the current LinkedIn jobs search surface. Decide whether the main "
                "search results area is ready for extraction, still loading, blocked by an "
                "unexpected screen, or showing a legitimate empty-results state. Also infer "
                "whether the Easy Apply and Past 24 hours filters appear active on the current "
                "results surface when that can be determined confidently. Return null instead of "
                "false when the filter state is ambiguous."
            ),
            schema=_SEARCH_SURFACE_EXTRACT_SCHEMA,
            options={"timeout": float(self._runtime_settings.linkedin_default_timeout_ms)},
        )
        result = response.data.result
        if not isinstance(result, dict):
            msg = "Stagehand returned an invalid search-surface extraction payload."
            raise StagehandLinkedInError(msg)
        return result


def resolve_stagehand_model_name(model_name: str) -> str:
    """Normalize user-facing model names to the provider/model format Stagehand expects."""

    normalized = (model_name or "").strip()
    if not normalized:
        return "openai/gpt-4.1-mini"
    if "/" in normalized:
        return normalized
    return f"openai/{normalized}"


async def _restore_storage_state(
    *,
    context: BrowserContext,
    page: Page,
    storage_state_path: Path,
) -> None:
    if not storage_state_path.exists():
        return
    payload = json.loads(storage_state_path.read_text(encoding="utf-8"))
    cookies = payload.get("cookies")
    if isinstance(cookies, list) and cookies:
        await context.add_cookies(cookies)
    origins = payload.get("origins")
    if not isinstance(origins, list):
        return
    for origin_entry in origins:
        if not isinstance(origin_entry, dict):
            continue
        origin = origin_entry.get("origin")
        local_storage = origin_entry.get("localStorage")
        if not isinstance(origin, str) or not isinstance(local_storage, list) or not local_storage:
            continue
        try:
            await page.goto(origin, wait_until="domcontentloaded")
            await page.evaluate(
                """
                (items) => {
                  for (const item of items) {
                    if (!item || typeof item.name !== "string") {
                      continue;
                    }
                    localStorage.setItem(item.name, String(item.value ?? ""));
                  }
                }
                """,
                local_storage,
            )
        except Exception:  # noqa: BLE001
            logger.debug("stagehand_storage_state_origin_restore_failed", exc_info=True)


def _resolve_cdp_context(contexts: Sequence[BrowserContext]) -> BrowserContext:
    if contexts:
        return contexts[0]
    msg = "Stagehand browser did not expose any Playwright browser context."
    raise StagehandLinkedInError(msg)


async def _resolve_cdp_page(context: BrowserContext) -> Page:
    if context.pages:
        return context.pages[0]
    return await context.new_page()


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _clean_text(value: object) -> str | None:
    collapsed = " ".join(str(value or "").split()).strip()
    return collapsed or None


def _clean_card_label(value: object) -> str | None:
    collapsed = _clean_text(value)
    if collapsed is None:
        return None
    normalized = re.sub(r"\s+with verification$", "", collapsed, flags=re.IGNORECASE).strip()
    parts = normalized.split(" ")
    if len(parts) >= 4 and len(parts) % 2 == 0:
        midpoint = len(parts) // 2
        if parts[:midpoint] == parts[midpoint:]:
            normalized = " ".join(parts[:midpoint])
    return normalized or None


def _stringify_path(value: Path | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _normalize_linkedin_job_url(value: object) -> str | None:
    raw = _clean_text(value)
    if raw is None or "/jobs/view/" not in raw:
        return None
    return raw.split("?", maxsplit=1)[0]


@contextmanager
def _stagehand_environment(cache_dir: Path) -> Iterator[None]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    original_xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(cache_dir)
    try:
        yield
    finally:
        if original_xdg_cache_home is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = original_xdg_cache_home
