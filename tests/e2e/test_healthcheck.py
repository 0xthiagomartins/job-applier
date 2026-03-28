import asyncio

from httpx import ASGITransport, AsyncClient

from job_applier.main import app


def test_healthcheck_returns_ok() -> None:
    async def exercise_healthcheck() -> tuple[int, dict[str, str]]:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/health")
            return response.status_code, response.json()

    status_code, payload = asyncio.run(exercise_healthcheck())

    assert status_code == 200
    assert payload == {"status": "ok"}
