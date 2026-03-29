"""LLM-guided browser control for volatile LinkedIn screens."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast
from urllib import error, request

from playwright.async_api import Locator, Page
from pydantic import SecretStr

logger = logging.getLogger(__name__)

BrowserActionType = Literal["click", "fill", "wait", "done", "fail"]
BrowserValueSource = str
PageRequiresLogin = Callable[[Page], Awaitable[bool]]
PageTaskComplete = Callable[[Page], Awaitable[bool]]
BrowserAssessmentStatus = Literal[
    "complete",
    "blocked",
    "pending",
    "manual_intervention",
    "unknown",
]

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action_type": {
            "type": "string",
            "enum": ["click", "fill", "wait", "done", "fail"],
        },
        "element_id": {
            "type": ["string", "null"],
            "description": "The agent element id for click/fill actions.",
        },
        "value_source": {
            "type": ["string", "null"],
            "description": (
                "Use literal for plain text or one of the task-specific value sources "
                "described in the prompt."
            ),
        },
        "value": {
            "type": ["string", "null"],
            "description": "Literal text to fill when value_source is literal.",
        },
        "action_intent": {
            "type": ["string", "null"],
            "description": "Short machine-readable intent for the action, when relevant.",
        },
        "wait_seconds": {
            "type": "integer",
            "minimum": 0,
            "maximum": 20,
        },
        "reasoning": {
            "type": "string",
            "description": "Short explanation of why this is the next action.",
        },
    },
    "required": [
        "action_type",
        "element_id",
        "value_source",
        "value",
        "action_intent",
        "wait_seconds",
        "reasoning",
    ],
}

STRUCTURED_ASSESSMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["complete", "blocked", "pending", "manual_intervention", "unknown"],
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "summary": {
            "type": "string",
            "description": "Short human-readable summary of the current browser state.",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Short signals visible in the page snapshot that support the assessment."
            ),
        },
    },
    "required": ["status", "confidence", "summary", "evidence"],
}

INTERACTIVE_SELECTORS = ",".join(
    (
        "input",
        "textarea",
        "select",
        "button",
        "a[href]",
        "[role='button']",
        "[role='link']",
        "[role='textbox']",
        "[role='combobox']",
        "[role='checkbox']",
        "[role='radio']",
        "[contenteditable='true']",
    ),
)

MANUAL_INTERVENTION_PATTERNS = (
    "captcha",
    "security verification",
    "verify your identity",
    "verify it's you",
    "enter the code",
    "check your email",
    "check your phone",
    "two-step verification",
    "one more step",
)


class BrowserAutomationError(RuntimeError):
    """Raised when the browser agent cannot safely continue."""


@dataclass(frozen=True, slots=True)
class BrowserAgentElement:
    """One visible interactive element from the current page."""

    element_id: str
    tag: str
    role: str | None = None
    label: str | None = None
    text: str | None = None
    placeholder: str | None = None
    name: str | None = None
    input_type: str | None = None
    href: str | None = None
    current_value: str | None = None
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class BrowserAgentSnapshot:
    """Compact page snapshot sent to the browser-planning model."""

    url: str
    title: str
    visible_text: str
    elements: tuple[BrowserAgentElement, ...]


@dataclass(frozen=True, slots=True)
class BrowserAgentAction:
    """One planned browser action returned by the model."""

    action_type: BrowserActionType
    element_id: str | None
    value_source: BrowserValueSource | None
    value: str | None
    action_intent: str | None
    wait_seconds: int
    reasoning: str


@dataclass(frozen=True, slots=True)
class BrowserTaskAssessment:
    """Structured interpretation of the current browser state."""

    status: BrowserAssessmentStatus
    confidence: float
    summary: str
    evidence: tuple[str, ...] = ()


def collapse_text(value: str | None) -> str:
    """Collapse repeated whitespace and trim empty values."""

    return re.sub(r"\s+", " ", value or "").strip()


def truncate_text(value: str, *, limit: int) -> str:
    """Trim long text blocks to keep prompts small and relevant."""

    collapsed = collapse_text(value)
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1].rstrip()}…"


def has_manual_intervention_cues(snapshot: BrowserAgentSnapshot) -> bool:
    """Return whether the page looks like a captcha or verification checkpoint."""

    haystack = " ".join((snapshot.url, snapshot.title, snapshot.visible_text)).lower()
    return any(pattern in haystack for pattern in MANUAL_INTERVENTION_PATTERNS)


class BrowserDomSnapshotter:
    """Capture a compact, selector-free snapshot of visible interactive controls."""

    def __init__(self, *, max_elements: int = 24, max_visible_text: int = 1_600) -> None:
        self._max_elements = max_elements
        self._max_visible_text = max_visible_text

    async def capture(self, page: Page) -> BrowserAgentSnapshot:
        """Return the cleaned page snapshot used by the planner."""

        title = await page.title()
        payload = await page.evaluate(
            """
            ({ interactiveSelectors, maxElements }) => {
              const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const isVisible = (node) => {
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return (
                  style.visibility !== "hidden" &&
                  style.display !== "none" &&
                  rect.width > 0 &&
                  rect.height > 0
                );
              };
              const labelFor = (node) => {
                const labels = Array.from(node.labels || []);
                const joined = labels
                  .map((item) => collapse(item.innerText || item.textContent || ""))
                  .filter(Boolean)
                  .join(" ");
                if (joined) {
                  return joined;
                }
                const id = node.getAttribute("id");
                if (!id) {
                  return "";
                }
                const label = document.querySelector(`label[for="${id}"]`);
                return collapse(label?.innerText || label?.textContent || "");
              };
              const nodes = Array.from(document.querySelectorAll(interactiveSelectors));
              const items = [];
              let counter = 1;

              for (const node of nodes) {
                if (!isVisible(node)) {
                  continue;
                }
                const elementId = `agent-${counter}`;
                counter += 1;
                node.setAttribute("data-job-applier-agent-id", elementId);
                items.push({
                  element_id: elementId,
                  tag: node.tagName.toLowerCase(),
                  role: collapse(node.getAttribute("role")),
                  label: collapse(node.getAttribute("aria-label")) || labelFor(node),
                  text: collapse(node.innerText || node.textContent || ""),
                  placeholder: collapse(node.getAttribute("placeholder")),
                  name: collapse(node.getAttribute("name")),
                  input_type: collapse(node.getAttribute("type")),
                  href: collapse(node.getAttribute("href")),
                  current_value: collapse(node.value),
                  disabled: Boolean(node.disabled || node.getAttribute("aria-disabled") === "true"),
                });
                if (items.length >= maxElements) {
                  break;
                }
              }

              const visibleText = collapse(document.body?.innerText || "");
              return {
                visible_text: visibleText,
                elements: items,
              };
            }
            """,
            {
                "interactiveSelectors": INTERACTIVE_SELECTORS,
                "maxElements": self._max_elements,
            },
        )
        raw_payload = cast(dict[str, object], payload)
        raw_elements_payload = raw_payload.get("elements", ())
        raw_elements = raw_elements_payload if isinstance(raw_elements_payload, list) else []
        elements = tuple(
            BrowserAgentElement(
                element_id=str(item.get("element_id") or "").strip(),
                tag=str(item.get("tag") or "").strip(),
                role=_optional_text(item.get("role")),
                label=_optional_text(item.get("label")),
                text=_optional_text(item.get("text")),
                placeholder=_optional_text(item.get("placeholder")),
                name=_optional_text(item.get("name")),
                input_type=_optional_text(item.get("input_type")),
                href=_optional_text(item.get("href")),
                current_value=_optional_text(item.get("current_value")),
                disabled=bool(item.get("disabled")),
            )
            for item in raw_elements
            if isinstance(item, dict) and str(item.get("element_id") or "").strip()
        )
        return BrowserAgentSnapshot(
            url=page.url,
            title=truncate_text(title, limit=160),
            visible_text=truncate_text(
                str(raw_payload.get("visible_text") or ""),
                limit=self._max_visible_text,
            ),
            elements=elements,
        )


class OpenAIResponsesBrowserAgent:
    """Plan and execute browser actions with compact page snapshots."""

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        max_steps: int = 18,
        snapshotter: BrowserDomSnapshotter | None = None,
        min_action_delay_ms: int = 350,
        max_action_delay_ms: int = 950,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_steps = max_steps
        self._snapshotter = snapshotter or BrowserDomSnapshotter()
        self._min_action_delay_ms = max(0, min_action_delay_ms)
        self._max_action_delay_ms = max(self._min_action_delay_ms, max_action_delay_ms)

    async def complete_linkedin_login(
        self,
        *,
        page: Page,
        credentials: dict[BrowserValueSource, str],
        page_requires_login: PageRequiresLogin,
        timeout_seconds: int,
    ) -> None:
        """Drive the LinkedIn login flow until the session becomes authenticated."""

        async def login_complete(candidate_page: Page) -> bool:
            return not await page_requires_login(candidate_page)

        await self.complete_browser_task(
            page=page,
            available_values=credentials,
            goal="Authenticate to LinkedIn so the automation can search and apply for jobs.",
            timeout_seconds=timeout_seconds,
            task_name="linkedin_login",
            is_complete=login_complete,
            extra_rules=(
                (
                    "Use linkedin_email and linkedin_password for credential fields. "
                    "Never ask for raw secrets."
                ),
                (
                    "If the password was already entered and a sign-in action is visible, "
                    "prefer clicking submit."
                ),
                (
                    "Do not declare done while the page still looks like a login, "
                    "challenge, or checkpoint screen."
                ),
                (
                    "If the screen suggests captcha, email verification, OTP, or human "
                    "checkpoint, choose wait."
                ),
            ),
        )

    async def complete_browser_task(
        self,
        *,
        page: Page,
        available_values: Mapping[BrowserValueSource, str],
        goal: str,
        timeout_seconds: int,
        task_name: str,
        is_complete: PageTaskComplete,
        extra_rules: Sequence[str] = (),
        allowed_action_types: Sequence[BrowserActionType] | None = None,
    ) -> None:
        """Drive one volatile browser task until its completion predicate succeeds."""

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        recent_actions: list[dict[str, object]] = []
        previous_snapshot_signature = ""

        for step_index in range(self._max_steps):
            if await is_complete(page):
                return

            snapshot = await self._snapshotter.capture(page)
            snapshot_signature = f"{snapshot.url}|{snapshot.title}|{snapshot.visible_text}"
            snapshot_changed = snapshot_signature != previous_snapshot_signature
            if has_manual_intervention_cues(snapshot):
                if asyncio.get_running_loop().time() >= deadline:
                    break
                logger.info(
                    "linkedin_browser_agent_waiting_for_manual_intervention",
                    extra={"step_index": step_index, "task_name": task_name, "url": snapshot.url},
                )
                await page.wait_for_timeout(5_000)
                previous_snapshot_signature = snapshot_signature
                continue

            remaining_seconds = max(1.0, deadline - asyncio.get_running_loop().time())
            action = await asyncio.wait_for(
                self._plan_action(
                    snapshot=snapshot,
                    goal=goal,
                    task_name=task_name,
                    available_values=available_values,
                    step_index=step_index,
                    recent_actions=recent_actions[-6:],
                    snapshot_changed=snapshot_changed,
                    extra_rules=extra_rules,
                    allowed_action_types=allowed_action_types,
                ),
                timeout=remaining_seconds,
            )
            logger.info(
                "linkedin_browser_agent_action_planned",
                extra={
                    "step_index": step_index,
                    "task_name": task_name,
                    "action_type": action.action_type,
                    "action_intent": action.action_intent,
                    "element_id": action.element_id,
                    "value_source": action.value_source,
                    "reasoning": action.reasoning,
                    "url": snapshot.url,
                },
            )
            await self._execute_action(page=page, action=action, values=available_values)
            recent_actions.append(
                {
                    "step_index": step_index,
                    "task_name": task_name,
                    "action_type": action.action_type,
                    "element_id": action.element_id,
                    "value_source": action.value_source,
                    "reasoning": action.reasoning,
                    "url": snapshot.url,
                    "snapshot_changed": snapshot_changed,
                }
            )
            previous_snapshot_signature = snapshot_signature

        if await is_complete(page):
            return
        msg = f"Browser agent exhausted the {task_name} task before completion."
        raise BrowserAutomationError(msg)

    async def assess_browser_task(
        self,
        *,
        page: Page,
        goal: str,
        task_name: str,
        extra_rules: Sequence[str] = (),
        recent_actions: Sequence[dict[str, object]] = (),
        step_index: int = 0,
    ) -> BrowserTaskAssessment:
        """Return a structured assessment of the current browser state."""

        snapshot = await self._snapshotter.capture(page)
        if has_manual_intervention_cues(snapshot):
            return BrowserTaskAssessment(
                status="manual_intervention",
                confidence=0.99,
                summary=(
                    "The page looks like a verification or checkpoint screen that needs a human."
                ),
                evidence=("manual_intervention_cue_detected", snapshot.title or snapshot.url),
            )
        response_data = await asyncio.to_thread(
            self._create_assessment_response,
            snapshot,
            goal,
            task_name,
            step_index,
            recent_actions,
            extra_rules,
        )
        raw_output = self._extract_output_text(response_data)
        logger.info(
            "linkedin_browser_agent_assessment_response",
            extra={"model": self._model, "task_name": task_name, "response_text": raw_output},
        )
        if not raw_output:
            msg = "Browser agent returned an empty assessment."
            raise BrowserAutomationError(msg)
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            msg = "Browser agent returned invalid JSON for the browser assessment."
            raise BrowserAutomationError(msg) from exc
        return parse_browser_task_assessment(payload)

    async def perform_single_task_action(
        self,
        *,
        page: Page,
        available_values: Mapping[BrowserValueSource, str],
        goal: str,
        task_name: str,
        extra_rules: Sequence[str] = (),
        allowed_action_types: Sequence[BrowserActionType] | None = None,
        recent_actions: Sequence[dict[str, object]] = (),
        step_index: int = 0,
    ) -> BrowserAgentAction:
        """Plan and execute one browser action for the current page state."""

        snapshot = await self._snapshotter.capture(page)
        action = await self._plan_action(
            snapshot=snapshot,
            goal=goal,
            task_name=task_name,
            available_values=available_values,
            step_index=step_index,
            recent_actions=recent_actions,
            snapshot_changed=True,
            extra_rules=extra_rules,
            allowed_action_types=allowed_action_types,
        )
        logger.info(
            "linkedin_browser_agent_single_action_planned",
            extra={
                "task_name": task_name,
                "step_index": step_index,
                "action_type": action.action_type,
                "action_intent": action.action_intent,
                "element_id": action.element_id,
                "value_source": action.value_source,
                "reasoning": action.reasoning,
                "url": snapshot.url,
            },
        )
        await self._execute_action(page=page, action=action, values=available_values)
        return action

    async def _plan_action(
        self,
        *,
        snapshot: BrowserAgentSnapshot,
        goal: str,
        task_name: str,
        available_values: Mapping[BrowserValueSource, str],
        step_index: int,
        recent_actions: Sequence[dict[str, object]],
        snapshot_changed: bool,
        extra_rules: Sequence[str],
        allowed_action_types: Sequence[BrowserActionType] | None,
    ) -> BrowserAgentAction:
        response_data = await asyncio.to_thread(
            self._create_response,
            snapshot,
            goal,
            task_name,
            available_values,
            step_index,
            recent_actions,
            snapshot_changed,
            extra_rules,
        )
        raw_output = self._extract_output_text(response_data)
        logger.info(
            "linkedin_browser_agent_response",
            extra={"model": self._model, "task_name": task_name, "response_text": raw_output},
        )
        if not raw_output:
            msg = "Browser agent returned an empty response."
            raise BrowserAutomationError(msg)
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            msg = "Browser agent returned invalid JSON."
            raise BrowserAutomationError(msg) from exc
        action = parse_browser_action(payload)
        self._validate_action_against_snapshot(
            action,
            snapshot=snapshot,
            available_values=available_values,
            allowed_action_types=allowed_action_types,
        )
        return action

    def _create_response(
        self,
        snapshot: BrowserAgentSnapshot,
        goal: str,
        task_name: str,
        available_values: Mapping[BrowserValueSource, str],
        step_index: int,
        recent_actions: Sequence[dict[str, object]],
        snapshot_changed: bool,
        extra_rules: Sequence[str],
    ) -> dict[str, object]:
        elements_payload = [
            {
                "element_id": element.element_id,
                "tag": element.tag,
                "role": element.role,
                "label": element.label,
                "text": truncate_text(element.text or "", limit=120) or None,
                "placeholder": element.placeholder,
                "name": element.name,
                "input_type": element.input_type,
                "href": element.href,
                "current_value": truncate_text(element.current_value or "", limit=80) or None,
                "disabled": element.disabled,
            }
            for element in snapshot.elements
        ]
        prompt_payload = {
            "goal": goal,
            "task_name": task_name,
            "step_index": step_index,
            "snapshot_changed_since_last_step": snapshot_changed,
            "page": {
                "url": snapshot.url,
                "title": snapshot.title,
                "visible_text": snapshot.visible_text,
                "elements": elements_payload,
            },
            "available_value_sources": {
                key: (f"Use the task-owned value source {key}. Never ask for the raw value.")
                for key in available_values
                if key != "literal"
            },
            "recent_action_history": list(recent_actions),
            "rules": [
                "Return exactly one next action.",
                "Only reference element ids that exist in the page snapshot.",
                "Use literal for plain text that is not sensitive.",
                (
                    "Use wait when the page is loading, animating, "
                    "or a human may need to solve verification."
                ),
                (
                    "Use recent_action_history to avoid repeating the same failed move "
                    "on an unchanged screen."
                ),
                (
                    "Set action_intent to a short snake_case phrase when it helps "
                    "the caller interpret the action."
                ),
                "Use done only when the task goal appears complete.",
                "Use fail only when no safe next action exists.",
                *extra_rules,
            ],
        }
        logger.info(
            "linkedin_browser_agent_prompt",
            extra={"model": self._model, "task_name": task_name, "prompt_payload": prompt_payload},
        )

        body = {
            "model": self._model,
            "input": [
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "You are controlling a browser for a LinkedIn task. "
                                "Pick the safest next action based on the current page snapshot, "
                                "the current goal, and the recent action history."
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
                    "name": "browser_agent_action",
                    "schema": STRUCTURED_OUTPUT_SCHEMA,
                    "strict": True,
                },
            },
        }
        payload_bytes = json.dumps(body, ensure_ascii=True).encode("utf-8")
        http_request = request.Request(
            self.endpoint,
            data=payload_bytes,
            headers={
                "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=30) as response:  # noqa: S310
                return cast(dict[str, object], json.loads(response.read().decode("utf-8")))
        except error.HTTPError as exc:
            logger.warning(
                "openai_browser_agent_http_error",
                extra={"status": exc.code, "body": exc.read().decode("utf-8", errors="replace")},
            )
            raise

    def _create_assessment_response(
        self,
        snapshot: BrowserAgentSnapshot,
        goal: str,
        task_name: str,
        step_index: int,
        recent_actions: Sequence[dict[str, object]],
        extra_rules: Sequence[str],
    ) -> dict[str, object]:
        elements_payload = [
            {
                "element_id": element.element_id,
                "tag": element.tag,
                "role": element.role,
                "label": element.label,
                "text": truncate_text(element.text or "", limit=120) or None,
                "placeholder": element.placeholder,
                "name": element.name,
                "input_type": element.input_type,
                "href": element.href,
                "current_value": truncate_text(element.current_value or "", limit=80) or None,
                "disabled": element.disabled,
            }
            for element in snapshot.elements
        ]
        prompt_payload = {
            "goal": goal,
            "task_name": task_name,
            "step_index": step_index,
            "page": {
                "url": snapshot.url,
                "title": snapshot.title,
                "visible_text": snapshot.visible_text,
                "elements": elements_payload,
            },
            "recent_action_history": list(recent_actions),
            "rules": [
                "Assess the current page state only. Do not propose a next action.",
                "Use complete only when the goal is already achieved on the visible page.",
                (
                    "Use blocked when the page shows a visible validation error, "
                    "denial, or a dead end."
                ),
                (
                    "Use pending when the page still looks like loading, processing, "
                    "or waiting for the UI to settle."
                ),
                (
                    "Use manual_intervention when a human needs to solve captcha, OTP, "
                    "or another checkpoint."
                ),
                "Use unknown when the snapshot does not provide enough evidence yet.",
                "Keep summary short and concrete.",
                "Use evidence for short visible clues from the page.",
                *extra_rules,
            ],
        }
        logger.info(
            "linkedin_browser_agent_assessment_prompt",
            extra={"model": self._model, "task_name": task_name, "prompt_payload": prompt_payload},
        )

        body = {
            "model": self._model,
            "input": [
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "You are assessing a browser state for a LinkedIn task. "
                                "Return only a structured assessment of whether the goal "
                                "is already complete, blocked, pending, requires manual "
                                "intervention, or is still unknown."
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
                    "name": "browser_task_assessment",
                    "schema": STRUCTURED_ASSESSMENT_SCHEMA,
                    "strict": True,
                },
            },
        }
        payload_bytes = json.dumps(body, ensure_ascii=True).encode("utf-8")
        http_request = request.Request(
            self.endpoint,
            data=payload_bytes,
            headers={
                "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=30) as response:  # noqa: S310
                return cast(dict[str, object], json.loads(response.read().decode("utf-8")))
        except error.HTTPError as exc:
            logger.warning(
                "openai_browser_agent_assessment_http_error",
                extra={"status": exc.code, "body": exc.read().decode("utf-8", errors="replace")},
            )
            raise

    def _extract_output_text(self, response_data: dict[str, object]) -> str:
        direct_output = response_data.get("output_text")
        if isinstance(direct_output, str):
            return direct_output.strip()

        output_items = response_data.get("output", ())
        if not isinstance(output_items, list):
            return ""
        for item in output_items:
            if not isinstance(item, dict):
                continue
            content_items = item.get("content", ())
            if not isinstance(content_items, list):
                continue
            for content_item in content_items:
                if not isinstance(content_item, dict):
                    continue
                if content_item.get("type") != "output_text":
                    continue
                text = content_item.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return ""

    async def _execute_action(
        self,
        *,
        page: Page,
        action: BrowserAgentAction,
        values: Mapping[BrowserValueSource, str],
    ) -> None:
        if action.action_type == "wait":
            await page.wait_for_timeout(max(1, action.wait_seconds) * 1_000)
            return
        if action.action_type == "done":
            await page.wait_for_timeout(750)
            return
        if action.action_type == "fail":
            msg = action.reasoning or "Browser agent reported that no safe action was available."
            raise BrowserAutomationError(msg)

        locator = self._locator_for_action(page, action)
        await locator.scroll_into_view_if_needed()

        if action.action_type == "click":
            await self._pause_before_click(page)
            await locator.click()
            await self._settle_page(page)
            return

        if action.action_type == "fill":
            value = self._resolve_fill_value(action, values)
            await locator.click()
            await locator.fill(value)
            await page.wait_for_timeout(350)
            return

    async def _pause_before_click(self, page: Page) -> None:
        delay_ms = random.randint(self._min_action_delay_ms, self._max_action_delay_ms)
        logger.info("linkedin_browser_agent_delay", extra={"delay_ms": delay_ms})
        await page.wait_for_timeout(delay_ms)

    def _locator_for_action(self, page: Page, action: BrowserAgentAction) -> Locator:
        if action.element_id is None:
            msg = "Browser agent returned an action without element_id."
            raise BrowserAutomationError(msg)
        locator = page.locator(f'[data-job-applier-agent-id="{action.element_id}"]').first
        return locator

    def _resolve_fill_value(
        self,
        action: BrowserAgentAction,
        values: Mapping[BrowserValueSource, str],
    ) -> str:
        if action.value_source == "literal":
            if action.value is None:
                msg = "Browser agent returned literal fill without a value."
                raise BrowserAutomationError(msg)
            return action.value
        source = action.value_source
        if source is not None and source in values:
            return values[source]
        msg = "Browser agent returned an unsupported task value source."
        raise BrowserAutomationError(msg)

    async def _settle_page(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(500)

    def _validate_action_against_snapshot(
        self,
        action: BrowserAgentAction,
        *,
        snapshot: BrowserAgentSnapshot,
        available_values: Mapping[BrowserValueSource, str],
        allowed_action_types: Sequence[BrowserActionType] | None,
    ) -> None:
        if allowed_action_types is not None and action.action_type not in set(allowed_action_types):
            msg = "Browser agent returned an action type that is not allowed for this task."
            raise BrowserAutomationError(msg)
        valid_element_ids = {element.element_id for element in snapshot.elements}
        if action.action_type in {"click", "fill"} and action.element_id not in valid_element_ids:
            msg = "Browser agent referenced an element that does not exist in the snapshot."
            raise BrowserAutomationError(msg)
        if action.action_type == "fill" and action.value_source is None:
            msg = "Browser agent returned fill without a value_source."
            raise BrowserAutomationError(msg)
        if action.action_type == "fill" and action.value_source not in {
            "literal",
            *available_values.keys(),
        }:
            msg = "Browser agent returned a value_source that does not belong to the current task."
            raise BrowserAutomationError(msg)


def parse_browser_action(payload: dict[str, object]) -> BrowserAgentAction:
    """Validate the structured action returned by the browser planner."""

    action_type = payload.get("action_type")
    if action_type not in {"click", "fill", "wait", "done", "fail"}:
        msg = "Browser agent returned an unsupported action_type."
        raise BrowserAutomationError(msg)

    value_source = payload.get("value_source")
    if value_source is not None and not isinstance(value_source, str):
        msg = "Browser agent returned an unsupported value_source."
        raise BrowserAutomationError(msg)

    wait_seconds_raw = payload.get("wait_seconds", 0)
    if not isinstance(wait_seconds_raw, (int, float, str)):
        msg = "Browser agent returned an invalid wait_seconds value."
        raise BrowserAutomationError(msg)
    try:
        wait_seconds = int(wait_seconds_raw)
    except (TypeError, ValueError) as exc:
        msg = "Browser agent returned an invalid wait_seconds value."
        raise BrowserAutomationError(msg) from exc

    return BrowserAgentAction(
        action_type=cast(BrowserActionType, action_type),
        element_id=_optional_text(payload.get("element_id")),
        value_source=_optional_text(value_source),
        value=_optional_text(payload.get("value")),
        action_intent=_optional_text(payload.get("action_intent")),
        wait_seconds=max(0, wait_seconds),
        reasoning=str(payload.get("reasoning") or "").strip(),
    )


def parse_browser_task_assessment(payload: dict[str, object]) -> BrowserTaskAssessment:
    """Validate the structured state assessment returned by the browser planner."""

    status = payload.get("status")
    if status not in {"complete", "blocked", "pending", "manual_intervention", "unknown"}:
        msg = "Browser agent returned an unsupported assessment status."
        raise BrowserAutomationError(msg)

    confidence_raw = payload.get("confidence", 0.0)
    if not isinstance(confidence_raw, (int, float, str)):
        msg = "Browser agent returned an invalid assessment confidence."
        raise BrowserAutomationError(msg)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError) as exc:
        msg = "Browser agent returned an invalid assessment confidence."
        raise BrowserAutomationError(msg) from exc

    evidence_raw = payload.get("evidence", ())
    evidence_items = evidence_raw if isinstance(evidence_raw, list) else ()
    evidence = tuple(
        item.strip() for item in evidence_items if isinstance(item, str) and item.strip()
    )

    return BrowserTaskAssessment(
        status=cast(BrowserAssessmentStatus, status),
        confidence=max(0.0, min(1.0, confidence)),
        summary=str(payload.get("summary") or "").strip(),
        evidence=evidence,
    )


def _optional_text(value: object) -> str | None:
    text = collapse_text(value if isinstance(value, str) else None)
    return text or None
