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
                        for (const attributeName of ["aria-describedby", "aria-errormessage"]) {
                          for (const id of splitIds(fieldNode.getAttribute(attributeName))) {
                            pushText(document.getElementById(id));
                          }
                        }
                        let ancestor = fieldNode.parentElement;
                        let depth = 0;
                        while (ancestor && depth < 4) {
                          for (const candidate of ancestor.querySelectorAll(
                            "[role='alert'], [aria-live='assertive'], [aria-live='polite']"
                          )) {
                            pushText(candidate);
                          }
                          ancestor = ancestor.parentElement;
                          depth += 1;
                        }
                        for (const candidate of document.querySelectorAll(
                          "div, p, span, small, li"
                        )) {
                          pushText(candidate);
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
                      const activeSurface = focusedNode && focusedNode.nodeType === 1
                        ? {
                            node: focusedNode,
                            label: describeSurface(focusedNode),
                          }
                        : null;
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
                      const nodes = activeSurface
                        ? [
                            ...Array.from(activeSurface.node.querySelectorAll(interactiveSelectors)),
                            ...relatedPopupNodes,
                          ]
                            .filter((node) => node && node.nodeType === 1)
                            .filter(isVisible)
                            .filter((node, index, collection) => collection.indexOf(node) === index)
                            .sort((left, right) => {
                              if (priorityField) {
                                if (left === priorityField && right !== priorityField) {
                                  return -1;
                                }
                                if (right === priorityField && left !== priorityField) {
                                  return 1;
                                }
                              }
                              const leftRect = left.getBoundingClientRect();
                              const rightRect = right.getBoundingClientRect();
                              if (Math.abs(leftRect.top - rightRect.top) > 4) {
                                return leftRect.top - rightRect.top;
                              }
                              return leftRect.left - rightRect.left;
                            })
                        : [];
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
                        });
                        if (items.length >= maxElements) {
                          break;
                        }
                      }

                      const visibleText = collapse(
                        activeSurface
                          ? [
                              describeSurface(activeSurface.node),
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
            ({ interactiveSelectors, maxElements }) => {
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
              const focusedSurfaceNode =
                null;
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
              if (activeSurface) {
                activeSurface.node.setAttribute("data-job-applier-active-surface", "true");
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
              const activeScrollTarget = activeSurface
                ? findActiveScrollTarget(activeSurface.node)
                : null;
              if (activeScrollTarget) {
                activeScrollTarget.setAttribute(
                  "data-job-applier-active-surface-scroll-target",
                  "true",
                );
              }
              const mainRoot = document.querySelector(mainSelectors);
              const scopeRoot = activeSurface ? activeSurface.node : (mainRoot || document);
              const nodes = Array.from(scopeRoot.querySelectorAll(interactiveSelectors));
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

              const visibleText = collapse(
                activeSurface ? activeSurface.node.innerText : (document.body?.innerText || ""),
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
                page_can_scroll_down: pageScrollTop + pageClientHeight < pageScrollHeight - 8,
                page_can_scroll_up: pageScrollTop > 8,
              };
            }
            """,
                    {
                        "interactiveSelectors": INTERACTIVE_SELECTORS,
                        "maxElements": self._max_elements,
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
            active_surface=_optional_text(raw_payload.get("active_surface")),
            active_surface_scrollable=bool(raw_payload.get("active_surface_scrollable")),
            active_surface_can_scroll_down=bool(raw_payload.get("active_surface_can_scroll_down")),
            active_surface_can_scroll_up=bool(raw_payload.get("active_surface_can_scroll_up")),
            page_can_scroll_down=bool(raw_payload.get("page_can_scroll_down")),
            page_can_scroll_up=bool(raw_payload.get("page_can_scroll_up")),
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
        openai_max_retries: int = 2,
        openai_retry_max_delay_seconds: float = 20.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_steps = max_steps
        self._snapshotter = snapshotter or BrowserDomSnapshotter()
        self._min_action_delay_ms = max(0, min_action_delay_ms)
        self._max_action_delay_ms = max(self._min_action_delay_ms, max_action_delay_ms)
        self._openai_max_retries = max(0, openai_max_retries)
        self._openai_retry_max_delay_seconds = max(1.0, openai_retry_max_delay_seconds)

    def _append_browser_agent_log(self, relative_path: str, payload: Mapping[str, object]) -> None:
        append_output_jsonl(relative_path, payload)

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

        for step_index in range(self._max_steps):
            if await is_complete(page):
                return

            snapshot = await self._snapshotter.capture(page)
            current_snapshot_signature = snapshot_signature(snapshot)
            snapshot_changed = current_snapshot_signature != previous_snapshot_signature
            self._append_browser_agent_log(
                "browser-agent/task-trace.jsonl",
                {
                    "kind": "task_step_snapshot",
                    "task_name": task_name,
                    "step_index": step_index,
                    "snapshot_signature": current_snapshot_signature,
                    "snapshot_changed": snapshot_changed,
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
                await self._execute_action(page=page, action=action, values=available_values)
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
        for attempt_index in range(3):
            snapshot = await self._snapshotter.capture(
                page,
                focus_locator=focus_locator,
                priority_locator=priority_locator,
            )
            current_snapshot_signature = snapshot_signature(snapshot)
            self._append_browser_agent_log(
                "browser-agent/single-action-trace.jsonl",
                {
                    "kind": "single_action_snapshot",
                    "task_name": task_name,
                    "step_index": step_index,
                    "attempt_index": attempt_index,
                    "snapshot_signature": current_snapshot_signature,
                    "snapshot": serialize_snapshot(snapshot),
                },
            )
            action = await self._plan_action(
                snapshot=snapshot,
                goal=goal,
                task_name=task_name,
                available_values=available_values,
                step_index=step_index,
                recent_actions=history[-6:],
                execution_feedback=feedback_history[-4:],
                snapshot_changed=True,
                extra_rules=extra_rules,
                allowed_action_types=allowed_action_types,
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
                if attempt_index >= 2:
                    raise
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
            return action
        msg = f"Browser agent could not complete a safe single action for {task_name}."
        raise BrowserAutomationError(msg)

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
        if action.action_type == "press":
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
            await self._pause_before_click(page)
            await self._execute_scroll(page, action)
            await page.wait_for_timeout(350)
            return

        locator = self._locator_for_action(page, action)
        try:
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
        except Exception as exc:  # noqa: BLE001
            msg = summarize_browser_action_error(exc)
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


def summarize_browser_action_error(error: Exception) -> str:
    """Return a planner-friendly summary for browser action failures."""

    message = collapse_text(str(error))
    lowered = message.lower()
    if "intercepts pointer events" in lowered:
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
