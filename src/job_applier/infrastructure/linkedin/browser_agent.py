"""LLM-guided browser control for volatile LinkedIn screens."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast
from urllib import error, request

from playwright.async_api import Locator, Page
from pydantic import SecretStr

from job_applier.observability import append_output_jsonl

logger = logging.getLogger(__name__)

BrowserActionType = Literal["click", "fill", "press", "scroll", "wait", "done", "fail"]
BrowserValueSource = str
BrowserScrollTarget = Literal["active_surface", "page"]
BrowserScrollDirection = Literal["down", "up"]
BrowserPressKey = Literal["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Space"]
PageRequiresLogin = Callable[[Page], Awaitable[bool]]
PageTaskComplete = Callable[[Page], Awaitable[bool]]
BrowserAssessmentStatus = Literal[
    "complete",
    "blocked",
    "pending",
    "manual_intervention",
    "unknown",
]
BrowserStallStatus = Literal["recoverable", "manual_intervention", "abort"]

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action_type": {
            "type": "string",
            "enum": ["click", "fill", "press", "scroll", "wait", "done", "fail"],
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
        "key_name": {
            "type": ["string", "null"],
            "enum": ["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Space", None],
            "description": "Keyboard key to press for press actions.",
        },
        "scroll_target": {
            "type": ["string", "null"],
            "enum": ["active_surface", "page", None],
            "description": (
                "Use active_surface for modal/dialog scrolling and page for background page "
                "scrolling."
            ),
        },
        "scroll_direction": {
            "type": ["string", "null"],
            "enum": ["down", "up", None],
            "description": "Direction for scroll actions.",
        },
        "scroll_amount": {
            "type": "integer",
            "minimum": 100,
            "maximum": 1600,
            "description": "Approximate scroll delta in pixels for scroll actions.",
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
        "key_name",
        "scroll_target",
        "scroll_direction",
        "scroll_amount",
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

STRUCTURED_STALL_DIAGNOSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["recoverable", "manual_intervention", "abort"],
        },
        "summary": {
            "type": "string",
            "description": "Short explanation of why the task looks stalled.",
        },
        "blocker_category": {
            "type": "string",
            "description": "Short machine-friendly blocker label.",
        },
        "next_plan": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 4,
            "description": "Short ordered recovery plan for the next actions.",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Visible clues that explain the stall.",
        },
    },
    "required": ["status", "summary", "blocker_category", "next_plan", "evidence"],
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
        "[role='option']",
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
    focused: bool = False
    invalid: bool = False
    expanded: bool = False
    selected: bool = False
    validation_text: str | None = None
    is_priority_target: bool = False
    candidate_label: str | None = None


@dataclass(frozen=True, slots=True)
class BrowserAgentSnapshot:
    """Compact page snapshot sent to the browser-planning model."""

    url: str
    title: str
    visible_text: str
    elements: tuple[BrowserAgentElement, ...]
    active_surface: str | None = None
    active_surface_scrollable: bool = False
    active_surface_can_scroll_down: bool = False
    active_surface_can_scroll_up: bool = False
    page_can_scroll_down: bool = False
    page_can_scroll_up: bool = False


@dataclass(frozen=True, slots=True)
class BrowserAgentAction:
    """One planned browser action returned by the model."""

    action_type: BrowserActionType
    element_id: str | None
    value_source: BrowserValueSource | None
    value: str | None
    action_intent: str | None
    key_name: BrowserPressKey | None
    scroll_target: BrowserScrollTarget | None
    scroll_direction: BrowserScrollDirection | None
    scroll_amount: int
    wait_seconds: int
    reasoning: str


@dataclass(frozen=True, slots=True)
class BrowserTaskAssessment:
    """Structured interpretation of the current browser state."""

    status: BrowserAssessmentStatus
    confidence: float
    summary: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrowserStallDiagnosis:
    """Structured diagnosis used when the browser flow stops making progress."""

    status: BrowserStallStatus
    summary: str
    blocker_category: str
    next_plan: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


def _serialize_action(action: BrowserAgentAction) -> dict[str, object]:
    return {
        "action_type": action.action_type,
        "element_id": action.element_id,
        "value_source": action.value_source,
        "value": action.value,
        "action_intent": action.action_intent,
        "key_name": action.key_name,
        "scroll_target": action.scroll_target,
        "scroll_direction": action.scroll_direction,
        "scroll_amount": action.scroll_amount,
        "wait_seconds": action.wait_seconds,
        "reasoning": action.reasoning,
    }


def _serialize_assessment(assessment: BrowserTaskAssessment) -> dict[str, object]:
    return {
        "status": assessment.status,
        "confidence": assessment.confidence,
        "summary": assessment.summary,
        "evidence": list(assessment.evidence),
    }


def _serialize_stall_diagnosis(diagnosis: BrowserStallDiagnosis) -> dict[str, object]:
    return {
        "status": diagnosis.status,
        "summary": diagnosis.summary,
        "blocker_category": diagnosis.blocker_category,
        "next_plan": list(diagnosis.next_plan),
        "evidence": list(diagnosis.evidence),
    }


def collapse_text(value: str | None) -> str:
    """Collapse repeated whitespace and trim empty values."""

    return re.sub(r"\s+", " ", value or "").strip()


def truncate_text(value: str, *, limit: int) -> str:
    """Trim long text blocks to keep prompts small and relevant."""

    collapsed = collapse_text(value)
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1].rstrip()}…"


def snapshot_signature(snapshot: BrowserAgentSnapshot) -> str:
    """Return a stable signature for one planner snapshot."""

    payload = {
        "url": snapshot.url,
        "title": snapshot.title,
        "visible_text": snapshot.visible_text,
        "active_surface": snapshot.active_surface,
        "active_surface_scrollable": snapshot.active_surface_scrollable,
        "active_surface_can_scroll_down": snapshot.active_surface_can_scroll_down,
        "active_surface_can_scroll_up": snapshot.active_surface_can_scroll_up,
        "page_can_scroll_down": snapshot.page_can_scroll_down,
        "page_can_scroll_up": snapshot.page_can_scroll_up,
        "elements": [
            {
                "element_id": element.element_id,
                "tag": element.tag,
                "role": element.role,
                "label": element.label,
                "text": element.text,
                "placeholder": element.placeholder,
                "name": element.name,
                "input_type": element.input_type,
                "href": element.href,
                "current_value": element.current_value,
                "disabled": element.disabled,
                "focused": element.focused,
                "invalid": element.invalid,
                "expanded": element.expanded,
                "selected": element.selected,
                "validation_text": element.validation_text,
                "is_priority_target": element.is_priority_target,
                "candidate_label": element.candidate_label,
            }
            for element in snapshot.elements
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def serialize_snapshot(snapshot: BrowserAgentSnapshot) -> dict[str, object]:
    """Return a JSON-serializable planner snapshot payload."""

    return {
        "url": snapshot.url,
        "title": snapshot.title,
        "visible_text": snapshot.visible_text,
        "active_surface": snapshot.active_surface,
        "active_surface_scrollable": snapshot.active_surface_scrollable,
        "active_surface_can_scroll_down": snapshot.active_surface_can_scroll_down,
        "active_surface_can_scroll_up": snapshot.active_surface_can_scroll_up,
        "page_can_scroll_down": snapshot.page_can_scroll_down,
        "page_can_scroll_up": snapshot.page_can_scroll_up,
        "elements": [
            {
                "element_id": element.element_id,
                "tag": element.tag,
                "role": element.role,
                "label": element.label,
                "text": element.text,
                "placeholder": element.placeholder,
                "name": element.name,
                "input_type": element.input_type,
                "href": element.href,
                "current_value": element.current_value,
                "disabled": element.disabled,
                "focused": element.focused,
                "invalid": element.invalid,
                "expanded": element.expanded,
                "selected": element.selected,
                "validation_text": element.validation_text,
                "is_priority_target": element.is_priority_target,
                "candidate_label": element.candidate_label,
            }
            for element in snapshot.elements
        ],
    }


def has_manual_intervention_cues(snapshot: BrowserAgentSnapshot) -> bool:
    """Return whether the page looks like a captcha or verification checkpoint."""

    haystack = " ".join((snapshot.url, snapshot.title, snapshot.visible_text)).lower()
    return any(pattern in haystack for pattern in MANUAL_INTERVENTION_PATTERNS)


class BrowserDomSnapshotter:
    """Capture a compact, selector-free snapshot of visible interactive controls."""

    def __init__(self, *, max_elements: int = 32, max_visible_text: int = 1_600) -> None:
        self._max_elements = max_elements
        self._max_visible_text = max_visible_text

    async def capture(
        self,
        page: Page,
        *,
        focus_locator: Locator | None = None,
        priority_locator: Locator | None = None,
    ) -> BrowserAgentSnapshot:
        """Return the cleaned page snapshot used by the planner."""

        title = await page.title()
        focus_handle = None
        priority_handle = None
        if focus_locator is not None:
            try:
                focus_handle = await focus_locator.first.element_handle()
            except Exception:  # noqa: BLE001
                focus_handle = None
        if priority_locator is not None:
            try:
                priority_handle = await priority_locator.first.element_handle()
            except Exception:  # noqa: BLE001
                priority_handle = None
        try:
            if focus_handle is not None:
                payload = await focus_handle.evaluate(
                    """
                    (focusedNode, { interactiveSelectors, maxElements, priorityNode }) => {
                      const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
                      const isVisible = (node) => {
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        if (
                          rect.bottom <= 0 ||
                          rect.right <= 0 ||
                          rect.top >= window.innerHeight ||
                          rect.left >= window.innerWidth
                        ) {
                          return false;
                        }
                        let ancestor = node.parentElement;
                        while (ancestor) {
                          const ancestorStyle = window.getComputedStyle(ancestor);
                          const overflowY = ancestorStyle.overflowY || ancestorStyle.overflow;
                          const overflowX = ancestorStyle.overflowX || ancestorStyle.overflow;
                          const clipsOverflow =
                            ["auto", "scroll", "hidden", "clip", "overlay"].includes(overflowY)
                            || ["auto", "scroll", "hidden", "clip", "overlay"].includes(overflowX);
                          if (clipsOverflow) {
                            const ancestorRect = ancestor.getBoundingClientRect();
                            if (
                              rect.bottom < ancestorRect.top + 1 ||
                              rect.top > ancestorRect.bottom - 1 ||
                              rect.right < ancestorRect.left + 1 ||
                              rect.left > ancestorRect.right - 1
                            ) {
                              return false;
                            }
                          }
                          ancestor = ancestor.parentElement;
                        }
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
                      const describeSurface = (node) => {
                        if (!node) {
                          return "";
                        }
                        const labelledBy = collapse(node.getAttribute("aria-labelledby"));
                        if (labelledBy) {
                          const heading = labelledBy
                            .split(/\\s+/)
                            .map((id) => document.getElementById(id))
                            .find(
                              (item) =>
                                item && collapse(item.innerText || item.textContent || ""),
                            );
                          if (heading) {
                            return collapse(heading.innerText || heading.textContent || "");
                          }
                        }
                        const ariaLabel = collapse(node.getAttribute("aria-label"));
                        if (ariaLabel) {
                          return ariaLabel;
                        }
                        const heading = node.querySelector("h1, h2, h3, [role='heading']");
                        if (heading && collapse(heading.innerText || heading.textContent || "")) {
                          return collapse(heading.innerText || heading.textContent || "");
                        }
                        return collapse(node.innerText || node.textContent || "").slice(0, 120);
                      };
                      const isScrollable = (node) => {
                        const style = window.getComputedStyle(node);
                        const overflowY = style.overflowY || style.overflow;
                        return (
                          ["auto", "scroll", "overlay"].includes(overflowY) &&
                          node.scrollHeight - node.clientHeight > 24
                        );
                      };
                      const summarizeNode = (node) => {
                        if (!node || node.nodeType !== 1) {
                          return "";
                        }
                        const tag = (node.tagName || "").toLowerCase();
                        const pieces = [];
                        const label = collapse(node.getAttribute("aria-label")) || labelFor(node);
                        const placeholder = collapse(node.getAttribute("placeholder"));
                        const role = collapse(node.getAttribute("role"));
                        const nodeText = collapse(node.innerText || node.textContent || "");
                        const currentValue = collapse(node.value);
                        if (tag === "button" || role === "button") {
                          pieces.push(label || nodeText);
                        } else if (tag === "select") {
                          pieces.push(label, currentValue || nodeText);
                        } else if (tag === "input" || tag === "textarea") {
                          pieces.push(label, currentValue || placeholder);
                        } else {
                          pieces.push(label, nodeText || placeholder);
                        }
                        return collapse(pieces.filter(Boolean).join(" "));
                      };
                      const splitIds = (value) =>
                        collapse(value)
                          .split(/\\s+/)
                          .map((item) => item.trim())
                          .filter(Boolean);
                      const collectValidationTextsForNode = (fieldNode) => {
                        if (!fieldNode || fieldNode.nodeType !== 1) {
                          return [];
                        }
                        const fieldRect = fieldNode.getBoundingClientRect();
                        const overlapsHorizontally = (candidateRect) => {
                          const overlap = Math.min(fieldRect.right, candidateRect.right)
                            - Math.max(fieldRect.left, candidateRect.left);
                          return overlap > Math.min(fieldRect.width, candidateRect.width) * 0.25;
                        };
                        const validationTexts = [];
                        const seenTexts = new Set();
                        const pushText = (candidate) => {
                          if (!candidate || candidate.nodeType !== 1) {
                            return;
                          }
                          if (!isVisible(candidate)) {
                            return;
                          }
                          if (
                            candidate === fieldNode
                            || candidate.contains(fieldNode)
                            || fieldNode.contains(candidate)
                          ) {
                            return;
                          }
                          const text = collapse(
                            candidate.innerText
                            || candidate.textContent
                            || candidate.getAttribute("aria-label")
                            || ""
                          );
                          if (!text || text.length > 180 || seenTexts.has(text)) {
                            return;
                          }
                          const rect = candidate.getBoundingClientRect();
                          const verticalGap = rect.top - fieldRect.bottom;
                          if (verticalGap < -6 || verticalGap > 96) {
                            return;
                          }
                          if (!overlapsHorizontally(rect)) {
                            return;
                          }
                          seenTexts.add(text);
                          validationTexts.push({ text, verticalGap });
                        };
                        const validationSelectors = [
                          "[role='alert']",
                          "[aria-live='assertive']",
                          "[aria-live='polite']",
                          ".artdeco-inline-feedback__message",
                          ".fb-dash-form-element__error-message",
                          "[data-test-form-element-error-messages]",
                        ].join(", ");
                        const validationScopes = [];
                        const pushValidationScope = (candidate) => {
                          if (
                            !candidate
                            || candidate.nodeType !== 1
                            || validationScopes.includes(candidate)
                          ) {
                            return;
                          }
                          validationScopes.push(candidate);
                        };
                        for (const attributeName of ["aria-errormessage"]) {
                          for (const id of splitIds(fieldNode.getAttribute(attributeName))) {
                            pushText(document.getElementById(id));
                          }
                        }
                        pushValidationScope(
                          fieldNode.closest(
                            [
                              ".fb-form-element",
                              ".jobs-easy-apply-form-section__grouping",
                              ".jobs-easy-apply-form-element",
                              "[role='group']",
                              "fieldset",
                              "section",
                              "form",
                            ].join(", ")
                          )
                        );
                        pushValidationScope(fieldNode.parentElement);
                        let ancestor = fieldNode.parentElement;
                        let depth = 0;
                        while (ancestor && depth < 4) {
                          for (const candidate of ancestor.querySelectorAll(validationSelectors)) {
                            pushText(candidate);
                          }
                          ancestor = ancestor.parentElement;
                          depth += 1;
                        }
                        for (const scope of validationScopes) {
                          for (const candidate of scope.querySelectorAll(validationSelectors)) {
                            pushText(candidate);
                          }
                        }
                        validationTexts.sort((left, right) => {
                          if (left.verticalGap !== right.verticalGap) {
                            return left.verticalGap - right.verticalGap;
                          }
                          return left.text.length - right.text.length;
                        });
                        return validationTexts.map((item) => item.text);
                      };
                      const collectPopupNodesForField = (fieldNode) => {
                        if (!fieldNode || fieldNode.nodeType !== 1) {
                          return [];
                        }
                        const relatedRoots = [];
                        const seenRoots = new Set();
                        const pushRoot = (candidate) => {
                          if (!candidate || candidate.nodeType !== 1 || seenRoots.has(candidate)) {
                            return;
                          }
                          seenRoots.add(candidate);
                          relatedRoots.push(candidate);
                        };
                        for (const attributeName of ["aria-controls", "aria-owns", "list"]) {
                          for (const id of splitIds(fieldNode.getAttribute(attributeName))) {
                            pushRoot(document.getElementById(id));
                          }
                        }
                        for (const id of splitIds(
                          fieldNode.getAttribute("aria-activedescendant"),
                        )) {
                          pushRoot(document.getElementById(id));
                        }
                        const nodes = [];
                        const seenNodes = new Set();
                        const pushNode = (candidate) => {
                          if (!candidate || candidate.nodeType !== 1 || seenNodes.has(candidate)) {
                            return;
                          }
                          if (!isVisible(candidate)) {
                            return;
                          }
                          seenNodes.add(candidate);
                          nodes.push(candidate);
                        };
                        const popupSelectors = [
                          interactiveSelectors,
                          "[aria-selected]",
                          "li",
                          "[data-value]",
                        ].join(", ");
                        for (const root of relatedRoots) {
                          pushNode(root);
                          for (const optionNode of root.querySelectorAll(popupSelectors)) {
                            if (optionNode === fieldNode) {
                              continue;
                            }
                            pushNode(optionNode);
                          }
                        }
                        if (nodes.length === 0) {
                          for (const optionNode of document.querySelectorAll(
                            "[role='option'], [aria-selected], [data-value]"
                          )) {
                            if (optionNode === fieldNode) {
                              continue;
                            }
                            pushNode(optionNode);
                          }
                        }
                        return nodes;
                      };
                      document
                        .querySelectorAll("[data-job-applier-agent-id]")
                        .forEach((node) => node.removeAttribute("data-job-applier-agent-id"));
                      document
                        .querySelectorAll("[data-job-applier-active-surface-scroll-target]")
                        .forEach((node) =>
                          node.removeAttribute("data-job-applier-active-surface-scroll-target"),
                        );
                      document
                        .querySelectorAll("[data-job-applier-active-surface]")
                        .forEach((node) => node.removeAttribute("data-job-applier-active-surface"));
                      const isInteractiveNode = (node) => {
                        if (!node || node.nodeType !== 1 || typeof node.matches !== "function") {
                          return false;
                        }
                        try {
                          return node.matches(interactiveSelectors);
                        } catch {
                          return false;
                        }
                      };
                      const markedBlockingAction = document.querySelector(
                        "[data-job-applier-blocking-action='true']"
                      );
                      const markedBlockingSurface = document.querySelector(
                        "[data-job-applier-blocking-surface='true']"
                      );
                      const activeSurface = (
                        markedBlockingSurface
                        && markedBlockingSurface.nodeType === 1
                        && isVisible(markedBlockingSurface)
                      )
                        ? {
                            node: markedBlockingSurface,
                            label: describeSurface(markedBlockingSurface),
                          }
                        : (
                          focusedNode && focusedNode.nodeType === 1
                            ? {
                                node: focusedNode,
                                label: describeSurface(focusedNode),
                              }
                            : null
                        );
                      if (activeSurface) {
                        activeSurface.node.setAttribute("data-job-applier-active-surface", "true");
                      }
                      const findActiveScrollTarget = (surfaceNode) => {
                        const candidates = [
                          surfaceNode,
                          ...Array.from(surfaceNode.querySelectorAll("*")),
                        ]
                          .filter(isVisible)
                          .filter(isScrollable)
                          .map((node, index) => {
                            const rect = node.getBoundingClientRect();
                            return {
                              node,
                              index,
                              area: rect.width * rect.height,
                              remainingScroll: Math.max(0, node.scrollHeight - node.clientHeight),
                            };
                          })
                          .sort((left, right) => {
                            if (left.area !== right.area) {
                              return right.area - left.area;
                            }
                            if (left.remainingScroll !== right.remainingScroll) {
                              return right.remainingScroll - left.remainingScroll;
                            }
                            return left.index - right.index;
                          });
                        return candidates[0]?.node || null;
                      };
                      const activeScrollTarget = activeSurface
                        ? findActiveScrollTarget(activeSurface.node)
                        : null;
                      if (activeScrollTarget) {
                        activeScrollTarget.setAttribute(
                          "data-job-applier-active-surface-scroll-target",
                          "true",
                        );
                      }
                      const priorityField =
                        priorityNode &&
                        priorityNode.nodeType === 1 &&
                        activeSurface &&
                        activeSurface.node.contains(priorityNode)
                          ? priorityNode
                          : null;
                      const activeField = priorityField || (
                        activeSurface &&
                        document.activeElement &&
                        document.activeElement.nodeType === 1 &&
                        activeSurface.node.contains(document.activeElement)
                          ? document.activeElement
                          : null
                      );
                      const relatedPopupNodes = activeField
                        ? collectPopupNodesForField(activeField)
                        : [];
                      const nodes = [];
                      if (activeSurface) {
                        const seenNodes = new Set();
                        const pushNode = (candidate) => {
                          if (
                            !candidate
                            || candidate.nodeType !== 1
                            || seenNodes.has(candidate)
                            || !isVisible(candidate)
                          ) {
                            return;
                          }
                          seenNodes.add(candidate);
                          nodes.push(candidate);
                        };
                        pushNode(markedBlockingAction);
                        pushNode(priorityField);
                        if (activeField && activeField !== priorityField) {
                          pushNode(activeField);
                        }
                        if (
                          activeSurface.node !== priorityField
                          && activeSurface.node !== activeField
                          && isInteractiveNode(activeSurface.node)
                        ) {
                          pushNode(activeSurface.node);
                        }
                        const activeSurfaceCandidates =
                          activeSurface.node.querySelectorAll(interactiveSelectors);
                        for (const candidate of activeSurfaceCandidates) {
                          pushNode(candidate);
                        }
                        for (const candidate of relatedPopupNodes) {
                          pushNode(candidate);
                        }
                        nodes.sort((left, right) => {
                          if (priorityField) {
                            if (left === priorityField && right !== priorityField) {
                              return -1;
                            }
                            if (right === priorityField && left !== priorityField) {
                              return 1;
                            }
                          }
                          if (activeField) {
                            if (left === activeField && right !== activeField) {
                              return -1;
                            }
                            if (right === activeField && left !== activeField) {
                              return 1;
                            }
                          }
                          const leftRect = left.getBoundingClientRect();
                          const rightRect = right.getBoundingClientRect();
                          if (Math.abs(leftRect.top - rightRect.top) > 4) {
                            return leftRect.top - rightRect.top;
                          }
                          return leftRect.left - rightRect.left;
                        });
                      }
                      const items = [];
                      let counter = 1;

                      for (const node of nodes) {
                        const elementId = `agent-${counter}`;
                        counter += 1;
                        node.setAttribute("data-job-applier-agent-id", elementId);
                        const validationTexts = collectValidationTextsForNode(node);
                        const nativeValidity = typeof node.checkValidity === "function"
                          ? node.checkValidity()
                          : true;
                        const invalidPseudoClass = typeof node.matches === "function"
                          ? node.matches(":invalid")
                          : false;
                        const ariaInvalid = node.getAttribute("aria-invalid") === "true";
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
                          disabled: Boolean(
                            node.disabled || node.getAttribute("aria-disabled") === "true",
                          ),
                          focused: document.activeElement === node,
                          invalid: (
                            ariaInvalid
                            || invalidPseudoClass
                            || !nativeValidity
                            || validationTexts.length > 0
                          ),
                          expanded: node.getAttribute("aria-expanded") === "true",
                          selected: (
                            node.getAttribute("aria-selected") === "true"
                            || node.getAttribute("aria-checked") === "true"
                          ),
                          validation_text: validationTexts[0] || "",
                          is_priority_target: priorityField === node,
                          candidate_label: summarizeNode(node),
                        });
                        if (items.length >= maxElements) {
                          break;
                        }
                      }

                      const visibleText = collapse(
                        activeSurface
                          ? [
                              describeSurface(activeSurface.node),
                              priorityField ? summarizeNode(priorityField) : "",
                              activeField && activeField !== priorityField
                                ? summarizeNode(activeField)
                                : "",
                              ...nodes.map(summarizeNode).filter(Boolean),
                            ].join(" ")
                          : (document.body?.innerText || ""),
                      );
                      const pageScrollTop =
                        window.scrollY
                        || document.documentElement.scrollTop
                        || document.body.scrollTop
                        || 0;
                      const pageScrollHeight = Math.max(
                        document.documentElement.scrollHeight || 0,
                        document.body?.scrollHeight || 0,
                      );
                      const pageClientHeight =
                        window.innerHeight || document.documentElement.clientHeight || 0;
                      return {
                        visible_text: visibleText,
                        elements: items,
                        active_surface: activeSurface ? activeSurface.label : "",
                        active_surface_scrollable: Boolean(activeScrollTarget),
                        active_surface_can_scroll_down: activeScrollTarget
                          ? (
                            activeScrollTarget.scrollTop + activeScrollTarget.clientHeight
                            < activeScrollTarget.scrollHeight - 8
                          )
                          : false,
                        active_surface_can_scroll_up: activeScrollTarget
                          ? activeScrollTarget.scrollTop > 8
                          : false,
                        page_can_scroll_down:
                          pageScrollTop + pageClientHeight < pageScrollHeight - 8,
                        page_can_scroll_up: pageScrollTop > 8,
                      };
                    }
                    """,
                    {
                        "interactiveSelectors": INTERACTIVE_SELECTORS,
                        "maxElements": self._max_elements,
                        "priorityNode": priority_handle,
                    },
                )
            else:
                payload = await page.evaluate(
                    """
            ({ interactiveSelectors, maxElements, priorityNode }) => {
              const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const isVisible = (node) => {
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                if (
                  rect.bottom <= 0 ||
                  rect.right <= 0 ||
                  rect.top >= window.innerHeight ||
                  rect.left >= window.innerWidth
                ) {
                  return false;
                }
                let ancestor = node.parentElement;
                while (ancestor) {
                  const ancestorStyle = window.getComputedStyle(ancestor);
                  const overflowY = ancestorStyle.overflowY || ancestorStyle.overflow;
                  const overflowX = ancestorStyle.overflowX || ancestorStyle.overflow;
                  const clipsOverflow =
                    ["auto", "scroll", "hidden", "clip", "overlay"].includes(overflowY)
                    || ["auto", "scroll", "hidden", "clip", "overlay"].includes(overflowX);
                  if (clipsOverflow) {
                    const ancestorRect = ancestor.getBoundingClientRect();
                    if (
                      rect.bottom < ancestorRect.top + 1 ||
                      rect.top > ancestorRect.bottom - 1 ||
                      rect.right < ancestorRect.left + 1 ||
                      rect.left > ancestorRect.right - 1
                    ) {
                      return false;
                    }
                  }
                  ancestor = ancestor.parentElement;
                }
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
              const summarizeNode = (node) => {
                if (!node || node.nodeType !== 1) {
                  return "";
                }
                const tag = (node.tagName || "").toLowerCase();
                const pieces = [];
                const label = collapse(node.getAttribute("aria-label")) || labelFor(node);
                const placeholder = collapse(node.getAttribute("placeholder"));
                const role = collapse(node.getAttribute("role"));
                const nodeText = collapse(node.innerText || node.textContent || "");
                const currentValue = collapse(node.value);
                if (tag === "button" || role === "button") {
                  pieces.push(label || nodeText);
                } else if (tag === "select") {
                  pieces.push(label, currentValue || nodeText);
                } else if (tag === "input" || tag === "textarea") {
                  pieces.push(label, currentValue || placeholder);
                } else {
                  pieces.push(label, nodeText || placeholder);
                }
                return collapse(pieces.filter(Boolean).join(" "));
              };
              const splitIds = (value) =>
                collapse(value)
                  .split(/\\s+/)
                  .map((item) => item.trim())
                  .filter(Boolean);
              const collectValidationTextsForNode = (fieldNode) => {
                if (!fieldNode || fieldNode.nodeType !== 1) {
                  return [];
                }
                const fieldRect = fieldNode.getBoundingClientRect();
                const overlapsHorizontally = (candidateRect) => {
                  const overlap = Math.min(fieldRect.right, candidateRect.right)
                    - Math.max(fieldRect.left, candidateRect.left);
                  return overlap > Math.min(fieldRect.width, candidateRect.width) * 0.25;
                };
                const validationTexts = [];
                const seenTexts = new Set();
                const pushText = (candidate) => {
                  if (!candidate || candidate.nodeType !== 1) {
                    return;
                  }
                  if (!isVisible(candidate)) {
                    return;
                  }
                  if (
                    candidate === fieldNode
                    || candidate.contains(fieldNode)
                    || fieldNode.contains(candidate)
                  ) {
                    return;
                  }
                  const text = collapse(
                    candidate.innerText
                    || candidate.textContent
                    || candidate.getAttribute("aria-label")
                    || ""
                  );
                  if (!text || text.length > 180 || seenTexts.has(text)) {
                    return;
                  }
                  const rect = candidate.getBoundingClientRect();
                  const verticalGap = rect.top - fieldRect.bottom;
                  if (verticalGap < -6 || verticalGap > 96) {
                    return;
                  }
                  if (!overlapsHorizontally(rect)) {
                    return;
                  }
                  seenTexts.add(text);
                  validationTexts.push({ text, verticalGap });
                };
                const validationSelectors = [
                  "[role='alert']",
                  "[aria-live='assertive']",
                  "[aria-live='polite']",
                  ".artdeco-inline-feedback__message",
                  ".fb-dash-form-element__error-message",
                  "[data-test-form-element-error-messages]",
                ].join(", ");
                const validationScopes = [];
                const pushValidationScope = (candidate) => {
                  if (
                    !candidate
                    || candidate.nodeType !== 1
                    || validationScopes.includes(candidate)
                  ) {
                    return;
                  }
                  validationScopes.push(candidate);
                };
                for (const attributeName of ["aria-errormessage"]) {
                  for (const id of splitIds(fieldNode.getAttribute(attributeName))) {
                    pushText(document.getElementById(id));
                  }
                }
                pushValidationScope(
                  fieldNode.closest(
                    [
                      ".fb-form-element",
                      ".jobs-easy-apply-form-section__grouping",
                      ".jobs-easy-apply-form-element",
                      "[role='group']",
                      "fieldset",
                      "section",
                      "form",
                    ].join(", ")
                  )
                );
                pushValidationScope(fieldNode.parentElement);
                let ancestor = fieldNode.parentElement;
                let depth = 0;
                while (ancestor && depth < 4) {
                  for (const candidate of ancestor.querySelectorAll(validationSelectors)) {
                    pushText(candidate);
                  }
                  ancestor = ancestor.parentElement;
                  depth += 1;
                }
                for (const scope of validationScopes) {
                  for (const candidate of scope.querySelectorAll(validationSelectors)) {
                    pushText(candidate);
                  }
                }
                validationTexts.sort((left, right) => {
                  if (left.verticalGap !== right.verticalGap) {
                    return left.verticalGap - right.verticalGap;
                  }
                  return left.text.length - right.text.length;
                });
                return validationTexts.map((item) => item.text);
              };
              const collectPopupNodesForField = (fieldNode) => {
                if (!fieldNode || fieldNode.nodeType !== 1) {
                  return [];
                }
                const relatedRoots = [];
                const seenRoots = new Set();
                const pushRoot = (candidate) => {
                  if (!candidate || candidate.nodeType !== 1 || seenRoots.has(candidate)) {
                    return;
                  }
                  seenRoots.add(candidate);
                  relatedRoots.push(candidate);
                };
                for (const attributeName of ["aria-controls", "aria-owns", "list"]) {
                  for (const id of splitIds(fieldNode.getAttribute(attributeName))) {
                    pushRoot(document.getElementById(id));
                  }
                }
                for (const id of splitIds(fieldNode.getAttribute("aria-activedescendant"))) {
                  pushRoot(document.getElementById(id));
                }
                const nodes = [];
                const seenNodes = new Set();
                const pushNode = (candidate) => {
                  if (!candidate || candidate.nodeType !== 1 || seenNodes.has(candidate)) {
                    return;
                  }
                  if (!isVisible(candidate)) {
                    return;
                  }
                  seenNodes.add(candidate);
                  nodes.push(candidate);
                };
                const popupSelectors = [
                  interactiveSelectors,
                  "[aria-selected]",
                  "li",
                  "[data-value]",
                ].join(", ");
                for (const root of relatedRoots) {
                  pushNode(root);
                  for (const optionNode of root.querySelectorAll(popupSelectors)) {
                    if (optionNode === fieldNode) {
                      continue;
                    }
                    pushNode(optionNode);
                  }
                }
                if (nodes.length === 0) {
                  for (const optionNode of document.querySelectorAll(
                    "[role='option'], [aria-selected], [data-value]"
                  )) {
                    if (optionNode === fieldNode) {
                      continue;
                    }
                    pushNode(optionNode);
                  }
                }
                return nodes;
              };
              const describeSurface = (node) => {
                if (!node) {
                  return "";
                }
                const labelledBy = collapse(node.getAttribute("aria-labelledby"));
                if (labelledBy) {
                  const heading = labelledBy
                    .split(/\\s+/)
                    .map((id) => document.getElementById(id))
                    .find((item) => item && collapse(item.innerText || item.textContent || ""));
                  if (heading) {
                    return collapse(heading.innerText || heading.textContent || "");
                  }
                }
                const ariaLabel = collapse(node.getAttribute("aria-label"));
                if (ariaLabel) {
                  return ariaLabel;
                }
                const heading = node.querySelector("h1, h2, h3, [role='heading']");
                if (heading && collapse(heading.innerText || heading.textContent || "")) {
                  return collapse(heading.innerText || heading.textContent || "");
                }
                return collapse(node.innerText || node.textContent || "").slice(0, 120);
              };
              const isScrollable = (node) => {
                const style = window.getComputedStyle(node);
                const overflowY = style.overflowY || style.overflow;
                return (
                  ["auto", "scroll", "overlay"].includes(overflowY) &&
                  node.scrollHeight - node.clientHeight > 24
                );
              };
              document
                .querySelectorAll("[data-job-applier-agent-id]")
                .forEach((node) => node.removeAttribute("data-job-applier-agent-id"));
              document
                .querySelectorAll("[data-job-applier-active-surface-scroll-target]")
                .forEach((node) =>
                  node.removeAttribute("data-job-applier-active-surface-scroll-target"),
                );
              document
                .querySelectorAll("[data-job-applier-active-surface]")
                .forEach((node) => node.removeAttribute("data-job-applier-active-surface"));
              const isInteractiveNode = (node) => {
                if (!node || node.nodeType !== 1 || typeof node.matches !== "function") {
                  return false;
                }
                try {
                  return node.matches(interactiveSelectors);
                } catch {
                  return false;
                }
              };
              const surfaceSelectors = [
                "dialog[open]",
                "[role='dialog'][open]",
                "[role='dialog']",
                "[aria-modal='true']",
                "[data-testid='dialog']",
              ].join(", ");
              const mainSelectors = [
                "main",
                "[role='main']",
                "#main",
                "[data-testid='main']",
                "article",
              ].join(", ");
              const visibleSurfaces = Array.from(document.querySelectorAll(surfaceSelectors))
                .filter(isVisible)
                .map((node, index) => {
                  const rect = node.getBoundingClientRect();
                  const style = window.getComputedStyle(node);
                  const zIndex = Number.parseInt(style.zIndex || "0", 10);
                  return {
                    node,
                    index,
                    area: rect.width * rect.height,
                    zIndex: Number.isFinite(zIndex) ? zIndex : 0,
                    label: describeSurface(node),
                  };
                })
                .sort((left, right) => {
                  if (left.zIndex !== right.zIndex) {
                    return right.zIndex - left.zIndex;
                  }
                  if (left.area !== right.area) {
                    return right.area - left.area;
                  }
                  return right.index - left.index;
                });
              const priorityTarget =
                priorityNode && priorityNode.nodeType === 1 ? priorityNode : null;
              const prioritySurfaceNode = priorityTarget
                ? priorityTarget.closest(
                    [
                      "dialog[open]",
                      "[role='dialog'][open]",
                      "[role='dialog']",
                      "[aria-modal='true']",
                      ".jobs-easy-apply-modal",
                      ".jobs-easy-apply-content",
                      ".jobs-easy-apply-form-section__grouping",
                      ".jobs-easy-apply-form-element",
                      "[role='group']",
                      "fieldset",
                      "section",
                      "form",
                    ].join(", ")
                  )
                : null;
              const activeElementNode =
                document.activeElement && document.activeElement.nodeType === 1
                  ? document.activeElement
                  : null;
              const activeElementSurfaceNode = activeElementNode
                ? activeElementNode.closest(
                    [
                      "dialog[open]",
                      "[role='dialog'][open]",
                      "[role='dialog']",
                      "[aria-modal='true']",
                      ".jobs-easy-apply-modal",
                      ".jobs-easy-apply-content",
                      ".jobs-easy-apply-form-section__grouping",
                      ".jobs-easy-apply-form-element",
                      "[role='group']",
                      "fieldset",
                      "section",
                      "form",
                    ].join(", ")
                  )
                : null;
              const focusedSurfaceNode =
                (prioritySurfaceNode && isVisible(prioritySurfaceNode))
                  ? prioritySurfaceNode
                  : (
                    activeElementSurfaceNode && isVisible(activeElementSurfaceNode)
                      ? activeElementSurfaceNode
                      : null
                  );
              const focusedSurfaceRect = focusedSurfaceNode
                ? focusedSurfaceNode.getBoundingClientRect()
                : null;
              const activeSurface = focusedSurfaceNode && isVisible(focusedSurfaceNode)
                ? {
                    node: focusedSurfaceNode,
                    index: -1,
                    area: focusedSurfaceRect
                      ? focusedSurfaceRect.width * focusedSurfaceRect.height
                      : 0,
                    zIndex: Number.MAX_SAFE_INTEGER,
                    label: describeSurface(focusedSurfaceNode),
                  }
                : (visibleSurfaces[0] || null);
              const markedBlockingAction = document.querySelector(
                "[data-job-applier-blocking-action='true']"
              );
              const markedBlockingSurfaceNode = document.querySelector(
                "[data-job-applier-blocking-surface='true']"
              );
              const markedBlockingSurfaceRect = markedBlockingSurfaceNode
                ? markedBlockingSurfaceNode.getBoundingClientRect()
                : null;
              const blockingSurface = (
                markedBlockingSurfaceNode
                && markedBlockingSurfaceNode.nodeType === 1
                && isVisible(markedBlockingSurfaceNode)
              )
                ? {
                    node: markedBlockingSurfaceNode,
                    index: -2,
                    area: markedBlockingSurfaceRect
                      ? markedBlockingSurfaceRect.width * markedBlockingSurfaceRect.height
                      : 0,
                    zIndex: Number.MAX_SAFE_INTEGER,
                    label: describeSurface(markedBlockingSurfaceNode),
                  }
                : null;
              const prioritizedActiveSurface = blockingSurface || activeSurface;
              if (prioritizedActiveSurface) {
                prioritizedActiveSurface.node.setAttribute(
                  "data-job-applier-active-surface",
                  "true"
                );
              }
              const findActiveScrollTarget = (surfaceNode) => {
                const candidates = [surfaceNode, ...Array.from(surfaceNode.querySelectorAll("*"))]
                  .filter(isVisible)
                  .filter(isScrollable)
                  .map((node, index) => {
                    const rect = node.getBoundingClientRect();
                    return {
                      node,
                      index,
                      area: rect.width * rect.height,
                      remainingScroll: Math.max(0, node.scrollHeight - node.clientHeight),
                    };
                  })
                  .sort((left, right) => {
                    if (left.area !== right.area) {
                      return right.area - left.area;
                    }
                    if (left.remainingScroll !== right.remainingScroll) {
                      return right.remainingScroll - left.remainingScroll;
                    }
                    return left.index - right.index;
                  });
                return candidates[0]?.node || null;
              };
              const activeScrollTarget = prioritizedActiveSurface
                ? findActiveScrollTarget(prioritizedActiveSurface.node)
                : null;
              if (activeScrollTarget) {
                activeScrollTarget.setAttribute(
                  "data-job-applier-active-surface-scroll-target",
                  "true",
                );
              }
              const mainRoot = document.querySelector(mainSelectors);
              const scopeRoot = prioritizedActiveSurface
                ? prioritizedActiveSurface.node
                : (
                  priorityTarget?.closest(
                    [
                      ".jobs-easy-apply-form-section__grouping",
                      ".jobs-easy-apply-form-element",
                      "[role='group']",
                      "fieldset",
                      "section",
                      "form",
                    ].join(", ")
                  )
                  || mainRoot
                  || document
                );
              const relatedPopupNodes = priorityTarget
                ? collectPopupNodesForField(priorityTarget)
                : [];
              const nodes = [];
              const seenNodes = new Set();
              const pushNode = (candidate) => {
                if (
                  !candidate
                  || candidate.nodeType !== 1
                  || seenNodes.has(candidate)
                  || !isVisible(candidate)
                ) {
                  return;
                }
                seenNodes.add(candidate);
                nodes.push(candidate);
              };
              pushNode(markedBlockingAction);
              pushNode(priorityTarget);
              if (
                prioritizedActiveSurface
                && prioritizedActiveSurface.node !== priorityTarget
                && isInteractiveNode(prioritizedActiveSurface.node)
              ) {
                pushNode(prioritizedActiveSurface.node);
              }
              for (const candidate of scopeRoot.querySelectorAll(interactiveSelectors)) {
                pushNode(candidate);
              }
              for (const candidate of relatedPopupNodes) {
                pushNode(candidate);
              }
              nodes.sort((left, right) => {
                if (priorityTarget) {
                  if (left === priorityTarget && right !== priorityTarget) {
                    return -1;
                  }
                  if (right === priorityTarget && left !== priorityTarget) {
                    return 1;
                  }
                }
                const leftRect = left.getBoundingClientRect();
                const rightRect = right.getBoundingClientRect();
                if (Math.abs(leftRect.top - rightRect.top) > 4) {
                  return leftRect.top - rightRect.top;
                }
                return leftRect.left - rightRect.left;
              });
              const items = [];
              let counter = 1;

              for (const node of nodes) {
                const elementId = `agent-${counter}`;
                counter += 1;
                node.setAttribute("data-job-applier-agent-id", elementId);
                const validationTexts = collectValidationTextsForNode(node);
                const nativeValidity = typeof node.checkValidity === "function"
                  ? node.checkValidity()
                  : true;
                const invalidPseudoClass = typeof node.matches === "function"
                  ? node.matches(":invalid")
                  : false;
                const ariaInvalid = node.getAttribute("aria-invalid") === "true";
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
                  focused: document.activeElement === node,
                  invalid: (
                    ariaInvalid
                    || invalidPseudoClass
                    || !nativeValidity
                    || validationTexts.length > 0
                  ),
                  expanded: node.getAttribute("aria-expanded") === "true",
                  selected: (
                    node.getAttribute("aria-selected") === "true"
                    || node.getAttribute("aria-checked") === "true"
                  ),
                  validation_text: validationTexts[0] || "",
                  is_priority_target: priorityTarget === node,
                  candidate_label: summarizeNode(node),
                });
                if (items.length >= maxElements) {
                  break;
                }
              }

              const visibleText = collapse(
                activeSurface
                  ? [
                      describeSurface(
                        prioritizedActiveSurface
                          ? prioritizedActiveSurface.node
                          : activeSurface.node
                      ),
                      priorityTarget ? summarizeNode(priorityTarget) : "",
                      ...nodes.map(summarizeNode).filter(Boolean),
                    ].join(" ")
                  : (
                    priorityTarget
                      ? [
                          summarizeNode(priorityTarget),
                          ...nodes.map(summarizeNode).filter(Boolean),
                        ].join(" ")
                      : (document.body?.innerText || "")
                  ),
              );
              const pageScrollTop =
                window.scrollY
                || document.documentElement.scrollTop
                || document.body.scrollTop
                || 0;
              const pageScrollHeight = Math.max(
                document.documentElement.scrollHeight || 0,
                document.body?.scrollHeight || 0,
              );
              const pageClientHeight =
                window.innerHeight || document.documentElement.clientHeight || 0;
              return {
                visible_text: visibleText,
                elements: items,
                active_surface: prioritizedActiveSurface ? prioritizedActiveSurface.label : "",
                active_surface_scrollable: Boolean(activeScrollTarget),
                active_surface_can_scroll_down: activeScrollTarget
                  ? (
                    activeScrollTarget.scrollTop + activeScrollTarget.clientHeight
                    < activeScrollTarget.scrollHeight - 8
                  )
                  : false,
                active_surface_can_scroll_up: activeScrollTarget
                  ? activeScrollTarget.scrollTop > 8
                  : false,
                page_can_scroll_down: pageScrollTop + pageClientHeight < pageScrollHeight - 8,
                page_can_scroll_up: pageScrollTop > 8,
              };
            }
            """,
                    {
                        "interactiveSelectors": INTERACTIVE_SELECTORS,
                        "maxElements": self._max_elements,
                        "priorityNode": priority_handle,
                    },
                )
        finally:
            if focus_handle is not None:
                await focus_handle.dispose()
            if priority_handle is not None:
                await priority_handle.dispose()
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
                focused=bool(item.get("focused")),
                invalid=bool(item.get("invalid")),
                expanded=bool(item.get("expanded")),
                selected=bool(item.get("selected")),
                validation_text=_optional_text(item.get("validation_text")),
                is_priority_target=bool(item.get("is_priority_target")),
                candidate_label=_optional_text(item.get("candidate_label")),
            )
            for item in raw_elements
            if isinstance(item, dict) and str(item.get("element_id") or "").strip()
        )
        snapshot = BrowserAgentSnapshot(
            url=page.url,
            title=truncate_text(title, limit=160),
            visible_text=truncate_text(
                str(raw_payload.get("visible_text") or ""),
                limit=self._max_visible_text,
            ),
            elements=elements,
            active_surface=_optional_text(raw_payload.get("active_surface")),
            active_surface_scrollable=bool(raw_payload.get("active_surface_scrollable")),
            active_surface_can_scroll_down=bool(raw_payload.get("active_surface_can_scroll_down")),
            active_surface_can_scroll_up=bool(raw_payload.get("active_surface_can_scroll_up")),
            page_can_scroll_down=bool(raw_payload.get("page_can_scroll_down")),
            page_can_scroll_up=bool(raw_payload.get("page_can_scroll_up")),
        )
        if focus_locator is not None and self._needs_page_scope_retry(snapshot):
            logger.info(
                "linkedin_browser_agent_focus_snapshot_too_sparse",
                extra={
                    "url": page.url,
                    "active_surface": snapshot.active_surface,
                    "visible_text": snapshot.visible_text,
                    "element_count": len(snapshot.elements),
                },
            )
            return await self.capture(page, priority_locator=priority_locator)
        return snapshot

    def _needs_page_scope_retry(self, snapshot: BrowserAgentSnapshot) -> bool:
        visible_text = snapshot.visible_text.strip()
        if not snapshot.elements:
            return True
        if any(element.is_priority_target or element.focused for element in snapshot.elements):
            return False
        return len(snapshot.elements) <= 1 and len(visible_text) < 48


class OpenAIResponsesBrowserAgent:
    """Plan and execute browser actions with compact page snapshots."""

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        max_steps: int = 18,
        single_action_max_attempts: int = 3,
        stall_threshold: int = 3,
        snapshotter: BrowserDomSnapshotter | None = None,
        min_action_delay_ms: int = 350,
        max_action_delay_ms: int = 950,
        openai_max_retries: int = 2,
        openai_retry_max_delay_seconds: float = 20.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_steps = max_steps
        self._single_action_max_attempts = max(1, single_action_max_attempts)
        self._stall_threshold = max(2, stall_threshold)
        self._snapshotter = snapshotter or BrowserDomSnapshotter()
        self._min_action_delay_ms = max(0, min_action_delay_ms)
        self._max_action_delay_ms = max(self._min_action_delay_ms, max_action_delay_ms)
        self._openai_max_retries = max(0, openai_max_retries)
        self._openai_retry_max_delay_seconds = max(1.0, openai_retry_max_delay_seconds)

    def _append_browser_agent_log(self, relative_path: str, payload: Mapping[str, object]) -> None:
        append_output_jsonl(relative_path, payload)

    async def _clear_marked_blockers(self, page: Page) -> None:
        try:
            await page.evaluate(
                """
                () => {
                  document
                    .querySelectorAll("[data-job-applier-blocking-action]")
                    .forEach((node) =>
                      node.removeAttribute("data-job-applier-blocking-action")
                    );
                  document
                    .querySelectorAll("[data-job-applier-blocking-surface]")
                    .forEach((node) =>
                      node.removeAttribute("data-job-applier-blocking-surface")
                    );
                }
                """
            )
        except Exception:  # noqa: BLE001
            return

    async def _mark_intercepting_blocker(
        self,
        page: Page,
        locator: Locator,
    ) -> str | None:
        try:
            payload = await locator.evaluate(
                """
                (target) => {
                  const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim();
                  const isVisible = (node) => {
                    if (!node || node.nodeType !== 1) {
                      return false;
                    }
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return (
                      style.visibility !== "hidden" &&
                      style.display !== "none" &&
                      rect.width > 0 &&
                      rect.height > 0 &&
                      rect.bottom > 0 &&
                      rect.right > 0 &&
                      rect.top < window.innerHeight &&
                      rect.left < window.innerWidth
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
                  const describeNode = (node) => {
                    if (!node || node.nodeType !== 1) {
                      return "";
                    }
                    const tag = (node.tagName || "").toLowerCase();
                    const role = collapse(node.getAttribute("role"));
                    const ariaLabel = collapse(node.getAttribute("aria-label"));
                    const text = collapse(node.innerText || node.textContent || "");
                    const placeholder = collapse(node.getAttribute("placeholder"));
                    const label = ariaLabel || labelFor(node);
                    if (tag === "input" || tag === "textarea" || tag === "select") {
                      return collapse([label, placeholder, collapse(node.value)].join(" "));
                    }
                    return collapse([label, text].join(" "));
                  };
                  const describeSurface = (node) => {
                    if (!node || node.nodeType !== 1) {
                      return "";
                    }
                    const labelledBy = collapse(node.getAttribute("aria-labelledby"));
                    if (labelledBy) {
                      const heading = labelledBy
                        .split(/\\s+/)
                        .map((id) => document.getElementById(id))
                        .find(
                          (item) => item && collapse(item.innerText || item.textContent || "")
                        );
                      if (heading) {
                        return collapse(heading.innerText || heading.textContent || "");
                      }
                    }
                    const ariaLabel = collapse(node.getAttribute("aria-label"));
                    if (ariaLabel) {
                      return ariaLabel;
                    }
                    const heading = node.querySelector("h1, h2, h3, [role='heading']");
                    if (heading) {
                      const headingText = collapse(heading.innerText || heading.textContent || "");
                      if (headingText) {
                        return headingText;
                      }
                    }
                    return describeNode(node);
                  };
                  const clearMarks = () => {
                    document
                      .querySelectorAll("[data-job-applier-blocking-action]")
                      .forEach((node) =>
                        node.removeAttribute("data-job-applier-blocking-action")
                      );
                    document
                      .querySelectorAll("[data-job-applier-blocking-surface]")
                      .forEach((node) =>
                        node.removeAttribute("data-job-applier-blocking-surface")
                      );
                  };
                  const interactiveAncestor = (node) =>
                    node?.closest(
                      [
                        "button",
                        "a[href]",
                        "label",
                        "input",
                        "select",
                        "textarea",
                        "[role='button']",
                        "[role='link']",
                        "[role='menuitem']",
                        "[role='option']",
                        "[role='tab']",
                      ].join(", ")
                    ) || null;
                  const surfaceSelectors = [
                    "dialog[open]",
                    "[role='dialog'][open]",
                    "[role='dialog']",
                    "[role='alertdialog']",
                    "[aria-modal='true']",
                    "[role='listbox']",
                    "[role='menu']",
                    "[role='tooltip']",
                    "[role='status']",
                    "[role='alert']",
                    "[aria-live]",
                    "[popover]",
                    "[data-test-artdeco-toast-item]",
                    "[data-artdeco-toast-item-type]",
                    ".artdeco-toast-item",
                  ].join(", ");
                  const pickSurface = (node) => {
                    if (!node || node.nodeType !== 1) {
                      return null;
                    }
                    const semanticSurface = node.closest(surfaceSelectors);
                    if (semanticSurface && isVisible(semanticSurface)) {
                      return semanticSurface;
                    }
                    let current = node;
                    while (current && current.nodeType === 1 && current !== document.body) {
                      if (!isVisible(current)) {
                        current = current.parentElement;
                        continue;
                      }
                      const style = window.getComputedStyle(current);
                      const rect = current.getBoundingClientRect();
                      const position = style.position || "";
                      const zIndex = Number.parseInt(style.zIndex || "0", 10);
                      const looksOverlay =
                        ["fixed", "sticky", "absolute"].includes(position)
                        || Number.isFinite(zIndex) && zIndex > 0
                        || rect.width * rect.height > 80_000;
                      if (looksOverlay) {
                        return current;
                      }
                      current = current.parentElement;
                    }
                    return node.parentElement || node;
                  };
                  const rect = target.getBoundingClientRect();
                  const samplePoints = [
                    [rect.left + rect.width / 2, rect.top + rect.height / 2],
                    [rect.left + 12, rect.top + 12],
                    [rect.right - 12, rect.top + 12],
                    [rect.left + 12, rect.bottom - 12],
                    [rect.right - 12, rect.bottom - 12],
                  ]
                    .map(([x, y]) => ({
                      x: Math.max(1, Math.min(window.innerWidth - 1, Math.round(x))),
                      y: Math.max(1, Math.min(window.innerHeight - 1, Math.round(y))),
                    }))
                    .filter((point, index, collection) =>
                      collection.findIndex(
                        (candidate) => candidate.x === point.x && candidate.y === point.y
                      ) === index
                    );
                  clearMarks();
                  for (const point of samplePoints) {
                    const candidate = document.elementFromPoint(point.x, point.y);
                    if (!candidate || candidate === target || target.contains(candidate)) {
                      continue;
                    }
                    const blockingAction = interactiveAncestor(candidate) || candidate;
                    const blockingSurface = pickSurface(blockingAction);
                    if (blockingAction && blockingAction.nodeType === 1) {
                      blockingAction.setAttribute("data-job-applier-blocking-action", "true");
                    }
                    if (blockingSurface && blockingSurface.nodeType === 1) {
                      blockingSurface.setAttribute("data-job-applier-blocking-surface", "true");
                    }
                    return {
                      blocker_tag: (blockingAction?.tagName || "").toLowerCase(),
                      blocker_role: collapse(blockingAction?.getAttribute("role")),
                      blocker_action_label: describeNode(blockingAction),
                      blocker_surface_label: describeSurface(blockingSurface),
                      blocker_point: point,
                    };
                  }
                  return null;
                }
                """
            )
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, dict):
            return None
        blocker_action_label = collapse_text(
            payload.get("blocker_action_label")
            if isinstance(payload.get("blocker_action_label"), str)
            else None
        )
        blocker_surface_label = collapse_text(
            payload.get("blocker_surface_label")
            if isinstance(payload.get("blocker_surface_label"), str)
            else None
        )
        blocker_tag = collapse_text(
            payload.get("blocker_tag") if isinstance(payload.get("blocker_tag"), str) else None
        )
        blocker_role = collapse_text(
            payload.get("blocker_role") if isinstance(payload.get("blocker_role"), str) else None
        )
        summary_parts = []
        if blocker_action_label:
            summary_parts.append(f"action '{blocker_action_label}'")
        elif blocker_tag or blocker_role:
            summary_parts.append(
                f"{blocker_role} {blocker_tag}".strip() if blocker_role else blocker_tag
            )
        if blocker_surface_label:
            summary_parts.append(f"surface '{blocker_surface_label}'")
        summary = ", ".join(part for part in summary_parts if part) or None
        self._append_browser_agent_log(
            "browser-agent/blocker-trace.jsonl",
            {
                "kind": "intercepting_blocker_marked",
                "url": page.url,
                "summary": summary,
                "payload": payload,
            },
        )
        return summary

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
        execution_feedback: list[dict[str, object]] = []
        previous_snapshot_signature = ""
        repeated_snapshot_count = 0
        stall_diagnosis: BrowserStallDiagnosis | None = None

        for step_index in range(self._max_steps):
            if await is_complete(page):
                return

            snapshot = await self._snapshotter.capture(page)
            current_snapshot_signature = snapshot_signature(snapshot)
            snapshot_changed = current_snapshot_signature != previous_snapshot_signature
            repeated_snapshot_count = 1 if snapshot_changed else repeated_snapshot_count + 1
            self._append_browser_agent_log(
                "browser-agent/task-trace.jsonl",
                {
                    "kind": "task_step_snapshot",
                    "task_name": task_name,
                    "step_index": step_index,
                    "snapshot_signature": current_snapshot_signature,
                    "snapshot_changed": snapshot_changed,
                    "repeated_snapshot_count": repeated_snapshot_count,
                    "snapshot": serialize_snapshot(snapshot),
                },
            )
            if has_manual_intervention_cues(snapshot):
                if asyncio.get_running_loop().time() >= deadline:
                    break
                logger.info(
                    "linkedin_browser_agent_waiting_for_manual_intervention",
                    extra={"step_index": step_index, "task_name": task_name, "url": snapshot.url},
                )
                self._append_browser_agent_log(
                    "browser-agent/task-trace.jsonl",
                    {
                        "kind": "manual_intervention_wait",
                        "task_name": task_name,
                        "step_index": step_index,
                        "snapshot_signature": current_snapshot_signature,
                        "url": snapshot.url,
                    },
                )
                await page.wait_for_timeout(5_000)
                previous_snapshot_signature = current_snapshot_signature
                continue

            if repeated_snapshot_count >= self._stall_threshold:
                stall_diagnosis = await self._diagnose_stall(
                    snapshot=snapshot,
                    goal=goal,
                    task_name=task_name,
                    step_index=step_index,
                    recent_actions=recent_actions[-8:],
                    execution_feedback=execution_feedback[-6:],
                    repeated_snapshot_count=repeated_snapshot_count,
                )
                self._append_browser_agent_log(
                    "browser-agent/stall-trace.jsonl",
                    {
                        "kind": "task_stall_detected",
                        "task_name": task_name,
                        "step_index": step_index,
                        "snapshot_signature": current_snapshot_signature,
                        "repeated_snapshot_count": repeated_snapshot_count,
                        "diagnosis": _serialize_stall_diagnosis(stall_diagnosis),
                    },
                )
                if stall_diagnosis.status in {"manual_intervention", "abort"}:
                    raise BrowserAutomationError(stall_diagnosis.summary)

            remaining_seconds = max(1.0, deadline - asyncio.get_running_loop().time())
            action = await asyncio.wait_for(
                self._plan_action(
                    snapshot=snapshot,
                    goal=goal,
                    task_name=task_name,
                    available_values=available_values,
                    step_index=step_index,
                    recent_actions=recent_actions[-6:],
                    execution_feedback=execution_feedback[-4:],
                    snapshot_changed=snapshot_changed,
                    extra_rules=extra_rules,
                    allowed_action_types=allowed_action_types,
                    stall_diagnosis=stall_diagnosis,
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
            try:
                await self._execute_action(
                    page=page,
                    action=action,
                    values=available_values,
                    snapshot=snapshot,
                )
            except BrowserAutomationError as exc:
                feedback = {
                    "step_index": step_index,
                    "task_name": task_name,
                    "failed_action_type": action.action_type,
                    "element_id": action.element_id,
                    "action_intent": action.action_intent,
                    "message": str(exc),
                    "url": snapshot.url,
                }
                execution_feedback.append(feedback)
                self._append_browser_agent_log(
                    "browser-agent/task-trace.jsonl",
                    {
                        "kind": "task_action_failed",
                        "task_name": task_name,
                        "step_index": step_index,
                        "snapshot_signature": current_snapshot_signature,
                        "action": _serialize_action(action),
                        "feedback": feedback,
                    },
                )
                logger.info(
                    "linkedin_browser_agent_action_failed",
                    extra=feedback,
                )
                previous_snapshot_signature = ""
                continue
            try:
                post_action_snapshot = await self._snapshotter.capture(page)
                post_action_signature = snapshot_signature(post_action_snapshot)
            except Exception:  # noqa: BLE001
                post_action_signature = None
                post_action_snapshot = None
            self._append_browser_agent_log(
                "browser-agent/task-trace.jsonl",
                {
                    "kind": "task_action_succeeded",
                    "task_name": task_name,
                    "step_index": step_index,
                    "before_snapshot_signature": current_snapshot_signature,
                    "after_snapshot_signature": post_action_signature,
                    "snapshot_changed_after_action": (
                        post_action_signature is not None
                        and post_action_signature != current_snapshot_signature
                    ),
                    "action": _serialize_action(action),
                    "post_action_snapshot": (
                        serialize_snapshot(post_action_snapshot)
                        if post_action_snapshot is not None
                        else None
                    ),
                },
            )
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
            previous_snapshot_signature = current_snapshot_signature
            if (
                post_action_signature is not None
                and post_action_signature != current_snapshot_signature
            ):
                stall_diagnosis = None

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
        focus_locator: Locator | None = None,
    ) -> BrowserTaskAssessment:
        """Return a structured assessment of the current browser state."""

        snapshot = await self._snapshotter.capture(page, focus_locator=focus_locator)
        current_snapshot_signature = snapshot_signature(snapshot)
        self._append_browser_agent_log(
            "browser-agent/assessment-trace.jsonl",
            {
                "kind": "assessment_snapshot",
                "task_name": task_name,
                "step_index": step_index,
                "snapshot_signature": current_snapshot_signature,
                "snapshot": serialize_snapshot(snapshot),
            },
        )
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
        assessment = parse_browser_task_assessment(payload)
        self._append_browser_agent_log(
            "browser-agent/assessment-trace.jsonl",
            {
                "kind": "assessment_result",
                "task_name": task_name,
                "step_index": step_index,
                "snapshot_signature": current_snapshot_signature,
                "assessment": _serialize_assessment(assessment),
            },
        )
        return assessment

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
        focus_locator: Locator | None = None,
        priority_locator: Locator | None = None,
    ) -> BrowserAgentAction:
        """Plan and execute one browser action for the current page state."""

        feedback_history: list[dict[str, object]] = []
        history = list(recent_actions)
        previous_snapshot_signature = ""
        repeated_snapshot_count = 0
        stall_diagnosis: BrowserStallDiagnosis | None = None
        for attempt_index in range(self._single_action_max_attempts):
            await self._align_priority_locator_into_view(page, priority_locator)
            snapshot = await self._snapshotter.capture(
                page,
                focus_locator=focus_locator,
                priority_locator=priority_locator,
            )
            current_snapshot_signature = snapshot_signature(snapshot)
            snapshot_changed = current_snapshot_signature != previous_snapshot_signature
            repeated_snapshot_count = 1 if snapshot_changed else repeated_snapshot_count + 1
            self._append_browser_agent_log(
                "browser-agent/single-action-trace.jsonl",
                {
                    "kind": "single_action_snapshot",
                    "task_name": task_name,
                    "step_index": step_index,
                    "attempt_index": attempt_index,
                    "snapshot_signature": current_snapshot_signature,
                    "snapshot_changed": snapshot_changed,
                    "repeated_snapshot_count": repeated_snapshot_count,
                    "snapshot": serialize_snapshot(snapshot),
                },
            )
            if repeated_snapshot_count >= self._stall_threshold:
                stall_diagnosis = await self._diagnose_stall(
                    snapshot=snapshot,
                    goal=goal,
                    task_name=task_name,
                    step_index=step_index,
                    recent_actions=history[-8:],
                    execution_feedback=feedback_history[-6:],
                    repeated_snapshot_count=repeated_snapshot_count,
                )
                self._append_browser_agent_log(
                    "browser-agent/stall-trace.jsonl",
                    {
                        "kind": "single_action_stall_detected",
                        "task_name": task_name,
                        "step_index": step_index,
                        "attempt_index": attempt_index,
                        "snapshot_signature": current_snapshot_signature,
                        "repeated_snapshot_count": repeated_snapshot_count,
                        "diagnosis": _serialize_stall_diagnosis(stall_diagnosis),
                    },
                )
                if stall_diagnosis.status in {"manual_intervention", "abort"}:
                    raise BrowserAutomationError(stall_diagnosis.summary)
            action = await self._plan_action(
                snapshot=snapshot,
                goal=goal,
                task_name=task_name,
                available_values=available_values,
                step_index=step_index,
                recent_actions=history[-6:],
                execution_feedback=feedback_history[-4:],
                snapshot_changed=snapshot_changed,
                extra_rules=extra_rules,
                allowed_action_types=allowed_action_types,
                stall_diagnosis=stall_diagnosis,
            )
            logger.info(
                "linkedin_browser_agent_single_action_planned",
                extra={
                    "task_name": task_name,
                    "step_index": step_index,
                    "attempt_index": attempt_index,
                    "action_type": action.action_type,
                    "action_intent": action.action_intent,
                    "element_id": action.element_id,
                    "value_source": action.value_source,
                    "reasoning": action.reasoning,
                    "url": snapshot.url,
                },
            )
            try:
                await self._execute_action(page=page, action=action, values=available_values)
            except BrowserAutomationError as exc:
                feedback = {
                    "attempt_index": attempt_index,
                    "task_name": task_name,
                    "failed_action_type": action.action_type,
                    "element_id": action.element_id,
                    "action_intent": action.action_intent,
                    "message": str(exc),
                    "url": snapshot.url,
                }
                feedback_history.append(feedback)
                history.append(
                    {
                        "step_index": step_index,
                        "task_name": task_name,
                        "action_type": action.action_type,
                        "element_id": action.element_id,
                        "action_intent": action.action_intent,
                        "reasoning": action.reasoning,
                        "url": snapshot.url,
                        "execution_feedback": str(exc),
                    }
                )
                logger.info("linkedin_browser_agent_single_action_failed", extra=feedback)
                self._append_browser_agent_log(
                    "browser-agent/single-action-trace.jsonl",
                    {
                        "kind": "single_action_failed",
                        "task_name": task_name,
                        "step_index": step_index,
                        "attempt_index": attempt_index,
                        "snapshot_signature": current_snapshot_signature,
                        "action": _serialize_action(action),
                        "feedback": feedback,
                    },
                )
                if attempt_index >= self._single_action_max_attempts - 1:
                    raise
                previous_snapshot_signature = ""
                continue
            try:
                post_action_snapshot = await self._snapshotter.capture(
                    page,
                    focus_locator=focus_locator,
                    priority_locator=priority_locator,
                )
                post_action_signature = snapshot_signature(post_action_snapshot)
            except Exception:  # noqa: BLE001
                post_action_snapshot = None
                post_action_signature = None
            self._append_browser_agent_log(
                "browser-agent/single-action-trace.jsonl",
                {
                    "kind": "single_action_succeeded",
                    "task_name": task_name,
                    "step_index": step_index,
                    "attempt_index": attempt_index,
                    "before_snapshot_signature": current_snapshot_signature,
                    "after_snapshot_signature": post_action_signature,
                    "snapshot_changed_after_action": (
                        post_action_signature is not None
                        and post_action_signature != current_snapshot_signature
                    ),
                    "action": _serialize_action(action),
                    "post_action_snapshot": (
                        serialize_snapshot(post_action_snapshot)
                        if post_action_snapshot is not None
                        else None
                    ),
                },
            )
            if (
                post_action_signature is not None
                and post_action_signature == current_snapshot_signature
            ):
                self._append_browser_agent_log(
                    "browser-agent/single-action-trace.jsonl",
                    {
                        "kind": "single_action_no_effect",
                        "task_name": task_name,
                        "step_index": step_index,
                        "attempt_index": attempt_index,
                        "snapshot_signature": current_snapshot_signature,
                        "action": _serialize_action(action),
                        "message": "Action completed without a visible snapshot change.",
                    },
                )
            previous_snapshot_signature = current_snapshot_signature
            if (
                post_action_signature is not None
                and post_action_signature != current_snapshot_signature
            ):
                stall_diagnosis = None
            return action
        msg = f"Browser agent could not complete a safe single action for {task_name}."
        raise BrowserAutomationError(msg)

    async def _align_priority_locator_into_view(
        self,
        page: Page,
        priority_locator: Locator | None,
    ) -> None:
        if priority_locator is None:
            return
        try:
            locator = priority_locator.first
            if await locator.count() == 0:
                return
            await locator.scroll_into_view_if_needed(timeout=1_500)
            await page.wait_for_timeout(120)
        except Exception:  # noqa: BLE001
            return

    async def _plan_action(
        self,
        *,
        snapshot: BrowserAgentSnapshot,
        goal: str,
        task_name: str,
        available_values: Mapping[BrowserValueSource, str],
        step_index: int,
        recent_actions: Sequence[dict[str, object]],
        execution_feedback: Sequence[dict[str, object]],
        snapshot_changed: bool,
        extra_rules: Sequence[str],
        allowed_action_types: Sequence[BrowserActionType] | None,
        stall_diagnosis: BrowserStallDiagnosis | None = None,
    ) -> BrowserAgentAction:
        response_data = await asyncio.to_thread(
            self._create_response,
            snapshot,
            goal,
            task_name,
            available_values,
            step_index,
            recent_actions,
            execution_feedback,
            snapshot_changed,
            extra_rules,
            stall_diagnosis,
        )
        raw_output = self._extract_output_text(response_data)
        self._append_browser_agent_log(
            "llm/browser-agent.jsonl",
            {
                "kind": "planning_response",
                "task_name": task_name,
                "step_index": step_index,
                "snapshot_signature": snapshot_signature(snapshot),
                "model": self._model,
                "response_payload": response_data,
                "response_text": raw_output,
            },
        )
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
        execution_feedback: Sequence[dict[str, object]],
        snapshot_changed: bool,
        extra_rules: Sequence[str],
        stall_diagnosis: BrowserStallDiagnosis | None,
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
                "focused": element.focused,
                "invalid": element.invalid,
                "expanded": element.expanded,
                "selected": element.selected,
                "validation_text": truncate_text(element.validation_text or "", limit=120) or None,
                "is_priority_target": element.is_priority_target,
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
                "active_surface": snapshot.active_surface,
                "active_surface_scrollable": snapshot.active_surface_scrollable,
                "active_surface_can_scroll_down": snapshot.active_surface_can_scroll_down,
                "active_surface_can_scroll_up": snapshot.active_surface_can_scroll_up,
                "page_can_scroll_down": snapshot.page_can_scroll_down,
                "page_can_scroll_up": snapshot.page_can_scroll_up,
                "visible_text": snapshot.visible_text,
                "elements": elements_payload,
            },
            "available_value_sources": {
                key: (f"Use the task-owned value source {key}. Never ask for the raw value.")
                for key in available_values
                if key != "literal"
            },
            "recent_action_history": list(recent_actions),
            "execution_feedback_history": list(execution_feedback),
            "stall_diagnosis": (
                _serialize_stall_diagnosis(stall_diagnosis) if stall_diagnosis is not None else None
            ),
            "rules": [
                "Return exactly one next action.",
                "Only reference element ids that exist in the page snapshot.",
                (
                    "The element list is already ordered by visible position inside the active "
                    "surface or page. Prefer that list over guessing from broad page text."
                ),
                (
                    "If an element is marked is_priority_target=true, treat it as the main "
                    "control that this task must resolve before touching unrelated controls."
                ),
                (
                    "If page.active_surface is present, treat it as the current blocking "
                    "surface and prioritize actions inside it over the dimmed background."
                ),
                (
                    "Use scroll when the needed control is likely outside the visible portion "
                    "of the current page or active surface."
                ),
                (
                    "If page.active_surface_scrollable is true, prefer scroll_target "
                    "active_surface before scrolling the full page."
                ),
                (
                    "If a visible control already appears to advance, continue, review, or "
                    "submit the current goal, click it instead of scrolling again."
                ),
                (
                    "Use press for keyboard confirmation when a combobox, autocomplete, "
                    "or suggestion list likely needs Enter, Tab, or arrow navigation."
                ),
                (
                    "When choosing among visible suggestions or options, prefer the "
                    "semantically closest valid match to the goal even if the wording "
                    "uses abbreviations, regions, or suffixes instead of an exact string match."
                ),
                (
                    "Do not repeat the same failed click on an unchanged screen when scrolling "
                    "could reveal more content."
                ),
                "Use literal for plain text that is not sensitive.",
                (
                    "Use wait when the page is loading, animating, "
                    "or a human may need to solve verification."
                ),
                (
                    "Use recent_action_history and execution_feedback_history to avoid "
                    "repeating the same failed move on an unchanged screen."
                ),
                (
                    "When stall_diagnosis is present, treat it as the latest macro diagnosis "
                    "of why progress stopped and follow its recovery plan unless the page "
                    "now clearly shows better evidence."
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
        self._append_browser_agent_log(
            "llm/browser-agent.jsonl",
            {
                "kind": "planning_request",
                "task_name": task_name,
                "step_index": step_index,
                "snapshot_signature": snapshot_signature(snapshot),
                "model": self._model,
                "prompt_payload": prompt_payload,
            },
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
        return self._post_openai_response(
            payload_bytes=payload_bytes,
            task_name=task_name,
            mode="planning",
            log_event_name="openai_browser_agent_http_error",
        )

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
                "focused": element.focused,
                "invalid": element.invalid,
                "expanded": element.expanded,
                "selected": element.selected,
                "validation_text": truncate_text(element.validation_text or "", limit=120) or None,
                "is_priority_target": element.is_priority_target,
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
                "active_surface": snapshot.active_surface,
                "active_surface_scrollable": snapshot.active_surface_scrollable,
                "active_surface_can_scroll_down": snapshot.active_surface_can_scroll_down,
                "active_surface_can_scroll_up": snapshot.active_surface_can_scroll_up,
                "page_can_scroll_down": snapshot.page_can_scroll_down,
                "page_can_scroll_up": snapshot.page_can_scroll_up,
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
        self._append_browser_agent_log(
            "llm/browser-agent.jsonl",
            {
                "kind": "assessment_request",
                "task_name": task_name,
                "step_index": step_index,
                "snapshot_signature": snapshot_signature(snapshot),
                "model": self._model,
                "prompt_payload": prompt_payload,
            },
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
        response_payload = self._post_openai_response(
            payload_bytes=payload_bytes,
            task_name=task_name,
            mode="assessment",
            log_event_name="openai_browser_agent_assessment_http_error",
        )
        self._append_browser_agent_log(
            "llm/browser-agent.jsonl",
            {
                "kind": "assessment_response",
                "task_name": task_name,
                "step_index": step_index,
                "snapshot_signature": snapshot_signature(snapshot),
                "model": self._model,
                "response_payload": response_payload,
            },
        )
        return response_payload

    async def _diagnose_stall(
        self,
        *,
        snapshot: BrowserAgentSnapshot,
        goal: str,
        task_name: str,
        step_index: int,
        recent_actions: Sequence[dict[str, object]],
        execution_feedback: Sequence[dict[str, object]],
        repeated_snapshot_count: int,
    ) -> BrowserStallDiagnosis:
        response_data = await asyncio.to_thread(
            self._create_stall_diagnosis_response,
            snapshot,
            goal,
            task_name,
            step_index,
            repeated_snapshot_count,
            recent_actions,
            execution_feedback,
        )
        raw_output = self._extract_output_text(response_data)
        self._append_browser_agent_log(
            "llm/browser-agent.jsonl",
            {
                "kind": "stall_diagnosis_response",
                "task_name": task_name,
                "step_index": step_index,
                "snapshot_signature": snapshot_signature(snapshot),
                "model": self._model,
                "response_payload": response_data,
                "response_text": raw_output,
            },
        )
        logger.info(
            "linkedin_browser_agent_stall_diagnosis_response",
            extra={"model": self._model, "task_name": task_name, "response_text": raw_output},
        )
        if not raw_output:
            msg = "Browser agent returned an empty stall diagnosis."
            raise BrowserAutomationError(msg)
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            msg = "Browser agent returned invalid JSON for the stall diagnosis."
            raise BrowserAutomationError(msg) from exc
        return parse_browser_stall_diagnosis(payload)

    def _create_stall_diagnosis_response(
        self,
        snapshot: BrowserAgentSnapshot,
        goal: str,
        task_name: str,
        step_index: int,
        repeated_snapshot_count: int,
        recent_actions: Sequence[dict[str, object]],
        execution_feedback: Sequence[dict[str, object]],
    ) -> dict[str, object]:
        prompt_payload = {
            "goal": goal,
            "task_name": task_name,
            "step_index": step_index,
            "repeated_snapshot_count": repeated_snapshot_count,
            "page": serialize_snapshot(snapshot),
            "recent_action_history": list(recent_actions),
            "execution_feedback_history": list(execution_feedback),
            "rules": [
                "Diagnose why progress appears stalled on the current browser snapshot.",
                "Use recoverable when the task can still continue with a better plan.",
                "Use manual_intervention when a human checkpoint is likely required.",
                "Use abort when no safe next move is available from the current surface.",
                "Keep blocker_category short and machine-friendly.",
                "next_plan must be a short ordered recovery plan, not prose.",
                "Base the diagnosis on the visible page plus the recent failed history.",
            ],
        }
        self._append_browser_agent_log(
            "llm/browser-agent.jsonl",
            {
                "kind": "stall_diagnosis_request",
                "task_name": task_name,
                "step_index": step_index,
                "snapshot_signature": snapshot_signature(snapshot),
                "model": self._model,
                "prompt_payload": prompt_payload,
            },
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
                                "You are diagnosing why a LinkedIn browser task is stalled. "
                                "Return a compact machine-readable diagnosis and a short "
                                "recovery plan."
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
                    "name": "browser_agent_stall_diagnosis",
                    "schema": STRUCTURED_STALL_DIAGNOSIS_SCHEMA,
                    "strict": True,
                },
            },
        }
        payload_bytes = json.dumps(body, ensure_ascii=True).encode("utf-8")
        return self._post_openai_response(
            payload_bytes=payload_bytes,
            task_name=task_name,
            mode="assessment",
            log_event_name="openai_browser_agent_stall_diagnosis_http_error",
        )

    def _post_openai_response(
        self,
        *,
        payload_bytes: bytes,
        task_name: str,
        mode: Literal["planning", "assessment"],
        log_event_name: str,
    ) -> dict[str, object]:
        for attempt in range(self._openai_max_retries + 1):
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
                error_body = exc.read().decode("utf-8", errors="replace")
                retry_delay_seconds = estimate_openai_retry_delay_seconds(
                    status=exc.code,
                    body=error_body,
                    retry_after_header=exc.headers.get("Retry-After"),
                    max_delay_seconds=self._openai_retry_max_delay_seconds,
                )
                logger.warning(
                    log_event_name,
                    extra={
                        "status": exc.code,
                        "body": error_body,
                        "attempt": attempt + 1,
                        "max_attempts": self._openai_max_retries + 1,
                        "retry_delay_seconds": retry_delay_seconds,
                    },
                )
                append_output_jsonl(
                    "run.log",
                    {
                        "source": "browser_agent",
                        "kind": "openai_http_error",
                        "mode": mode,
                        "task_name": task_name,
                        "status": exc.code,
                        "attempt": attempt + 1,
                        "max_attempts": self._openai_max_retries + 1,
                        "retry_delay_seconds": retry_delay_seconds,
                        "body_excerpt": truncate_text(error_body, limit=320),
                    },
                )
                self._append_browser_agent_log(
                    "llm/browser-agent.jsonl",
                    {
                        "kind": "openai_http_error",
                        "mode": mode,
                        "task_name": task_name,
                        "status": exc.code,
                        "attempt": attempt + 1,
                        "max_attempts": self._openai_max_retries + 1,
                        "retry_delay_seconds": retry_delay_seconds,
                        "body_excerpt": truncate_text(error_body, limit=320),
                    },
                )
                if exc.code == 429 and attempt < self._openai_max_retries:
                    time.sleep(retry_delay_seconds)
                    continue
                raise BrowserAutomationError(
                    summarize_openai_responses_error(
                        status=exc.code,
                        body=error_body,
                        task_name=task_name,
                        mode=mode,
                    )
                ) from exc
            except error.URLError as exc:
                msg = (
                    "Could not reach the OpenAI Responses API for the LinkedIn browser "
                    f"{'agent' if mode == 'planning' else 'assessor'}. Reason: {exc.reason}"
                )
                raise BrowserAutomationError(msg) from exc
        msg = "OpenAI Responses API request loop exited unexpectedly."
        raise BrowserAutomationError(msg)

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
        snapshot: BrowserAgentSnapshot | None = None,
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
        locator: Locator | None = None
        if action.action_type == "press":
            await self._clear_marked_blockers(page)
            if action.key_name is None:
                msg = "Browser agent returned press without a key_name."
                raise BrowserAutomationError(msg)
            await self._pause_before_click(page)
            try:
                await page.keyboard.press(action.key_name)
                if action.key_name in {"Enter", "Tab", "Space"}:
                    await self._settle_page(page)
                else:
                    await page.wait_for_timeout(350)
                return
            except Exception as exc:  # noqa: BLE001
                msg = summarize_browser_action_error(exc)
                raise BrowserAutomationError(msg) from exc
        if action.action_type == "scroll":
            await self._clear_marked_blockers(page)
            await self._pause_before_click(page)
            await self._execute_scroll(page, action)
            await page.wait_for_timeout(350)
            return

        try:
            if action.action_type == "click":
                await self._clear_marked_blockers(page)
                locator = self._locator_for_action(page, action)
                await locator.scroll_into_view_if_needed()
                await self._pause_before_click(page)
                await locator.click()
                await self._settle_page(page)
                return

            if action.action_type == "fill":
                await self._clear_marked_blockers(page)
                locator = await self._resolve_fill_locator(
                    page,
                    action=action,
                    snapshot=snapshot,
                )
                await locator.scroll_into_view_if_needed()
                value = self._resolve_fill_value(action, values)
                if await self._locator_is_select_like(locator):
                    await self._select_option_for_fill(locator, value)
                else:
                    await self._fill_text_like_locator(page, locator, value)
                await page.wait_for_timeout(350)
                return
        except Exception as exc:  # noqa: BLE001
            blocker_summary = None
            if locator is not None and _error_looks_like_intercepted_pointer(exc):
                blocker_summary = await self._mark_intercepting_blocker(page, locator)
            msg = summarize_browser_action_error(exc, blocker_summary=blocker_summary)
            raise BrowserAutomationError(msg) from exc

    async def _execute_scroll(self, page: Page, action: BrowserAgentAction) -> None:
        direction = action.scroll_direction or "down"
        amount = max(100, min(1_600, action.scroll_amount or 550))
        delta = amount if direction == "down" else -amount
        try:
            if action.scroll_target == "active_surface":
                locator = page.locator(
                    '[data-job-applier-active-surface-scroll-target="true"]'
                ).first
                if await locator.count() == 0:
                    msg = "No scrollable active surface is available for the requested action."
                    raise BrowserAutomationError(msg)
                await locator.evaluate(
                    """
                    (node, scrollDelta) => {
                      if (node && node.nodeType === 1 && typeof node.scrollBy === "function") {
                        node.scrollBy({ top: scrollDelta, behavior: "instant" });
                      }
                    }
                    """,
                    delta,
                )
                return
            await page.mouse.wheel(0, delta)
        except BrowserAutomationError:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = summarize_browser_action_error(exc)
            raise BrowserAutomationError(msg) from exc

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

    def _element_is_fillable_candidate(self, element: BrowserAgentElement) -> bool:
        tag = element.tag.lower()
        input_type = (element.input_type or "").lower()
        role = (element.role or "").lower()
        if tag in {"textarea", "select"}:
            return True
        if tag == "input":
            return input_type not in {"hidden", "radio", "checkbox", "button", "submit"}
        return role in {"textbox", "combobox"}

    def _resolve_contextual_fill_element_id(
        self,
        snapshot: BrowserAgentSnapshot | None,
    ) -> str | None:
        if snapshot is None:
            return None
        priority_candidates = [
            element
            for element in snapshot.elements
            if (
                element.is_priority_target
                and not element.disabled
                and self._element_is_fillable_candidate(element)
            )
        ]
        if len(priority_candidates) == 1:
            return priority_candidates[0].element_id
        focused_candidates = [
            element
            for element in snapshot.elements
            if (
                element.focused
                and not element.disabled
                and self._element_is_fillable_candidate(element)
            )
        ]
        if len(focused_candidates) == 1:
            return focused_candidates[0].element_id
        return None

    def _snapshot_element_for_action(
        self,
        snapshot: BrowserAgentSnapshot | None,
        action: BrowserAgentAction,
    ) -> BrowserAgentElement | None:
        element_id = action.element_id or self._resolve_contextual_fill_element_id(snapshot)
        if snapshot is None or element_id is None:
            return None
        for element in snapshot.elements:
            if element.element_id == element_id:
                return element
        return None

    async def _resolve_fill_locator(
        self,
        page: Page,
        *,
        action: BrowserAgentAction,
        snapshot: BrowserAgentSnapshot | None,
    ) -> Locator:
        element_id = action.element_id or self._resolve_contextual_fill_element_id(snapshot)
        if element_id is None:
            msg = "Browser agent returned fill without a usable target element."
            raise BrowserAutomationError(msg)
        locator = page.locator(f'[data-job-applier-agent-id="{element_id}"]').first
        if await self._locator_is_fillable(locator):
            return locator

        snapshot_element = self._snapshot_element_for_action(snapshot, action)
        resolved_token = await page.evaluate(
            """
            ({ elementId, expectedLabel, expectedName, expectedPlaceholder, expectedRole,
               expectedInputType, expectedText, expectedCandidateLabel }) => {
              const collapse = (value) => (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
              const isVisible = (node) => {
                if (!node || node.nodeType !== 1) {
                  return false;
                }
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                if (
                  rect.width <= 0 ||
                  rect.height <= 0 ||
                  rect.bottom <= 0 ||
                  rect.right <= 0 ||
                  rect.top >= window.innerHeight ||
                  rect.left >= window.innerWidth
                ) {
                  return false;
                }
                return style.visibility !== "hidden" && style.display !== "none";
              };
              const splitTokens = (value) =>
                collapse(value)
                  .split(/[^a-z0-9]+/)
                  .map((item) => item.trim())
                  .filter(Boolean);
              const tokenOverlapScore = (left, right) => {
                const leftTokens = splitTokens(left);
                const rightTokens = splitTokens(right);
                if (leftTokens.length === 0 || rightTokens.length === 0) {
                  return 0;
                }
                const rightSet = new Set(rightTokens);
                let overlap = 0;
                for (const token of leftTokens) {
                  if (rightSet.has(token)) {
                    overlap += 1;
                  }
                }
                return overlap;
              };
              const labelFor = (node) => {
                const labels = Array.from(node.labels || []);
                const joined = labels
                  .map(
                    (item) =>
                      (item.innerText || item.textContent || "")
                        .replace(/\\s+/g, " ")
                        .trim()
                  )
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
                return (label?.innerText || label?.textContent || "").replace(/\\s+/g, " ").trim();
              };
              const isFillable = (node) => {
                if (!node || node.nodeType !== 1) {
                  return false;
                }
                if (!isVisible(node)) {
                  return false;
                }
                const tag = (node.tagName || "").toLowerCase();
                const type = collapse(node.getAttribute("type"));
                const role = collapse(node.getAttribute("role"));
                if (tag === "textarea" || tag === "select") {
                  return true;
                }
                if (tag === "input") {
                  return !["hidden", "radio", "checkbox", "button", "submit"].includes(type);
                }
                if (node.isContentEditable) {
                  return true;
                }
                return role === "textbox" || role === "combobox";
              };
              const fillableSelectors = [
                "input",
                "textarea",
                "select",
                "[contenteditable='true']",
                "[role='textbox']",
                "[role='combobox']",
              ].join(", ");
              const candidates = [];
              const seenNodes = new Set();
              const addCandidate = (node, baseScore) => {
                if (!node || seenNodes.has(node) || !isFillable(node)) {
                  return;
                }
                seenNodes.add(node);
                const label = (
                  labelFor(node) || node.getAttribute("aria-label") || ""
                ).replace(/\\s+/g, " ").trim();
                const placeholder = (
                  node.getAttribute("placeholder") || ""
                ).replace(/\\s+/g, " ").trim();
                const name = (node.getAttribute("name") || "").replace(/\\s+/g, " ").trim();
                const role = (node.getAttribute("role") || "").replace(/\\s+/g, " ").trim();
                const inputType = (node.getAttribute("type") || "")
                  .replace(/\\s+/g, " ")
                  .trim();
                const candidateLabel = [
                  label,
                  placeholder,
                  name,
                  node.innerText || node.textContent || "",
                ].join(" ").replace(/\\s+/g, " ").trim();
                let score = baseScore;
                if (collapse(label) && collapse(label) === collapse(expectedLabel)) {
                  score += 18;
                } else {
                  score += tokenOverlapScore(label, expectedLabel) * 4;
                }
                if (collapse(name) && collapse(name) === collapse(expectedName)) {
                  score += 12;
                }
                if (
                  collapse(placeholder)
                  && collapse(placeholder) === collapse(expectedPlaceholder)
                ) {
                  score += 10;
                } else {
                  score += tokenOverlapScore(placeholder, expectedPlaceholder) * 3;
                }
                if (collapse(role) && collapse(role) === collapse(expectedRole)) {
                  score += 6;
                }
                if (collapse(inputType) && collapse(inputType) === collapse(expectedInputType)) {
                  score += 4;
                }
                score += tokenOverlapScore(candidateLabel, expectedCandidateLabel) * 2;
                score += tokenOverlapScore(candidateLabel, expectedText) * 2;
                candidates.push({ node, score });
              };

              document
                .querySelectorAll("[data-job-applier-resolved-fill-target]")
                .forEach((node) => node.removeAttribute("data-job-applier-resolved-fill-target"));

              const directNode = document.querySelector(
                `[data-job-applier-agent-id="${elementId}"]`
              );
              if (directNode) {
                addCandidate(directNode, 100);
                for (const candidate of directNode.querySelectorAll(fillableSelectors)) {
                  addCandidate(candidate, 90);
                }
              }

              const activeSurface = document.querySelector(
                "[data-job-applier-active-surface='true']"
              );
              const scopeRoot = activeSurface || document;
              for (const candidate of scopeRoot.querySelectorAll(fillableSelectors)) {
                addCandidate(candidate, 0);
              }

              candidates.sort((left, right) => right.score - left.score);
              const best = candidates[0]?.node;
              if (!best) {
                return null;
              }
              const token = `${elementId}-resolved-fill`;
              best.setAttribute("data-job-applier-resolved-fill-target", token);
              return token;
            }
            """,
            {
                "elementId": element_id,
                "expectedLabel": snapshot_element.label if snapshot_element is not None else "",
                "expectedName": snapshot_element.name if snapshot_element is not None else "",
                "expectedPlaceholder": (
                    snapshot_element.placeholder if snapshot_element is not None else ""
                ),
                "expectedRole": snapshot_element.role if snapshot_element is not None else "",
                "expectedInputType": (
                    snapshot_element.input_type if snapshot_element is not None else ""
                ),
                "expectedText": snapshot_element.text if snapshot_element is not None else "",
                "expectedCandidateLabel": (
                    snapshot_element.candidate_label if snapshot_element is not None else ""
                ),
            },
        )
        if isinstance(resolved_token, str) and resolved_token.strip():
            return page.locator(
                f'[data-job-applier-resolved-fill-target="{resolved_token.strip()}"]'
            ).first
        return locator

    async def _locator_is_fillable(self, locator: Locator) -> bool:
        try:
            if await locator.count() == 0:
                return False
            return bool(
                await locator.evaluate(
                    """
                    (node) => {
                      if (!node || node.nodeType !== 1) {
                        return false;
                      }
                      const tag = (node.tagName || "").toLowerCase();
                      const type = (node.getAttribute("type") || "").toLowerCase();
                      const role = (node.getAttribute("role") || "").toLowerCase();
                      if (tag === "textarea" || tag === "select") {
                        return true;
                      }
                      if (tag === "input") {
                        return !["hidden", "radio", "checkbox", "button", "submit"].includes(type);
                      }
                      if (node.isContentEditable) {
                        return true;
                      }
                      return role === "textbox" || role === "combobox";
                    }
                    """
                )
            )
        except Exception:  # noqa: BLE001
            return False

    async def _locator_is_select_like(self, locator: Locator) -> bool:
        try:
            if await locator.count() == 0:
                return False
            return bool(
                await locator.evaluate(
                    """
                    (node) => {
                      if (!node || node.nodeType !== 1) {
                        return false;
                      }
                      const tag = (node.tagName || "").toLowerCase();
                      const role = (node.getAttribute("role") || "").toLowerCase();
                      return tag === "select" || role === "listbox";
                    }
                    """
                )
            )
        except Exception:  # noqa: BLE001
            return False

    async def _select_option_for_fill(self, locator: Locator, value: str) -> None:
        option_choice = await locator.evaluate(
            """
            (node, requestedValue) => {
              const collapse = (input) => (input || "").replace(/\\s+/g, " ").trim().toLowerCase();
              const tokenize = (input) =>
                collapse(input)
                  .split(/[^a-z0-9]+/)
                  .map((item) => item.trim())
                  .filter(Boolean);
              const overlapScore = (left, right) => {
                const leftTokens = tokenize(left);
                const rightTokens = tokenize(right);
                if (leftTokens.length === 0 || rightTokens.length === 0) {
                  return 0;
                }
                const rightSet = new Set(rightTokens);
                let overlap = 0;
                for (const token of leftTokens) {
                  if (rightSet.has(token)) {
                    overlap += 1;
                  }
                }
                return overlap;
              };
              const requested = collapse(requestedValue);
              const options = Array.from(node.options || []);
              let best = null;
              for (const option of options) {
                const label = (option.label || option.textContent || "")
                  .replace(/\\s+/g, " ")
                  .trim();
                const optionValue = (option.value || "")
                  .replace(/\\s+/g, " ")
                  .trim();
                const normalizedLabel = collapse(label);
                const normalizedValue = collapse(optionValue);
                let score = -1;
                if (!normalizedLabel && !normalizedValue) {
                  continue;
                }
                if (normalizedLabel === requested) {
                  score = 120;
                } else if (normalizedValue === requested) {
                  score = 118;
                } else if (
                  requested &&
                  normalizedLabel &&
                  (normalizedLabel.includes(requested) || requested.includes(normalizedLabel))
                ) {
                  score = 96;
                } else if (
                  requested &&
                  normalizedValue &&
                  (normalizedValue.includes(requested) || requested.includes(normalizedValue))
                ) {
                  score = 92;
                } else {
                  const labelOverlap = overlapScore(label, requestedValue);
                  const valueOverlap = overlapScore(optionValue, requestedValue);
                  score = Math.max(labelOverlap * 10, valueOverlap * 8);
                }
                if (!best || score > best.score) {
                  best = {
                    index: option.index,
                    value: optionValue,
                    score,
                  };
                }
              }
              if (!best || best.score <= 0) {
                return null;
              }
              return best;
            }
            """,
            value,
        )
        if not isinstance(option_choice, dict):
            msg = f"No selectable option matched the requested value: {value!r}"
            raise BrowserAutomationError(msg)
        option_value = option_choice.get("value")
        option_index = option_choice.get("index")
        if isinstance(option_value, str) and option_value:
            await locator.select_option(value=option_value)
            return
        if isinstance(option_index, int):
            await locator.select_option(index=option_index)
            return
        msg = f"No selectable option matched the requested value: {value!r}"
        raise BrowserAutomationError(msg)

    async def _fill_text_like_locator(self, page: Page, locator: Locator, value: str) -> None:
        """Fill text inputs with a more human-like interaction pattern.

        Some LinkedIn controls ignore or immediately reset `fill()` values. Focusing the field,
        clearing it, and typing sequentially produces browser events closer to a human user while
        still keeping the execution primitive generic.
        """

        await self._pause_before_click(page)
        await locator.click()
        try:
            await locator.clear()
        except Exception:  # noqa: BLE001
            try:
                await locator.press("ControlOrMeta+A")
                await locator.press("Backspace")
            except Exception:  # noqa: BLE001
                pass
        try:
            per_key_delay_ms = random.randint(35, 95)
            await locator.press_sequentially(value, delay=per_key_delay_ms)
        except Exception:  # noqa: BLE001
            await locator.fill(value)

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
        if action.action_type == "click" and action.element_id not in valid_element_ids:
            msg = "Browser agent referenced an element that does not exist in the snapshot."
            raise BrowserAutomationError(msg)
        if action.action_type == "fill":
            resolved_element_id = action.element_id or self._resolve_contextual_fill_element_id(
                snapshot
            )
            if resolved_element_id not in valid_element_ids:
                msg = "Browser agent referenced an element that does not exist in the snapshot."
                raise BrowserAutomationError(msg)
        if action.action_type == "press" and action.key_name not in {
            "Enter",
            "Tab",
            "Escape",
            "ArrowDown",
            "ArrowUp",
            "Space",
        }:
            msg = "Browser agent returned press without a supported key_name."
            raise BrowserAutomationError(msg)
        if action.action_type == "scroll":
            if action.scroll_target not in {"active_surface", "page"}:
                msg = "Browser agent returned scroll without a valid scroll_target."
                raise BrowserAutomationError(msg)
            if action.scroll_direction not in {"down", "up"}:
                msg = "Browser agent returned scroll without a valid scroll_direction."
                raise BrowserAutomationError(msg)
            if action.scroll_target == "active_surface" and not snapshot.active_surface_scrollable:
                msg = "Browser agent tried to scroll an active surface that is not scrollable."
                raise BrowserAutomationError(msg)
            if (
                action.scroll_target == "active_surface"
                and action.scroll_direction == "down"
                and not snapshot.active_surface_can_scroll_down
            ):
                msg = "Browser agent tried to scroll down an active surface with no room below."
                raise BrowserAutomationError(msg)
            if (
                action.scroll_target == "active_surface"
                and action.scroll_direction == "up"
                and not snapshot.active_surface_can_scroll_up
            ):
                msg = "Browser agent tried to scroll up an active surface with no room above."
                raise BrowserAutomationError(msg)
            if (
                action.scroll_target == "page"
                and action.scroll_direction == "down"
                and not snapshot.page_can_scroll_down
            ):
                msg = "Browser agent tried to scroll down the page with no room below."
                raise BrowserAutomationError(msg)
            if (
                action.scroll_target == "page"
                and action.scroll_direction == "up"
                and not snapshot.page_can_scroll_up
            ):
                msg = "Browser agent tried to scroll up the page with no room above."
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


def summarize_openai_responses_error(
    *,
    status: int,
    body: str,
    task_name: str,
    mode: Literal["planning", "assessment"],
) -> str:
    """Return a clearer OpenAI Responses API error for browser-agent failures."""

    task_label = task_name.replace("_", " ")
    if status == 429:
        return (
            "OpenAI Responses API rate limit while the LinkedIn browser agent was "
            f"{mode} {task_label}. This is not a LinkedIn page-rate-limit signal."
        )
    if status >= 500:
        return (
            "OpenAI Responses API failed while the LinkedIn browser agent was "
            f"{mode} {task_label}. Status: {status}."
        )
    excerpt = truncate_text(body, limit=220)
    return (
        "OpenAI Responses API returned an error while the LinkedIn browser agent was "
        f"{mode} {task_label}. Status: {status}. Details: {excerpt}"
    )


def estimate_openai_retry_delay_seconds(
    *,
    status: int,
    body: str,
    retry_after_header: str | None,
    max_delay_seconds: float,
) -> float:
    """Estimate a safe retry delay for OpenAI Responses API throttling."""

    if status != 429:
        return 1.0
    retry_candidates: list[float] = []
    if retry_after_header:
        try:
            retry_candidates.append(float(retry_after_header.strip()))
        except ValueError:
            pass
    retry_match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", body, re.I)
    if retry_match:
        retry_candidates.append(float(retry_match.group(1)))
    base_delay = max(retry_candidates, default=8.0)
    jitter = random.uniform(0.25, 1.25)
    return max(1.0, min(max_delay_seconds, base_delay + jitter))


def parse_browser_action(payload: dict[str, object]) -> BrowserAgentAction:
    """Validate the structured action returned by the browser planner."""

    action_type = payload.get("action_type")
    if action_type not in {"click", "fill", "press", "scroll", "wait", "done", "fail"}:
        msg = "Browser agent returned an unsupported action_type."
        raise BrowserAutomationError(msg)

    value_source = payload.get("value_source")
    if value_source is not None and not isinstance(value_source, str):
        msg = "Browser agent returned an unsupported value_source."
        raise BrowserAutomationError(msg)

    key_name = payload.get("key_name")
    if key_name is not None and key_name not in {
        "Enter",
        "Tab",
        "Escape",
        "ArrowDown",
        "ArrowUp",
        "Space",
    }:
        msg = "Browser agent returned an unsupported key_name."
        raise BrowserAutomationError(msg)

    scroll_target = payload.get("scroll_target")
    if scroll_target is not None and scroll_target not in {"active_surface", "page"}:
        msg = "Browser agent returned an unsupported scroll_target."
        raise BrowserAutomationError(msg)

    scroll_direction = payload.get("scroll_direction")
    if scroll_direction is not None and scroll_direction not in {"down", "up"}:
        msg = "Browser agent returned an unsupported scroll_direction."
        raise BrowserAutomationError(msg)

    scroll_amount_raw = payload.get("scroll_amount", 550)
    if not isinstance(scroll_amount_raw, (int, float, str)):
        msg = "Browser agent returned an invalid scroll_amount value."
        raise BrowserAutomationError(msg)
    try:
        scroll_amount = int(scroll_amount_raw)
    except (TypeError, ValueError) as exc:
        msg = "Browser agent returned an invalid scroll_amount value."
        raise BrowserAutomationError(msg) from exc

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
        key_name=cast(BrowserPressKey | None, _optional_text(key_name)),
        scroll_target=cast(BrowserScrollTarget | None, _optional_text(scroll_target)),
        scroll_direction=cast(BrowserScrollDirection | None, _optional_text(scroll_direction)),
        scroll_amount=max(100, min(1_600, scroll_amount)),
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


def parse_browser_stall_diagnosis(payload: dict[str, object]) -> BrowserStallDiagnosis:
    """Validate the structured stall diagnosis returned by the browser planner."""

    status = payload.get("status")
    if status not in {"recoverable", "manual_intervention", "abort"}:
        msg = "Browser agent returned an unsupported stall diagnosis status."
        raise BrowserAutomationError(msg)

    blocker_category = collapse_text(str(payload.get("blocker_category") or ""))
    if not blocker_category:
        blocker_category = "unknown_blocker"

    next_plan_raw = payload.get("next_plan", ())
    next_plan_items = next_plan_raw if isinstance(next_plan_raw, list) else ()
    next_plan = tuple(
        item.strip() for item in next_plan_items if isinstance(item, str) and item.strip()
    )

    evidence_raw = payload.get("evidence", ())
    evidence_items = evidence_raw if isinstance(evidence_raw, list) else ()
    evidence = tuple(
        item.strip() for item in evidence_items if isinstance(item, str) and item.strip()
    )

    return BrowserStallDiagnosis(
        status=cast(BrowserStallStatus, status),
        summary=str(payload.get("summary") or "").strip(),
        blocker_category=blocker_category,
        next_plan=next_plan,
        evidence=evidence,
    )


def _error_looks_like_intercepted_pointer(error: Exception) -> bool:
    message = collapse_text(str(error)).lower()
    return (
        "intercepts pointer events" in message
        or "another element would receive the click" in message
    )


def summarize_browser_action_error(
    error: Exception,
    *,
    blocker_summary: str | None = None,
) -> str:
    """Return a planner-friendly summary for browser action failures."""

    message = collapse_text(str(error))
    lowered = message.lower()
    if _error_looks_like_intercepted_pointer(error):
        if blocker_summary:
            return (
                "The chosen target is blocked by another visible element. "
                f"Observed blocker: {truncate_text(blocker_summary, limit=140)}."
            )
        return "The chosen target is blocked by an open dialog or overlay."
    if "element is not attached" in lowered:
        return "The chosen target disappeared before the action could finish."
    if "element is not visible" in lowered:
        return "The chosen target is no longer visible."
    if "timeout" in lowered:
        return "The chosen action timed out before the page accepted it."
    return truncate_text(message, limit=220)


def _optional_text(value: object) -> str | None:
    text = collapse_text(value if isinstance(value, str) else None)
    return text or None
