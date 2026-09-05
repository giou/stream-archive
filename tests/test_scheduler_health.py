import asyncio

from aiohttp import ClientSession

from stream_archive import scheduler as scheduler_module
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


def test_readyz_flips_with_ready_flag():
    async def scenario():
        old = scheduler_module._READY
        scheduler_module._READY = False
        try:
            runner = await _start_health_server(port=0)
            assert runner is not None
            try:
                host, port = runner.addresses[0][:2]
                async with ClientSession() as session:
                    async with session.get(f"http://{host}:{port}/readyz") as resp:
                        assert resp.status == 503
                    scheduler_module._READY = True
                    async with session.get(f"http://{host}:{port}/readyz") as resp:
                        assert resp.status == 200
                        assert await resp.text() == "ready"
                    async with session.get(f"http://{host}:{port}/healthz") as resp:
                        assert resp.status == 200
            finally:
                await runner.cleanup()
        finally:
            scheduler_module._READY = old

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
