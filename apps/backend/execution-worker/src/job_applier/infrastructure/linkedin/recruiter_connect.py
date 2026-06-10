"""Recruiter identification, message generation, and connect attempts."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib import error, request
from uuid import UUID

from playwright.async_api import BrowserContext, Page

from job_applier.application.config import UserAgentSettings
from job_applier.cost_observability import record_openai_usage
from job_applier.domain.entities import JobPosting, RecruiterInteraction, utc_now
from job_applier.domain.enums import RecruiterAction, RecruiterInteractionStatus
from job_applier.infrastructure.linkedin.question_resolution import normalize_text
from job_applier.recruiter_connect_observability import record_recruiter_connect_observation
from job_applier.settings import RuntimeSettings

logger = logging.getLogger(__name__)

RECRUITER_MESSAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "message": {
            "type": "string",
            "description": "One LinkedIn connection note under 300 characters.",
        },
    },
    "required": ["message"],
}


@dataclass(frozen=True, slots=True)
class RecruiterCandidate:
    """One recruiter-like profile identified on the LinkedIn job page."""

    name: str
    profile_url: str
    context_label: str | None = None


@dataclass(frozen=True, slots=True)
class RecruiterConnectAttempt:
    """Result of the recruiter connect stage for one submission."""

    interaction: RecruiterInteraction
    screenshot_path: Path | None = None
    connect_path: str | None = None
    send_action: str | None = None
    success_signal: str | None = None
    result_reason: str | None = None
    message_source: str | None = None
    note_mode: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedRecruiterMessage:
    """Prepared recruiter note with provenance metadata."""

    message: str
    source: str


class RecruiterMessageAI(Protocol):
    """Protocol used by the recruiter message generator for AI fallback."""

    async def generate(
        self,
        *,
        recruiter: RecruiterCandidate,
        posting: JobPosting,
        settings: UserAgentSettings,
    ) -> str | None:
        """Return a short contextual recruiter note or `None`."""


class LinkedInRecruiterCandidateFinder:
    """Find a recruiter or hiring manager candidate on the job page."""

    async def find(
        self,
        page: Page,
        settings: UserAgentSettings,
    ) -> RecruiterCandidate | None:
        """Return one recruiter candidate when the toggle is enabled."""

        if not settings.agent.auto_connect_with_recruiter:
            logger.info("linkedin_recruiter_connect_skipped", extra={"reason": "toggle_disabled"})
            return None

        payload = await page.evaluate(
            """
            () => {
              const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]'));
              const items = [];

              for (const anchor of anchors) {
                const name = collapse(anchor.innerText || anchor.textContent || "");
                const profileUrl = anchor.href || "";
                const container = anchor.closest("section, article, li, div, aside");
                const context = collapse(container?.innerText || "");
                if (!name || !profileUrl || profileUrl.includes("/jobs/")) {
                  continue;
                }
                items.push({
                  name,
                  profile_url: profileUrl,
                  context_label: context,
                });
              }

              return items.slice(0, 30);
            }
            """,
        )

        raw_candidates = payload if isinstance(payload, list) else []
        candidates = tuple(
            RecruiterCandidate(
                name=str(item.get("name") or "").strip(),
                profile_url=str(item.get("profile_url") or "").strip(),
                context_label=str(item.get("context_label") or "").strip() or None,
            )
            for item in raw_candidates
            if isinstance(item, dict)
            and str(item.get("name") or "").strip()
            and str(item.get("profile_url") or "").strip()
        )
        candidate = select_recruiter_candidate(candidates)
        if candidate is None:
            logger.info(
                "linkedin_recruiter_connect_skipped",
                extra={"reason": "candidate_not_found"},
            )
            return None
        return candidate


class OpenAIResponsesRecruiterMessageGenerator:
    """Use the OpenAI Responses API to draft a short recruiter note."""

    endpoint = "https://api.openai.com/v1/responses"

    async def generate(
        self,
        *,
        recruiter: RecruiterCandidate,
        posting: JobPosting,
        settings: UserAgentSettings,
    ) -> str | None:
        """Return an AI-generated recruiter note when an API key is configured."""

        if settings.ai.api_key is None:
            return None

        prompt_payload: dict[str, object] = {
            "recruiter_name": recruiter.name,
            "company_name": posting.company_name,
            "job_title": posting.title,
            "candidate_name": settings.profile.name,
            "constraints": [
                "under 300 characters",
                "professional tone",
                "mention the role and company",
                "no bullet points",
                "plain text only",
            ],
        }
        logger.info(
            "linkedin_recruiter_message_prompt",
            extra={"model": settings.ai.model, "prompt_payload": prompt_payload},
        )

        try:
            response_data = await asyncio.to_thread(
                self._create_response,
                api_key=settings.ai.api_key.get_secret_value(),
                model=settings.ai.model,
                prompt_payload=prompt_payload,
            )
        except Exception:  # noqa: BLE001
            logger.exception("linkedin_recruiter_message_failed")
            return None

        raw_output = self._extract_output_text(response_data)
        logger.info(
            "linkedin_recruiter_message_response",
            extra={"model": settings.ai.model, "response_text": raw_output},
        )
        if not raw_output:
            return None

        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError:
            return None

        message = str(payload.get("message") or "").strip()
        return finalize_recruiter_message(message)

    def _create_response(
        self,
        *,
        api_key: str,
        model: str,
        prompt_payload: dict[str, object],
    ) -> dict[str, object]:
        body = {
            "model": model,
            "input": [
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Write one concise LinkedIn connection note. "
                                "Keep it warm, professional, and under 300 characters."
                            ),
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(prompt_payload, ensure_ascii=True),
                        },
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "recruiter_connect_message",
                    "schema": RECRUITER_MESSAGE_SCHEMA,
                    "strict": True,
                },
            },
        }
        payload_bytes = json.dumps(body, ensure_ascii=True).encode("utf-8")
        http_request = request.Request(
            self.endpoint,
            data=payload_bytes,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started_at = time.perf_counter()
        try:
            with request.urlopen(http_request, timeout=30) as response:  # noqa: S310
                parsed = cast(dict[str, object], json.loads(response.read().decode("utf-8")))
                record_openai_usage(
                    category="openai.recruiter_connect.message",
                    model=model,
                    latency_ms=int((time.perf_counter() - started_at) * 1000),
                    response_payload=parsed,
                )
                return parsed
        except error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            record_openai_usage(
                category="openai.recruiter_connect.message",
                model=model,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                status="rate_limited" if exc.code == 429 else "http_error",
                error_status=exc.code,
                error_message=error_body[:300],
            )
            logger.warning(
                "openai_recruiter_message_http_error",
                extra={"status": exc.code, "body": error_body},
            )
            raise

    def _extract_output_text(self, response_data: dict[str, object]) -> str:
        direct_output = response_data.get("output_text")
        if isinstance(direct_output, str):
            return direct_output.strip()

        output_items = response_data.get("output", ())
        if not isinstance(output_items, list):
            return ""

        parts: list[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            content_items = item.get("content", ())
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part.strip()).strip()


class RecruiterMessageGenerator:
    """Generate a recruiter note with AI first and a deterministic fallback."""

    def __init__(self, ai_generator: RecruiterMessageAI | None = None) -> None:
        self._ai_generator = ai_generator or OpenAIResponsesRecruiterMessageGenerator()

    async def generate(
        self,
        *,
        recruiter: RecruiterCandidate,
        posting: JobPosting,
        settings: UserAgentSettings,
    ) -> GeneratedRecruiterMessage:
        """Return a short recruiter note suitable for LinkedIn connect."""

        ai_message = await self._generate_ai_message(
            recruiter=recruiter,
            posting=posting,
            settings=settings,
        )
        if ai_message:
            return GeneratedRecruiterMessage(message=ai_message, source="ai")
        return GeneratedRecruiterMessage(
            message=build_recruiter_message_template(
                recruiter_name=recruiter.name,
                company_name=posting.company_name,
                job_title=posting.title,
                candidate_name=settings.profile.name,
            ),
            source="template",
        )

    async def _generate_ai_message(
        self,
        *,
        recruiter: RecruiterCandidate,
        posting: JobPosting,
        settings: UserAgentSettings,
    ) -> str | None:
        try:
            return await self._ai_generator.generate(
                recruiter=recruiter,
                posting=posting,
                settings=settings,
            )
        except Exception:  # noqa: BLE001
            logger.exception("linkedin_recruiter_message_unhandled_error")
            return None


class PlaywrightRecruiterConnector:
    """Attempt the recruiter connection inside the authenticated LinkedIn session."""

    def __init__(
        self,
        runtime_settings: RuntimeSettings,
        *,
        message_generator: RecruiterMessageGenerator | None = None,
    ) -> None:
        self._runtime_settings = runtime_settings
        self._message_generator = message_generator or RecruiterMessageGenerator()

    async def connect(
        self,
        context: BrowserContext,
        *,
        recruiter: RecruiterCandidate,
        settings: UserAgentSettings,
        posting: JobPosting,
        submission_id: UUID,
        screenshot_path: Path,
    ) -> RecruiterConnectAttempt:
        """Try to send a LinkedIn connection request and return the result."""

        message: str | None = None
        message_source: str | None = None
        page = await context.new_page()
        page.set_default_timeout(self._runtime_settings.linkedin_default_timeout_ms)

        try:
            await page.goto(recruiter.profile_url, wait_until="domcontentloaded")
            record_recruiter_connect_observation(
                counters=("candidate_detected", "attempted", "profile_opened"),
                recruiter_name=recruiter.name,
                recruiter_profile_url=recruiter.profile_url,
                timeline_event="recruiter_connect_profile_opened",
            )

            existing_status, existing_signal = await self._detect_existing_status(page)
            if existing_status is not None:
                record_recruiter_connect_observation(
                    status=existing_status.value,
                    reason="existing_status",
                    success_signal=existing_signal,
                    recruiter_name=recruiter.name,
                    recruiter_profile_url=recruiter.profile_url,
                    timeline_event="recruiter_connect_existing_status_detected",
                )
                await page.screenshot(path=str(screenshot_path), full_page=True)
                return RecruiterConnectAttempt(
                    interaction=RecruiterInteraction(
                        submission_id=submission_id,
                        recruiter_name=recruiter.name,
                        recruiter_profile_url=recruiter.profile_url,
                        action=RecruiterAction.CONNECT,
                        status=existing_status,
                        message_sent=None,
                    ),
                    screenshot_path=screenshot_path,
                    success_signal=existing_signal,
                    result_reason="existing_status",
                )

            connect_path = await self._open_connect_flow(page, recruiter=recruiter)
            if connect_path is None:
                record_recruiter_connect_observation(
                    status=RecruiterInteractionStatus.SKIPPED.value,
                    reason="connect_unavailable",
                    recruiter_name=recruiter.name,
                    recruiter_profile_url=recruiter.profile_url,
                    timeline_event="recruiter_connect_unavailable",
                )
                await page.screenshot(path=str(screenshot_path), full_page=True)
                return RecruiterConnectAttempt(
                    interaction=RecruiterInteraction(
                        submission_id=submission_id,
                        recruiter_name=recruiter.name,
                        recruiter_profile_url=recruiter.profile_url,
                        action=RecruiterAction.CONNECT,
                        status=RecruiterInteractionStatus.SKIPPED,
                        message_sent=None,
                    ),
                    screenshot_path=screenshot_path,
                    result_reason="connect_unavailable",
                )
            record_recruiter_connect_observation(
                connect_path=connect_path,
                recruiter_name=recruiter.name,
                recruiter_profile_url=recruiter.profile_url,
                timeline_event="recruiter_connect_action_opened",
            )

            prepared_message = await self._message_generator.generate(
                recruiter=recruiter,
                posting=posting,
                settings=settings,
            )
            message = prepared_message.message
            message_source = prepared_message.source
            record_recruiter_connect_observation(
                recruiter_name=recruiter.name,
                recruiter_profile_url=recruiter.profile_url,
                message_source=message_source,
                timeline_event="recruiter_connect_message_prepared",
            )

            note_mode = await self._add_note_if_available(page, message)
            if note_mode is not None:
                record_recruiter_connect_observation(
                    note_mode=note_mode,
                    recruiter_name=recruiter.name,
                    recruiter_profile_url=recruiter.profile_url,
                    timeline_event="recruiter_connect_note_prepared",
                )
            send_action = await self._send_connection_request(page)
            if send_action is not None:
                record_recruiter_connect_observation(
                    send_action=send_action,
                    recruiter_name=recruiter.name,
                    recruiter_profile_url=recruiter.profile_url,
                    timeline_event="recruiter_connect_submit_triggered",
                )
            success, success_signal = await self._await_connection_success(page)
            await page.screenshot(path=str(screenshot_path), full_page=True)
            final_status = (
                RecruiterInteractionStatus.SENT if success else RecruiterInteractionStatus.FAILED
            )
            final_reason = None if success else "success_not_observed"
            record_recruiter_connect_observation(
                status=final_status.value,
                reason=final_reason,
                connect_path=connect_path,
                send_action=send_action,
                success_signal=success_signal,
                recruiter_name=recruiter.name,
                recruiter_profile_url=recruiter.profile_url,
                note_mode=note_mode,
                timeline_event="recruiter_connect_result",
            )

            return RecruiterConnectAttempt(
                interaction=RecruiterInteraction(
                    submission_id=submission_id,
                    recruiter_name=recruiter.name,
                    recruiter_profile_url=recruiter.profile_url,
                    action=RecruiterAction.CONNECT,
                    status=final_status,
                    message_sent=message,
                    sent_at=utc_now() if success else None,
                ),
                screenshot_path=screenshot_path,
                connect_path=connect_path,
                send_action=send_action,
                success_signal=success_signal,
                result_reason=final_reason,
                message_source=message_source,
                note_mode=note_mode,
            )
        finally:
            await page.close()

    async def _existing_status(self, page: Page) -> RecruiterInteractionStatus | None:
        status, _signal = await self._detect_existing_status(page)
        return status

    async def _detect_existing_status(
        self,
        page: Page,
    ) -> tuple[RecruiterInteractionStatus | None, str | None]:
        for pattern in (r"pending", r"invitation sent"):
            locator = page.get_by_role("button", name=re.compile(pattern, re.I))
            if await locator.count():
                return RecruiterInteractionStatus.SKIPPED, f"button:{pattern}"
            text = page.get_by_text(re.compile(pattern, re.I))
            if await text.count():
                return RecruiterInteractionStatus.SKIPPED, f"text:{pattern}"
        return None, None

    async def _open_connect_flow(self, page: Page, *, recruiter: RecruiterCandidate) -> str | None:
        recruiter_name = re.escape(recruiter.name.split("•", 1)[0].strip())
        direct_connect = page.get_by_role(
            "button",
            name=re.compile(rf"invite\s+{recruiter_name}\s+to\s+connect", re.I),
        )
        if await direct_connect.count():
            await direct_connect.first.click()
            return "direct_button"

        more_button = page.get_by_role("button", name=re.compile(r"more", re.I))
        if await more_button.count():
            await more_button.first.click()
            await page.wait_for_timeout(300)
            menu_connect = page.get_by_role(
                "menuitem",
                name=re.compile(r"connect", re.I),
            )
            if await menu_connect.count():
                await menu_connect.first.click()
                return "more_menuitem"
            alt_connect = page.get_by_role("button", name=re.compile(r"connect", re.I))
            if await alt_connect.count():
                await alt_connect.first.click()
                return "more_button"
        return None

    async def _add_note_if_available(self, page: Page, message: str) -> str | None:
        dialog = page.get_by_role("dialog")
        add_note = dialog.get_by_role("button", name=re.compile(r"add a note", re.I))
        if await add_note.count():
            await add_note.first.click()
            await page.wait_for_timeout(300)
            note_mode = "add_note_clicked"
        else:
            note_mode = "no_add_note_button"

        textarea = dialog.locator("textarea")
        if await textarea.count():
            await textarea.first.fill(message)
            return "note_filled"
        return note_mode

    async def _send_connection_request(self, page: Page) -> str | None:
        dialog = page.get_by_role("dialog")
        send_without_note = dialog.get_by_role(
            "button",
            name=re.compile(r"send without a note", re.I),
        )
        if await send_without_note.count():
            await send_without_note.first.click()
            return "send_without_note"

        send_button = dialog.get_by_role("button", name=re.compile(r"send", re.I))
        if await send_button.count():
            await send_button.first.click()
            return "send_button"

        modal_connect = dialog.get_by_role("button", name=re.compile(r"connect", re.I))
        if await modal_connect.count():
            await modal_connect.first.click()
            return "dialog_connect"
        return None

    async def _await_connection_success(self, page: Page) -> tuple[bool, str | None]:
        for _ in range(20):
            status, signal = await self._detect_existing_status(page)
            if status is RecruiterInteractionStatus.SKIPPED:
                return True, signal
            success_text = page.get_by_text(re.compile(r"invitation sent|pending", re.I))
            if await success_text.count():
                return True, "success_text"
            await page.wait_for_timeout(500)
        return False, "timeout"


def build_recruiter_message_template(
    *,
    recruiter_name: str,
    company_name: str,
    job_title: str,
    candidate_name: str,
) -> str:
    """Return the deterministic recruiter-connect fallback note."""

    first_name = recruiter_name.strip().split()[0] if recruiter_name.strip() else "there"
    candidate_first_name = (
        candidate_name.strip().split()[0] if candidate_name.strip() else candidate_name.strip()
    )
    message = (
        f"Hi {first_name}, I just applied for the {job_title} role at {company_name} "
        f"and would love to connect. Thanks, {candidate_first_name}."
    )
    return finalize_recruiter_message(message)


def finalize_recruiter_message(message: str) -> str:
    """Normalize whitespace and keep the note within LinkedIn's short limit."""

    compact = re.sub(r"\s+", " ", message).strip()
    if len(compact) <= 300:
        return compact
    trimmed = compact[:297].rstrip(" ,.;:-")
    return f"{trimmed}..."


def select_recruiter_candidate(
    candidates: tuple[RecruiterCandidate, ...],
) -> RecruiterCandidate | None:
    """Pick the most relevant recruiter candidate from the extracted anchors."""

    best_candidate: RecruiterCandidate | None = None
    best_score = -1

    for candidate in candidates:
        context = normalize_text(candidate.context_label or "")
        score = 0

        if not candidate.profile_url.startswith("http"):
            continue
        if any(
            term in normalize_text(candidate.name) for term in ("people also viewed", "followers")
        ):
            continue
        if "who posted this job" in context or "posted by" in context:
            score += 100
        if "hiring team" in context:
            score += 80
        if any(term in context for term in ("recruiter", "talent acquisition", "human resources")):
            score += 60
        if "hiring manager" in context:
            score += 50
        if "/in/" in candidate.profile_url:
            score += 10

        if score > best_score:
            best_score = score
            best_candidate = candidate

    if best_score <= 0:
        return None
    return best_candidate
