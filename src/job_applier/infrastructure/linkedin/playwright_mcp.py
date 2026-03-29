"""HTTP client and LLM planner for the Playwright MCP server."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib import error, parse, request

from pydantic import SecretStr

from job_applier.observability import append_output_jsonl, write_output_text

logger = logging.getLogger(__name__)

McpActionType = Literal["click", "type", "wait", "done", "fail"]
McpValueSource = Literal["literal", "linkedin_email", "linkedin_password"]
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_INITIALIZE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action_type": {
            "type": "string",
            "enum": ["click", "type", "wait", "done", "fail"],
        },
        "ref": {
            "type": ["string", "null"],
            "description": "Exact target ref from the Playwright MCP page snapshot.",
        },
        "element": {
            "type": ["string", "null"],
            "description": "Short human-readable description of the element.",
        },
        "value_source": {
            "type": ["string", "null"],
            "enum": ["literal", "linkedin_email", "linkedin_password", None],
        },
        "value": {
            "type": ["string", "null"],
            "description": "Literal text to type when value_source is literal.",
        },
        "wait_seconds": {
            "type": "integer",
            "minimum": 0,
            "maximum": 20,
        },
        "reasoning": {
            "type": "string",
            "description": "Short explanation for audit logs.",
        },
    },
    "required": [
        "action_type",
        "ref",
        "element",
        "value_source",
        "value",
        "wait_seconds",
        "reasoning",
    ],
}
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


class PlaywrightMcpError(RuntimeError):
    """Raised when the Playwright MCP handshake or tool calls fail."""


class PlaywrightMcpSessionNotFoundError(PlaywrightMcpError):
    """Raised when the MCP server no longer recognizes the current session."""


@dataclass(frozen=True, slots=True)
class PlaywrightMcpAction:
    """One browser step planned from an MCP accessibility snapshot."""

    action_type: McpActionType
    ref: str | None
    element: str | None
    value_source: McpValueSource | None
    value: str | None
    wait_seconds: int
    reasoning: str


class PlaywrightMcpClient(Protocol):
    """Minimal browser control contract shared by HTTP and stdio transports."""

    async def initialize(self) -> None:
        """Prepare the transport session."""

    async def shutdown(self) -> None:
        """Tear down the transport session."""

    async def navigate(self, url: str) -> None:
        """Navigate the browser to one URL."""

    async def snapshot(self) -> str:
        """Return the current accessibility snapshot."""

    async def click(self, *, ref: str, element: str | None = None) -> None:
        """Click one snapshot ref."""

    async def type(
        self,
        *,
        ref: str,
        text: str,
        element: str | None = None,
        submit: bool = False,
    ) -> None:
        """Type text into one snapshot ref."""

    async def wait(self, *, seconds: int) -> None:
        """Wait in the remote browser."""

    async def save_storage_state(self, path: Path) -> None:
        """Export storage state."""

    async def close_browser(self) -> None:
        """Close the remote browser."""


def normalize_playwright_mcp_url(url: str) -> str:
    """Normalize root URLs so the Python client always talks to `/mcp`."""

    normalized = url.strip()
    if not normalized:
        msg = "Playwright MCP URL cannot be empty."
        raise ValueError(msg)
    parsed = parse.urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        msg = "Playwright MCP URL must start with http:// or https://."
        raise ValueError(msg)
    hostname = parsed.hostname or ""
    if hostname in {"127.0.0.1", "0.0.0.0"}:
        host = "localhost"
        if parsed.port is not None:
            netloc = f"{host}:{parsed.port}"
        else:
            netloc = host
        parsed = parsed._replace(netloc=netloc)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/mcp"
    elif path not in {"/mcp", "/sse"}:
        path = f"{path}/mcp"
    rebuilt = parsed._replace(path=path, params="", query="", fragment="")
    return parse.urlunparse(rebuilt)


def is_local_playwright_mcp_url(url: str) -> bool:
    """Return whether the configured MCP endpoint points to the local machine."""

    parsed = parse.urlparse(normalize_playwright_mcp_url(url))
    return (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "0.0.0.0"}


def collapse_text(value: str | None) -> str:
    """Collapse repeated whitespace for prompts and heuristics."""

    return re.sub(r"\s+", " ", value or "").strip()


def truncate_text(value: str, *, limit: int) -> str:
    """Keep long snapshots inside a predictable token budget."""

    collapsed = collapse_text(value)
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1].rstrip()}…"


def has_manual_intervention_cues(snapshot_text: str) -> bool:
    """Return whether the current MCP snapshot looks like captcha or checkpoint."""

    normalized = snapshot_text.lower()
    return any(pattern in normalized for pattern in MANUAL_INTERVENTION_PATTERNS)


class PlaywrightMcpHttpClient:
    """Minimal HTTP client for the official Playwright MCP server."""

    def __init__(self, *, base_url: str, timeout_seconds: int = 30) -> None:
        self._base_url = normalize_playwright_mcp_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._session_id: str | None = None
        self._initialized = False
        self._request_id = 0

    async def initialize(self) -> None:
        """Create one MCP session and notify the server the client is ready."""

        if self._initialized:
            return
        await asyncio.to_thread(
            self._send_jsonrpc,
            {
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "job-applier",
                        "version": "0.1.0",
                    },
                },
            },
            True,
        )
        session_id = self._session_id
        if not session_id:
            msg = "Playwright MCP did not return a session id."
            raise PlaywrightMcpError(msg)
        await asyncio.to_thread(
            self._send_jsonrpc,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            False,
        )
        self._initialized = True

    async def shutdown(self) -> None:
        """Close the HTTP MCP session when supported by the server."""

        if self._session_id is None:
            return
        await asyncio.to_thread(self._delete_session)
        self._session_id = None
        self._initialized = False

    async def navigate(self, url: str) -> None:
        """Navigate the remote browser to one URL."""

        await self.call_tool("browser_navigate", {"url": url})

    async def snapshot(self) -> str:
        """Return the current accessibility snapshot as plain text."""

        result = await self.call_tool("browser_snapshot", {})
        return extract_mcp_text_content(result)

    async def click(self, *, ref: str, element: str | None = None) -> None:
        """Click one ref from the last snapshot."""

        arguments: dict[str, object] = {"ref": ref}
        if element:
            arguments["element"] = element
        await self.call_tool("browser_click", arguments)

    async def type(
        self,
        *,
        ref: str,
        text: str,
        element: str | None = None,
        submit: bool = False,
    ) -> None:
        """Type text into one MCP snapshot target."""

        arguments: dict[str, object] = {"ref": ref, "text": text}
        if element:
            arguments["element"] = element
        if submit:
            arguments["submit"] = True
        await self.call_tool("browser_type", arguments)

    async def wait(self, *, seconds: int) -> None:
        """Wait inside the MCP-driven browser session."""

        await self.call_tool("browser_wait_for", {"time": seconds})

    async def save_storage_state(self, path: Path) -> None:
        """Export the authenticated browser context into a storage-state file."""

        target_path = str(path.resolve())
        code = (
            "async (page) => {"
            f" await page.context().storageState({{ path: {json.dumps(target_path)} }});"
            " return 'storage_state_saved';"
            " }"
        )
        await self.call_tool("browser_run_code", {"code": code})

    async def close_browser(self) -> None:
        """Close the remote browser session after exporting state."""

        if not self._initialized:
            return
        try:
            await self.call_tool("browser_close", {})
        except PlaywrightMcpSessionNotFoundError:
            return
        except PlaywrightMcpError:
            logger.exception("playwright_mcp_browser_close_failed")

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Call one MCP tool and return the result payload."""

        await self.initialize()
        response_body = await self._call_tool_once(name, arguments)
        result = response_body.get("result")
        if not isinstance(result, dict):
            msg = f"Playwright MCP tool {name!r} returned an unexpected response."
            raise PlaywrightMcpError(msg)
        return cast(dict[str, object], result)

    async def _call_tool_once(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        response = await asyncio.to_thread(
            self._send_jsonrpc,
            {
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments,
                },
            },
            True,
        )
        return cast(dict[str, object], response["body"])

    def _send_jsonrpc(
        self,
        body: dict[str, object],
        expect_response: bool,
    ) -> dict[str, object]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        sanitized_request = _sanitize_mcp_transport_payload(body)
        logger.info(
            "playwright_mcp_request",
            extra={
                "base_url": self._base_url,
                "request_body": sanitized_request,
                "session_id": self._session_id,
            },
        )
        append_output_jsonl(
            "mcp/traffic.jsonl",
            {
                "direction": "request",
                "base_url": self._base_url,
                "request_body": sanitized_request,
                "headers": _sanitize_mcp_headers(headers),
                "session_id": self._session_id,
            },
        )
        append_output_jsonl(
            "run.log",
            {
                "source": "playwright_mcp",
                "kind": "request",
                "base_url": self._base_url,
                "request_body": sanitized_request,
                "headers": _sanitize_mcp_headers(headers),
                "session_id": self._session_id,
            },
        )

        http_request = request.Request(
            self._base_url,
            data=json.dumps(body, ensure_ascii=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=self._timeout_seconds) as response:  # noqa: S310
                response_body = response.read().decode("utf-8", errors="replace")
                session_id = response.headers.get("Mcp-Session-Id")
                append_output_jsonl(
                    "mcp/traffic.jsonl",
                    {
                        "direction": "response",
                        "status": getattr(response, "status", 200),
                        "session_id": session_id or self._session_id,
                        "body": response_body,
                    },
                )
                append_output_jsonl(
                    "run.log",
                    {
                        "source": "playwright_mcp",
                        "kind": "response",
                        "status": getattr(response, "status", 200),
                        "session_id": session_id or self._session_id,
                        "body_excerpt": truncate_text(response_body, limit=1_500),
                    },
                )
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            append_output_jsonl(
                "mcp/traffic.jsonl",
                {
                    "direction": "error",
                    "status": exc.code,
                    "session_id": self._session_id,
                    "body": detail,
                },
            )
            append_output_jsonl(
                "run.log",
                {
                    "source": "playwright_mcp",
                    "kind": "error",
                    "status": exc.code,
                    "session_id": self._session_id,
                    "body_excerpt": truncate_text(detail, limit=1_500),
                },
            )
            if exc.code == 404 and "Session not found" in detail:
                self._session_id = None
                self._initialized = False
                raise PlaywrightMcpSessionNotFoundError(detail) from exc
            msg = f"Playwright MCP request failed with status {exc.code}: {detail}"
            raise PlaywrightMcpError(msg) from exc
        except error.URLError as exc:
            msg = f"Could not reach Playwright MCP at {self._base_url}: {exc.reason}"
            raise PlaywrightMcpError(msg) from exc

        if session_id:
            self._session_id = session_id
        logger.info(
            "playwright_mcp_response",
            extra={
                "base_url": self._base_url,
                "session_id": self._session_id,
                "response_excerpt": truncate_text(response_body, limit=1_500),
            },
        )

        parsed_body = parse_mcp_response_body(response_body) if expect_response else {}
        if "error" in parsed_body:
            error_payload = parsed_body["error"]
            msg = f"Playwright MCP returned an error: {json.dumps(error_payload)}"
            raise PlaywrightMcpError(msg)
        return {"body": parsed_body, "session_id": session_id}

    def _delete_session(self) -> None:
        headers = {"Accept": "application/json"}
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        http_request = request.Request(
            self._base_url,
            headers=headers,
            method="DELETE",
        )
        try:
            with request.urlopen(http_request, timeout=self._timeout_seconds):  # noqa: S310
                return
        except Exception:  # noqa: BLE001
            logger.exception("playwright_mcp_session_delete_failed")

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id


class PlaywrightMcpStdioClient:
    """Subprocess-backed MCP client that avoids the flaky local HTTP session transport."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        timeout_seconds: int = 30,
    ) -> None:
        if not command:
            msg = "Playwright MCP stdio command cannot be empty."
            raise ValueError(msg)
        self._command = tuple(command)
        self._timeout_seconds = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._initialized = False
        self._request_id = 0

    async def initialize(self) -> None:
        """Launch the subprocess and complete the MCP initialize handshake."""

        if self._initialized:
            return
        await self._ensure_process()
        response = await self._send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "job-applier",
                        "version": "0.1.0",
                    },
                },
            },
            expect_response=True,
        )
        if not isinstance(response, dict):
            msg = "Playwright MCP stdio initialize returned an invalid payload."
            raise PlaywrightMcpError(msg)
        await self._send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            expect_response=False,
        )
        self._initialized = True

    async def shutdown(self) -> None:
        """Terminate the subprocess session."""

        process = self._process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        self._process = None
        self._stderr_task = None
        self._initialized = False

    async def navigate(self, url: str) -> None:
        await self.call_tool("browser_navigate", {"url": url})

    async def snapshot(self) -> str:
        result = await self.call_tool("browser_snapshot", {})
        return extract_mcp_text_content(result)

    async def click(self, *, ref: str, element: str | None = None) -> None:
        arguments: dict[str, object] = {"ref": ref}
        if element:
            arguments["element"] = element
        await self.call_tool("browser_click", arguments)

    async def type(
        self,
        *,
        ref: str,
        text: str,
        element: str | None = None,
        submit: bool = False,
    ) -> None:
        arguments: dict[str, object] = {"ref": ref, "text": text}
        if element:
            arguments["element"] = element
        if submit:
            arguments["submit"] = True
        await self.call_tool("browser_type", arguments)

    async def wait(self, *, seconds: int) -> None:
        await self.call_tool("browser_wait_for", {"time": seconds})

    async def save_storage_state(self, path: Path) -> None:
        target_path = str(path.resolve())
        code = (
            "async (page) => {"
            f" await page.context().storageState({{ path: {json.dumps(target_path)} }});"
            " return 'storage_state_saved';"
            " }"
        )
        await self.call_tool("browser_run_code", {"code": code})

    async def close_browser(self) -> None:
        if not self._initialized:
            return
        try:
            await self.call_tool("browser_close", {})
        except PlaywrightMcpError:
            logger.exception("playwright_mcp_stdio_browser_close_failed")

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        await self.initialize()
        response = await self._send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments,
                },
            },
            expect_response=True,
        )
        if not isinstance(response, dict):
            msg = f"Playwright MCP stdio tool {name!r} returned an invalid response."
            raise PlaywrightMcpError(msg)
        result = response.get("result")
        if not isinstance(result, dict):
            msg = f"Playwright MCP stdio tool {name!r} returned an unexpected response."
            raise PlaywrightMcpError(msg)
        return cast(dict[str, object], result)

    async def _ensure_process(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        logger.info("playwright_mcp_stdio_start", extra={"command": self._command})
        append_output_jsonl(
            "mcp/stdio.jsonl",
            {
                "event": "process_start",
                "command": list(self._command),
            },
        )
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._pump_stderr(), name="playwright-mcp-stderr")

    async def _pump_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            logger.info("playwright_mcp_stdio_stderr", extra={"line": text})
            append_output_jsonl(
                "mcp/stdio.jsonl",
                {
                    "event": "stderr",
                    "line": text,
                },
            )
            append_output_jsonl(
                "run.log",
                {
                    "source": "playwright_mcp_stdio",
                    "kind": "stderr",
                    "line": text,
                },
            )

    async def _send_jsonrpc(
        self,
        body: dict[str, object],
        *,
        expect_response: bool,
    ) -> dict[str, object] | None:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            msg = "Playwright MCP stdio process is not available."
            raise PlaywrightMcpError(msg)
        sanitized_request = _sanitize_mcp_transport_payload(body)
        append_output_jsonl(
            "mcp/stdio.jsonl",
            {
                "event": "request",
                "body": sanitized_request,
            },
        )
        append_output_jsonl(
            "run.log",
            {
                "source": "playwright_mcp_stdio",
                "kind": "request",
                "body": sanitized_request,
            },
        )
        process.stdin.write(f"{json.dumps(body, ensure_ascii=True)}\n".encode())
        await process.stdin.drain()
        if not expect_response:
            return None
        expected_id = body.get("id")
        while True:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=self._timeout_seconds,
                )
            except TimeoutError as exc:
                msg = "Timed out waiting for Playwright MCP stdio response."
                raise PlaywrightMcpError(msg) from exc
            if not line:
                msg = "Playwright MCP stdio process closed unexpectedly."
                raise PlaywrightMcpError(msg)
            payload = json.loads(line.decode("utf-8"))
            if not isinstance(payload, dict):
                msg = "Playwright MCP stdio returned a non-object JSON payload."
                raise PlaywrightMcpError(msg)
            append_output_jsonl(
                "mcp/stdio.jsonl",
                {
                    "event": "response",
                    "body": payload,
                },
            )
            append_output_jsonl(
                "run.log",
                {
                    "source": "playwright_mcp_stdio",
                    "kind": "response",
                    "body": payload,
                },
            )
            payload_id = payload.get("id")
            if expected_id is not None and payload_id != expected_id:
                continue
            error_payload = payload.get("error")
            if isinstance(error_payload, dict):
                msg = f"Playwright MCP stdio returned an error: {json.dumps(error_payload)}"
                raise PlaywrightMcpError(msg)
            return cast(dict[str, object], payload)

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id


class OpenAIResponsesPlaywrightMcpAgent:
    """Plan login actions from Playwright MCP snapshots using the Responses API."""

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, *, api_key: SecretStr, model: str, max_steps: int = 24) -> None:
        self._api_key = api_key
        self._model = model
        self._max_steps = max_steps

    async def complete_linkedin_login(
        self,
        *,
        client: PlaywrightMcpClient,
        credentials: dict[McpValueSource, str],
        timeout_seconds: int,
    ) -> None:
        """Drive the MCP browser until the model concludes the login is complete."""

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        recent_actions: list[dict[str, object]] = []
        previous_snapshot_excerpt = ""
        for step_index in range(self._max_steps):
            snapshot_text = await client.snapshot()
            snapshot_excerpt = truncate_text(snapshot_text, limit=1_200)
            snapshot_changed = snapshot_excerpt != previous_snapshot_excerpt
            write_output_text(f"mcp/snapshots/step-{step_index:02d}.txt", snapshot_text)
            append_output_jsonl(
                "mcp/timeline.jsonl",
                {
                    "step_index": step_index,
                    "kind": "snapshot",
                    "snapshot_excerpt": snapshot_excerpt,
                    "snapshot_changed": snapshot_changed,
                },
            )

            remaining_seconds = max(1.0, deadline - asyncio.get_running_loop().time())
            action = await asyncio.wait_for(
                self._plan_action(
                    snapshot_text=snapshot_text,
                    step_index=step_index,
                    recent_actions=recent_actions[-6:],
                    snapshot_changed=snapshot_changed,
                ),
                timeout=remaining_seconds,
            )
            logger.info(
                "playwright_mcp_action_planned",
                extra={
                    "step_index": step_index,
                    "action_type": action.action_type,
                    "ref": action.ref,
                    "element": action.element,
                    "value_source": action.value_source,
                    "reasoning": action.reasoning,
                },
            )
            append_output_jsonl(
                "mcp/timeline.jsonl",
                {
                    "step_index": step_index,
                    "kind": "planned_action",
                    "action_type": action.action_type,
                    "ref": action.ref,
                    "element": action.element,
                    "value_source": action.value_source,
                    "reasoning": action.reasoning,
                },
            )
            if action.action_type == "done":
                await client.wait(seconds=1)
                return
            if action.action_type == "fail":
                msg = action.reasoning or "Playwright MCP agent reported no safe next step."
                raise PlaywrightMcpError(msg)
            if action.action_type == "wait":
                await client.wait(seconds=max(1, action.wait_seconds))
                recent_actions.append(
                    {
                        "step_index": step_index,
                        "action_type": action.action_type,
                        "reasoning": action.reasoning,
                        "snapshot_excerpt": snapshot_excerpt,
                    }
                )
                previous_snapshot_excerpt = snapshot_excerpt
                continue
            if action.action_type == "click":
                if action.ref is None:
                    msg = "Playwright MCP agent returned click without a ref."
                    raise PlaywrightMcpError(msg)
                await client.click(ref=action.ref, element=action.element)
                recent_actions.append(
                    {
                        "step_index": step_index,
                        "action_type": action.action_type,
                        "ref": action.ref,
                        "element": action.element,
                        "reasoning": action.reasoning,
                        "snapshot_excerpt": snapshot_excerpt,
                    }
                )
                previous_snapshot_excerpt = snapshot_excerpt
                continue
            if action.action_type == "type":
                if action.ref is None:
                    msg = "Playwright MCP agent returned type without a ref."
                    raise PlaywrightMcpError(msg)
                await client.type(
                    ref=action.ref,
                    element=action.element,
                    text=self._resolve_text(action, credentials),
                )
                recent_actions.append(
                    {
                        "step_index": step_index,
                        "action_type": action.action_type,
                        "ref": action.ref,
                        "element": action.element,
                        "value_source": action.value_source,
                        "reasoning": action.reasoning,
                        "snapshot_excerpt": snapshot_excerpt,
                    }
                )
                previous_snapshot_excerpt = snapshot_excerpt
                continue

        msg = "Playwright MCP agent exhausted the login flow before completion."
        raise PlaywrightMcpError(msg)

    async def _plan_action(
        self,
        *,
        snapshot_text: str,
        step_index: int,
        recent_actions: list[dict[str, object]],
        snapshot_changed: bool,
    ) -> PlaywrightMcpAction:
        response_data = await asyncio.to_thread(
            self._create_response,
            snapshot_text,
            step_index,
            recent_actions,
            snapshot_changed,
        )
        raw_output = extract_output_text(response_data)
        logger.info(
            "playwright_mcp_agent_response",
            extra={"model": self._model, "response_text": raw_output},
        )
        append_output_jsonl(
            "llm/login-planner.jsonl",
            {
                "kind": "response_text",
                "step_index": step_index,
                "model": self._model,
                "response_text": raw_output,
            },
        )
        if not raw_output:
            msg = "Playwright MCP agent returned an empty response."
            raise PlaywrightMcpError(msg)
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            msg = "Playwright MCP agent returned invalid JSON."
            raise PlaywrightMcpError(msg) from exc
        return parse_playwright_mcp_action(cast(dict[str, object], payload))

    def _create_response(
        self,
        snapshot_text: str,
        step_index: int,
        recent_actions: list[dict[str, object]],
        snapshot_changed: bool,
    ) -> dict[str, object]:
        prompt_payload = {
            "goal": "Log into LinkedIn successfully.",
            "step_index": step_index,
            "snapshot_changed_since_last_step": snapshot_changed,
            "snapshot_markdown": truncate_text(snapshot_text, limit=5_500),
            "recent_action_history": recent_actions,
            "available_value_sources": {
                "linkedin_email": "Use the user's LinkedIn email or phone field.",
                "linkedin_password": "Use the user's LinkedIn password field.",
            },
            "rules": [
                "Return exactly one next action.",
                "Use the exact ref that appears in the MCP snapshot.",
                "Use type for text inputs and click for buttons, links, checkboxes, and radios.",
                (
                    "Use linkedin_email and linkedin_password for credential fields. "
                    "Never ask for raw secrets."
                ),
                (
                    "Reason carefully before acting. Prefer the next highest-confidence "
                    "action over guessing."
                ),
                (
                    "Use recent_action_history to avoid repeating the same failed move "
                    "on an unchanged snapshot."
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
                "Use wait when the page is loading or a human may need to solve verification.",
                "Use done only when the login flow appears complete.",
                "Use fail only when no safe next action exists.",
            ],
        }
        logger.info(
            "playwright_mcp_agent_prompt",
            extra={"model": self._model, "prompt_payload": prompt_payload},
        )
        body = {
            "model": self._model,
            "reasoning": {"effort": "high"},
            "input": [
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "You are controlling LinkedIn login through Playwright MCP. "
                                "The snapshot is an accessibility tree with refs. "
                                "Pick the safest next action. Think in terms of login progression, "
                                "recent attempts, and whether the page actually changed."
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
                    "name": "playwright_mcp_login_action",
                    "schema": MCP_INITIALIZE_SCHEMA,
                    "strict": True,
                },
            },
        }
        append_output_jsonl(
            "llm/login-planner.jsonl",
            {
                "kind": "request",
                "step_index": step_index,
                "model": self._model,
                "payload": body,
            },
        )
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
                payload = cast(dict[str, object], json.loads(response.read().decode("utf-8")))
                append_output_jsonl(
                    "llm/login-planner.jsonl",
                    {
                        "kind": "response_payload",
                        "step_index": step_index,
                        "model": self._model,
                        "payload": payload,
                    },
                )
                return payload
        except error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "openai_playwright_mcp_agent_http_error",
                extra={"status": exc.code, "body": error_body},
            )
            append_output_jsonl(
                "llm/login-planner.jsonl",
                {
                    "kind": "http_error",
                    "step_index": step_index,
                    "status": exc.code,
                    "body": error_body,
                },
            )
            raise

    def _resolve_text(
        self,
        action: PlaywrightMcpAction,
        credentials: dict[McpValueSource, str],
    ) -> str:
        if action.value_source == "literal":
            if action.value is None:
                msg = "Playwright MCP agent returned literal type without a value."
                raise PlaywrightMcpError(msg)
            return action.value
        if action.value_source in credentials:
            return credentials[action.value_source]
        msg = "Playwright MCP agent returned an unsupported value source."
        raise PlaywrightMcpError(msg)


def extract_mcp_text_content(result: Mapping[str, object]) -> str:
    """Return joined text from the standard MCP `content` array."""

    content_items = result.get("content", ())
    if not isinstance(content_items, list):
        return ""
    texts = [
        item.get("text", "")
        for item in content_items
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return collapse_text("\n".join(text for text in texts if isinstance(text, str)))


def extract_output_text(response_data: dict[str, object]) -> str:
    """Extract `output_text` from a Responses API payload."""

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


def parse_mcp_response_body(body: str) -> dict[str, object]:
    """Parse either plain JSON or SSE-wrapped JSON from the MCP HTTP transport."""

    payload = body.strip()
    if not payload:
        return {}
    if payload.startswith("{"):
        return cast(dict[str, object], json.loads(payload))

    data_lines = [
        line.removeprefix("data:").strip()
        for line in payload.splitlines()
        if line.startswith("data:")
    ]
    for item in reversed(data_lines):
        if not item:
            continue
        return cast(dict[str, object], json.loads(item))
    msg = "Could not parse the Playwright MCP HTTP response body."
    raise PlaywrightMcpError(msg)


def parse_playwright_mcp_action(payload: dict[str, object]) -> PlaywrightMcpAction:
    """Validate one structured action returned by the login planner."""

    action_type = payload.get("action_type")
    if action_type not in {"click", "type", "wait", "done", "fail"}:
        msg = "Playwright MCP agent returned an unsupported action_type."
        raise PlaywrightMcpError(msg)

    value_source = payload.get("value_source")
    if value_source not in {"literal", "linkedin_email", "linkedin_password", None}:
        msg = "Playwright MCP agent returned an unsupported value_source."
        raise PlaywrightMcpError(msg)

    wait_seconds_raw = payload.get("wait_seconds", 0)
    if not isinstance(wait_seconds_raw, (int, float, str)):
        msg = "Playwright MCP agent returned an invalid wait_seconds value."
        raise PlaywrightMcpError(msg)
    try:
        wait_seconds = int(wait_seconds_raw)
    except (TypeError, ValueError) as exc:
        msg = "Playwright MCP agent returned an invalid wait_seconds value."
        raise PlaywrightMcpError(msg) from exc

    ref = _optional_text(payload.get("ref"))
    if action_type in {"click", "type"} and ref is None:
        msg = "Playwright MCP agent returned an action without a ref."
        raise PlaywrightMcpError(msg)

    return PlaywrightMcpAction(
        action_type=cast(McpActionType, action_type),
        ref=ref,
        element=_optional_text(payload.get("element")),
        value_source=cast(McpValueSource | None, value_source),
        value=_optional_text(payload.get("value")),
        wait_seconds=max(0, wait_seconds),
        reasoning=str(payload.get("reasoning") or "").strip(),
    )


def _optional_text(value: object) -> str | None:
    text = collapse_text(value if isinstance(value, str) else None)
    return text or None


def _sanitize_mcp_transport_payload(payload: Mapping[str, object]) -> dict[str, object]:
    sanitized = dict(payload)
    params = sanitized.get("params")
    if not isinstance(params, dict):
        return sanitized
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return sanitized
    sanitized_arguments = dict(arguments)
    if "text" in sanitized_arguments:
        sanitized_arguments["text"] = "[redacted]"
    params = dict(params)
    params["arguments"] = sanitized_arguments
    sanitized["params"] = params
    return sanitized


def _sanitize_mcp_headers(headers: Mapping[str, str]) -> dict[str, str]:
    sanitized = dict(headers)
    if "Authorization" in sanitized:
        sanitized["Authorization"] = "[redacted]"
    return sanitized
