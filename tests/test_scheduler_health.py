import asyncio
import logging

from aiohttp import ClientSession, ClientTimeout

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
            assert runner.addresses, "health server exposes no bound address"
            host, port = runner.addresses[0][:2]
            # A short timeout fails the test fast when the server never answers.
            async with (
                ClientSession(timeout=ClientTimeout(total=5)) as session,
                session.get(f"http://{host}:{port}/healthz") as resp,
            ):
                assert resp.status == 200
                assert await resp.text() == "ok"
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_readyz_flips_with_ready_flag(monkeypatch):
    # monkeypatch restores the process-wide flag at teardown, so the false
    # readiness of this test cannot leak into another test.
    monkeypatch.setattr(scheduler_module, "_READY", False)

    async def scenario():
        runner = await _start_health_server(port=0)
        assert runner is not None
        try:
            assert runner.addresses, "health server exposes no bound address"
            host, port = runner.addresses[0][:2]
            async with ClientSession(timeout=ClientTimeout(total=5)) as session:
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

    asyncio.run(scenario())


def test_health_bind_failure_returns_none(caplog):
    """A busy port must degrade to 'no healthcheck', never crash the app."""

    async def scenario():
        blocker = await _start_health_server(port=0)
        assert blocker is not None
        try:
            assert blocker.addresses, "health server exposes no bound address"
            occupied = blocker.addresses[0][1]
            second = await _start_health_server(port=occupied)
            try:
                assert second is None
            finally:
                # A failed test must not leave a second listener running.
                if second is not None:
                    await second.cleanup()
        finally:
            await blocker.cleanup()

    with caplog.at_level("WARNING", logger="stream_archive.scheduler"):
        asyncio.run(scenario())

    warnings = [r for r in caplog.records if r.name == "stream_archive.scheduler" and r.levelno == logging.WARNING]
    assert any("health endpoint unavailable" in r.getMessage() for r in warnings)


class _HangingRecorder:
    """A recorder whose close() never returns, like one stuck in an emote fetch."""

    def __init__(self):
        self.closed = False
        self.cancelled = False

    async def close(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.closed = True


def test_shutdown_bounds_the_recorder_close(monkeypatch):
    """The teardown must not outlast the container's stop grace period.

    The chat finalizer writes its trailer after awaiting the emote fetch, and
    that fetch is bounded per request rather than in total. An unbounded close
    let a slow emote CDN hold shutdown past the grace period, and the process
    was then killed with the chat file still an unterminated .tmp.
    """
    monkeypatch.setattr(scheduler_module, "_SHUTDOWN_DEADLINE_S", 0.05)
    recorder = _HangingRecorder()

    async def scenario():
        await scheduler_module._shutdown(
            health_runner=None,
            kick_webhook=None,
            eventsub=None,
            twitch_api=None,
            kick_api=None,
            recorder=recorder,
            notifier=None,
            updater=None,
            updater_task=None,
            telegram=None,
            youtube_streamer=None,
            shared_http=None,
        )

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert recorder.cancelled is True
    # The cancellation still ran the close path's cleanup, not just the wait.
    assert recorder.closed is True
