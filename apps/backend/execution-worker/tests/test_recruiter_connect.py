from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

from job_applier.domain.entities import JobPosting
from job_applier.domain.enums import Platform, RecruiterInteractionStatus
from job_applier.infrastructure.linkedin.recruiter_connect import (
    GeneratedRecruiterMessage,
    PlaywrightRecruiterConnector,
    RecruiterCandidate,
    RecruiterMessageGenerator,
)
from job_applier.settings import RuntimeSettings


class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, *, counts: dict[str, int]) -> None:
        self._counts = counts
        self.timeout_ms: int | None = None
        self.gotos: list[str] = []
        self.screenshots: list[str] = []
        self.closed = False

    def get_by_role(self, _role: str, *, name) -> _FakeLocator:  # type: ignore[no-untyped-def]
        pattern = getattr(name, "pattern", str(name))
        return _FakeLocator(self._counts.get(pattern, 0))

    def get_by_text(self, name) -> _FakeLocator:  # type: ignore[no-untyped-def]
        pattern = getattr(name, "pattern", str(name))
        return _FakeLocator(self._counts.get(pattern, 0))

    def set_default_timeout(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    async def goto(self, url: str, *, wait_until: str) -> None:
        self.gotos.append(f"{url}|{wait_until}")

    async def screenshot(self, *, path: str, full_page: bool) -> None:
        self.screenshots.append(f"{path}|{full_page}")

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


class _FakeMessageGenerator:
    def __init__(self) -> None:
        self.generate = AsyncMock(
            return_value=GeneratedRecruiterMessage(message="hello recruiter", source="ai")
        )


class RecruiterConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_status_ignores_message_button(self) -> None:
        connector = PlaywrightRecruiterConnector(RuntimeSettings())
        page = _FakePage(counts={r"message": 1})

        status = await connector._existing_status(cast(Any, page))

        self.assertIsNone(status)

    async def test_existing_status_marks_pending_invitation_as_skipped(self) -> None:
        connector = PlaywrightRecruiterConnector(RuntimeSettings())
        page = _FakePage(counts={r"pending": 1})

        status = await connector._existing_status(cast(Any, page))

        self.assertEqual(status, RecruiterInteractionStatus.SKIPPED)

    async def test_connect_skips_message_generation_when_existing_status_detected(self) -> None:
        generator = _FakeMessageGenerator()
        connector = PlaywrightRecruiterConnector(
            RuntimeSettings(),
            message_generator=cast(RecruiterMessageGenerator, generator),
        )
        page = _FakePage(counts={})
        context = _FakeContext(page)
        connector._detect_existing_status = AsyncMock(  # type: ignore[method-assign]
            return_value=(RecruiterInteractionStatus.SKIPPED, "text:pending")
        )
        connector._open_connect_flow = AsyncMock(return_value="direct_button")  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await connector.connect(
                cast(Any, context),
                recruiter=RecruiterCandidate(
                    name="Alex Recruiter",
                    profile_url="https://www.linkedin.com/in/alex/",
                ),
                settings=cast(Any, SimpleNamespace(profile=SimpleNamespace(name="Thiago"))),
                posting=JobPosting(
                    platform=Platform.LINKEDIN,
                    url="https://www.linkedin.com/jobs/view/1/",
                    external_job_id="1",
                    title="Backend Engineer",
                    company_name="Acme",
                    description_raw="Backend Engineer at Acme",
                    easy_apply=True,
                ),
                submission_id=uuid4(),
                screenshot_path=Path(temp_dir) / "shot.png",
            )

        generator.generate.assert_not_awaited()
        self.assertEqual(result.interaction.status, RecruiterInteractionStatus.SKIPPED)
        self.assertEqual(result.result_reason, "existing_status")

    async def test_connect_skips_message_generation_when_connect_is_unavailable(self) -> None:
        generator = _FakeMessageGenerator()
        connector = PlaywrightRecruiterConnector(
            RuntimeSettings(),
            message_generator=cast(RecruiterMessageGenerator, generator),
        )
        page = _FakePage(counts={})
        context = _FakeContext(page)
        connector._detect_existing_status = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
        connector._open_connect_flow = AsyncMock(return_value=None)  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as temp_dir:
            result = await connector.connect(
                cast(Any, context),
                recruiter=RecruiterCandidate(
                    name="Alex Recruiter",
                    profile_url="https://www.linkedin.com/in/alex/",
                ),
                settings=cast(Any, SimpleNamespace(profile=SimpleNamespace(name="Thiago"))),
                posting=JobPosting(
                    platform=Platform.LINKEDIN,
                    url="https://www.linkedin.com/jobs/view/1/",
                    external_job_id="1",
                    title="Backend Engineer",
                    company_name="Acme",
                    description_raw="Backend Engineer at Acme",
                    easy_apply=True,
                ),
                submission_id=uuid4(),
                screenshot_path=Path(temp_dir) / "shot.png",
            )

        generator.generate.assert_not_awaited()
        self.assertEqual(result.interaction.status, RecruiterInteractionStatus.SKIPPED)
        self.assertEqual(result.result_reason, "connect_unavailable")


if __name__ == "__main__":
    unittest.main()
