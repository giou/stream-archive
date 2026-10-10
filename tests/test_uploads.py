"""Upload hub: parallel uploads share one state for Telegram and the web."""

import asyncio

from stream_archive.uploads import UploadHub


async def wait_until(condition, timeout=5.0):
    async def _poll():
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


def test_parallel_uploads_cancel_one(tmp_path):
    """Two uploads run together: one cancels, the other completes."""

    async def scenario():
        hub = UploadHub()
        release = asyncio.Event()
        started_a = asyncio.Event()
        started_b = asyncio.Event()

        async def runner_a(progress):
            started_a.set()
            progress(10, 100, "upload")
            await release.wait()
            return "https://www.youtube.com/watch?v=a"

        async def runner_b(progress):
            started_b.set()
            progress(100, 100, "upload")
            return "https://www.youtube.com/watch?v=b"

        path_a = str(tmp_path / "a.mp4")
        path_b = str(tmp_path / "b.mp4")
        id_a = hub.submit("twitch:x", path_a, runner_a)
        id_b = hub.submit("twitch:x", path_b, runner_b)
        await asyncio.wait_for(asyncio.gather(started_a.wait(), started_b.wait()), 5.0)
        assert hub.running_id(path_a) == id_a
        assert hub.cancel(id_a) is True
        release.set()
        await wait_until(lambda: not hub._tasks)
        snap = {item["id"]: item for item in hub.snapshot()}
        assert snap[id_a]["status"] == "cancelled"
        assert snap[id_b]["status"] == "done"
        assert snap[id_b]["result_url"] == "https://www.youtube.com/watch?v=b"
        assert hub.running_id(path_a) is None
        assert hub.cancel(id_a) is False

    asyncio.run(scenario())


def test_failed_upload_reports_error(tmp_path):
    """A failing runner lands in the snapshot with its error."""

    async def scenario():
        hub = UploadHub()

        async def boom(progress):
            msg = "quota exceeded"
            raise RuntimeError(msg)

        upload_id = hub.submit("twitch:x", str(tmp_path / "c.mp4"), boom)
        await wait_until(lambda: not hub._tasks)
        snap = {item["id"]: item for item in hub.snapshot()}
        assert snap[upload_id]["status"] == "failed"
        assert "see logs" in snap[upload_id]["error"]

    asyncio.run(scenario())


def test_done_upload_remembers_url(tmp_path):
    """A finished upload leaves its URL in a sidecar for the panel."""
    from stream_archive.youtube_upload import read_youtube_url

    async def scenario():
        hub = UploadHub()

        async def runner(progress):
            return "https://www.youtube.com/watch?v=kept"

        target = tmp_path / "d.mp4"
        target.write_bytes(b"x")
        upload_id = hub.submit("twitch:x", str(target), runner)
        await wait_until(lambda: not hub._tasks)
        snap = {item["id"]: item for item in hub.snapshot()}
        assert snap[upload_id]["status"] == "done"
        assert read_youtube_url(target) == "https://www.youtube.com/watch?v=kept"

    asyncio.run(scenario())


def test_progress_visible_in_snapshot(tmp_path):
    """Progress reports update the record the web list reads."""

    async def scenario():
        hub = UploadHub()
        gate = asyncio.Event()

        async def runner(progress):
            progress(25, 100, "upload")
            await gate.wait()
            return "url"

        upload_id = hub.submit("twitch:x", str(tmp_path / "d.mp4"), runner)
        await wait_until(lambda: hub.snapshot()[0]["sent"] == 25)
        snap = {item["id"]: item for item in hub.snapshot()}
        assert snap[upload_id]["note"] == "upload"
        assert snap[upload_id]["status"] == "running"
        gate.set()
        await wait_until(lambda: not hub._tasks)

    asyncio.run(scenario())
