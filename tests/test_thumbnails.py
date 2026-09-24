"""Thumbnail cache mapping, capture, serving, and cleanup."""

import asyncio
import shutil
import subprocess

from aiohttp.test_utils import TestClient, TestServer

from stream_archive import disk


def _thumb_paths(config):
    from pathlib import Path

    rec = Path(disk.resolve_recording_dir(config)) / "twitch" / "channel1" / "show.mp4"
    return rec, disk.thumbnail_path(config, rec)


def test_thumbnail_path_mirrors_archive(tmp_path):
    from test_webui import make_webui

    config, _, _, _, _ = make_webui(tmp_path)
    rec, thumb = _thumb_paths(config)
    assert thumb is not None
    assert str(thumb.relative_to(config.workdir)) == ".cache/thumbnails/twitch/channel1/show.jpg"


def test_thumbnail_path_rejects_audio_and_foreign(tmp_path):
    from test_webui import make_webui

    config, _, _, _, _ = make_webui(tmp_path)
    base = disk.resolve_recording_dir(config)
    assert disk.thumbnail_path(config, base / "a.m4a") is None
    assert disk.thumbnail_path(config, tmp_path / "elsewhere" / "x.mp4") is None


def test_capture_thumbnail_needs_real_frame(tmp_path):
    if shutil.which("ffmpeg") is None:
        return
    from test_webui import make_webui

    from stream_archive.recorder.remux import capture_thumbnail

    config, _, _, _, _ = make_webui(tmp_path)
    rec, thumb = _thumb_paths(config)
    rec.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x180:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(rec),
        ],
        check=True,
        timeout=120,
    )
    assert capture_thumbnail(rec, thumb) is True
    head = thumb.read_bytes()[:3]
    assert head.startswith(b"\xff\xd8\xff")


def test_thumb_endpoint_serves_and_cleans_up(tmp_path):
    import os as _os

    from test_webui import login, make_webui, rec_dir

    config, _, recorder, _, wh = make_webui(tmp_path)
    base = rec_dir(config)
    target = base / "show.mp4"
    target.write_bytes(b"v" * 10)
    _, thumb = _thumb_paths(config)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"\xff\xd8\xff" + b"\0" * 10)

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            csrf = await login(client)
            headers = {"X-CSRF-Token": csrf}
            ok = await client.get("/api/recordings/thumb?id=twitch/channel1/show.mp4")
            body = await ok.read()
            escape = await client.get("/api/recordings/thumb?id=../config.json")
            live = recorder._active_paths
            recorder._active_paths = lambda: {_os.path.realpath(target)}  # type: ignore[method-assign]
            try:
                blocked = await client.get("/api/recordings/thumb?id=twitch/channel1/show.mp4")
            finally:
                recorder._active_paths = live
            gone = await client.delete("/api/recordings?id=twitch/channel1/show.mp4", headers=headers)
            after = await client.get("/api/recordings/thumb?id=twitch/channel1/show.mp4")
            return ok, body, escape.status, blocked.status, gone.status, after.status

    ok, body, escape, blocked, gone, after = asyncio.run(scenario())
    assert ok.status == 200
    assert ok.headers["Content-Type"] == "image/jpeg"
    assert ok.headers["Cache-Control"] == "private, max-age=86400"
    assert body.startswith(b"\xff\xd8\xff")
    assert escape == 400
    assert blocked == 409
    assert gone == 200
    assert after == 404
    assert not thumb.exists()


def test_thumb_endpoint_generates_on_demand(tmp_path):
    import asyncio as _asyncio

    if shutil.which("ffmpeg") is None:
        return
    from test_webui import login, make_webui, rec_dir

    config, _, _, _, wh = make_webui(tmp_path)
    base = rec_dir(config)
    target = base / "show.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x180:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        check=True,
        timeout=120,
    )

    async def scenario():
        async with TestClient(TestServer(wh._app)) as client:
            await login(client)
            resp = await client.get("/api/recordings/thumb?id=twitch/channel1/show.mp4")
            return resp.status, await resp.read()

    status, body = _asyncio.run(scenario())
    assert status == 200
    assert body.startswith(b"\xff\xd8\xff")
