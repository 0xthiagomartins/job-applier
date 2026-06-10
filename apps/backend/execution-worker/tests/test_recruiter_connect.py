from __future__ import annotations

import unittest
from typing import Any, cast

from job_applier.domain.enums import RecruiterInteractionStatus
from job_applier.infrastructure.linkedin.recruiter_connect import PlaywrightRecruiterConnector
from job_applier.settings import RuntimeSettings


class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, *, counts: dict[str, int]) -> None:
        self._counts = counts

    def get_by_role(self, _role: str, *, name) -> _FakeLocator:  # type: ignore[no-untyped-def]
        pattern = getattr(name, "pattern", str(name))
        return _FakeLocator(self._counts.get(pattern, 0))

    def get_by_text(self, name) -> _FakeLocator:  # type: ignore[no-untyped-def]
        pattern = getattr(name, "pattern", str(name))
        return _FakeLocator(self._counts.get(pattern, 0))


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


if __name__ == "__main__":
    unittest.main()
