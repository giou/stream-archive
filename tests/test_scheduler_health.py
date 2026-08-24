import asyncio

from aiohttp import ClientSession

from stream_archive.scheduler import _HEALTH_HOST, _HEALTH_PORT, _start_health_server


def test_health_defaults_are_loopback_and_fixed_port():
    assert _HEALTH_HOST == "127.0.0.1"
    assert _HEALTH_PORT == 9100


def test_healthz_serves_ok():
    async def scenario():
        runner = await _start_health_server(port=0)  # ephemeral port: immune to collisions
        assert runner is not None
        try:
            host, port = runner.addresses[0][:2]
            async with ClientSession() as session, session.get(f"http://{host}:{port}/healthz") as resp:
                assert resp.status == 200
                assert await resp.text() == "ok"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_health_bind_failure_returns_none(caplog):
    """A busy port must degrade to 'no healthcheck', never crash the app."""

    async def scenario():
        blocker = await _start_health_server(port=0)
        try:
            occupied = blocker.addresses[0][1]
            assert await _start_health_server(port=occupied) is None
        finally:
            await blocker.cleanup()

    with caplog.at_level("WARNING", logger="stream_archive.scheduler"):
        asyncio.run(scenario())

    assert any("health endpoint unavailable" in r.message for r in caplog.records)
