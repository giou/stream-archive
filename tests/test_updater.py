import asyncio
import hashlib
import json

import httpx
import pytest

from src.stream_archive.config import _validate
from src.stream_archive.updater import (
    _PLUGIN_DOWNLOAD_URL,
    _PLUGIN_RELEASES_URL,
    _PYPI_STREAMLINK_URL,
    _STREAMLINK_RELEASE_NOTES_URL,
    UpdateChecker,
)

PLUGIN_CURRENT = "8.3.0-20260701"
PLUGIN_LATEST = "9.0.0-20260801"
STREAMLINK_CURRENT = "8.4.0"
STREAMLINK_LATEST = "8.5.0"
LOCAL_SHA = "abc1234"
REMOTE_SHA = "abc1234"


class FakeNotifier:
    def __init__(self):
        self.calls = []

    async def notify(self, message):
        self.calls.append(message)


class FakeResponse:
    def __init__(self, status, json_data=None, content=b""):
        self.status_code = status
        self._json_data = json_data
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://fake"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._json_data


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        return self.routes[url]


class FakeRunCmd:
    """run_cmd driven by a scripted {tuple(cmd): (rc, stdout, stderr)} dict."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    async def __call__(self, cmd, cwd):
        self.calls.append((list(cmd), str(cwd)))
        return self.script.get(tuple(cmd), (1, "", "unscripted: " + " ".join(cmd)))


def make_config(tmp_path):
    config = {
        "telegram_user_id": 12345,
        "bot_telegram_api": "bot_token",
        "twitch_client_id": "client_id",
        "twitch_client_secret": "client_secret",
        "channels": ["channel1"],
        "proxy_list": ["httpproxy://user:pass@host:port"],
        "monitoring_interval": 60,
        "timezone": "UTC",
        "plugin_dir": "plugins",
        "recording_dir": "recordings",
    }
    _validate(config)
    config["_workdir"] = tmp_path
    config["_config_path"] = tmp_path / "config.json"
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "twitch.py").write_text(
        f'STREAMLINK_TTVLOL_VERSION = "{PLUGIN_CURRENT}"\n'
    )
    return config


def app_up_to_date_script(local=LOCAL_SHA, remote=REMOTE_SHA, count="0", subject="Latest commit", commits=""):
    return {
        ("git", "rev-parse", "HEAD"): (0, local + "\n", ""),
        ("git", "fetch", "origin"): (0, "", ""),
        ("git", "rev-parse", "FETCH_HEAD"): (0, remote + "\n", ""),
        ("git", "rev-list", "--count", "HEAD..FETCH_HEAD"): (0, count + "\n", ""),
        ("git", "log", "-1", "--format=%s", "FETCH_HEAD"): (0, subject + "\n", ""),
        ("git", "log", "--format=%s", "HEAD..FETCH_HEAD"): (0, (commits + "\n") if commits else "", ""),
    }


def plugin_http(plugin_tag=PLUGIN_CURRENT, streamlink_version=STREAMLINK_CURRENT,
                plugin_body=None, streamlink_body=None):
    routes = {
        _PLUGIN_RELEASES_URL: FakeResponse(200, {"tag_name": plugin_tag, "assets": [], "body": plugin_body}),
        _PYPI_STREAMLINK_URL: FakeResponse(200, {"info": {"version": streamlink_version}}),
    }
    if streamlink_body is not None:
        routes[_STREAMLINK_RELEASE_NOTES_URL.format(tag=streamlink_version)] = FakeResponse(200, {"body": streamlink_body})
    return FakeHttp(routes)


@pytest.fixture
def set_streamlink_version(monkeypatch):
    def _set(version):
        monkeypatch.setattr("importlib.metadata.version", lambda name: version)

    return _set


def read_plugin(tmp_path):
    return (tmp_path / "plugins" / "twitch.py").read_text()


def test_check_all_up_to_date_no_notify_and_records_state(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(
        config, notifier, run_cmd=FakeRunCmd(app_up_to_date_script()), http=plugin_http()
    )
    report = asyncio.run(u.check(notify=True))
    assert report["app"]["status"] == "up_to_date"
    assert report["streamlink"]["status"] == "up_to_date"
    assert report["plugin"]["status"] == "up_to_date"
    assert notifier.calls == []
    state = json.loads((tmp_path / "update_state.json").read_text())
    assert state["app"] == LOCAL_SHA
    assert state["streamlink"] == STREAMLINK_CURRENT
    assert state["plugin"] == PLUGIN_CURRENT


def test_plugin_update_notifies_once_then_dedups(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(
        config,
        notifier,
        run_cmd=FakeRunCmd(app_up_to_date_script()),
        http=plugin_http(
            plugin_tag=PLUGIN_LATEST,
            plugin_body="Fixed: crash on live edge\nImproved: proxy rotation",
        ),
    )
    report = asyncio.run(u.check(notify=True))
    assert report["plugin"]["status"] == "update"
    assert len(notifier.calls) == 1
    text = notifier.calls[0]
    assert f"streamlink-ttvlol: {PLUGIN_CURRENT} → {PLUGIN_LATEST}" in text
    assert "  Changelog:" in text
    assert "  • Fixed: crash on live edge" in text
    assert "  • Improved: proxy rotation" in text
    state = json.loads((tmp_path / "update_state.json").read_text())
    assert state["plugin"] == PLUGIN_LATEST
    asyncio.run(u.check(notify=True))
    assert len(notifier.calls) == 1


def test_check_notify_false_neither_notifies_nor_writes_state(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(
        config,
        notifier,
        run_cmd=FakeRunCmd(app_up_to_date_script()),
        http=plugin_http(plugin_tag=PLUGIN_LATEST),
    )
    report = asyncio.run(u.check(notify=False))
    assert report["plugin"]["status"] == "update"
    assert notifier.calls == []
    assert not (tmp_path / "update_state.json").exists()


def test_streamlink_update_notifies(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(
        config,
        notifier,
        run_cmd=FakeRunCmd(app_up_to_date_script()),
        http=plugin_http(
            plugin_tag=PLUGIN_CURRENT,
            streamlink_version=STREAMLINK_LATEST,
            streamlink_body="Fixed: named-pipe cleanup\nFixed: stream start offsets",
        ),
    )
    report = asyncio.run(u.check(notify=True))
    assert report["streamlink"]["status"] == "update"
    assert len(notifier.calls) == 1
    text = notifier.calls[0]
    assert f"streamlink: {STREAMLINK_CURRENT} → {STREAMLINK_LATEST}" in text
    assert "  • Fixed: named-pipe cleanup" in text
    assert "  • Fixed: stream start offsets" in text


def test_app_update_detects_behind(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    script = app_up_to_date_script(
        local=LOCAL_SHA, remote="def5678", count="3",
        subject="Add retention cleanup",
        commits="Add retention cleanup\nFix proxy retry loop",
    )
    u = UpdateChecker(config, notifier, run_cmd=FakeRunCmd(script), http=plugin_http())
    report = asyncio.run(u.check(notify=True))
    assert report["app"]["status"] == "update"
    assert report["app"]["behind"] == 3
    assert report["app"]["subject"] == "Add retention cleanup"
    assert report["app"]["local"] == LOCAL_SHA
    assert report["app"]["remote"] == "def5678"
    assert report["app"]["changelog"] == ["Add retention cleanup", "Fix proxy retry loop"]
    text = notifier.calls[0]
    assert '• stream-archive: 3 new commit(s) — "Add retention cleanup"' in text
    assert "  Changelog:" in text
    assert "  • Add retention cleanup" in text
    assert "  • Fix proxy retry loop" in text


def test_app_fetch_failure_reports_unknown(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    script = {
        ("git", "rev-parse", "HEAD"): (0, LOCAL_SHA + "\n", ""),
        ("git", "fetch", "origin"): (1, "", "fatal: could not read Username for 'https://github.com'"),
    }
    u = UpdateChecker(
        config, FakeNotifier(), run_cmd=FakeRunCmd(script), http=plugin_http()
    )
    report = asyncio.run(u.check(notify=False))
    assert report["app"]["status"] == "unknown"
    assert report["app"]["local"] == LOCAL_SHA
    assert report["app"]["remote"] is None


def test_check_disabled_sources_left_out(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    config["update_check"]["check_app"] = False
    config["update_check"]["check_plugin"] = False
    u = UpdateChecker(
        config, FakeNotifier(), run_cmd=FakeRunCmd({}), http=plugin_http()
    )
    report = asyncio.run(u.check(notify=False))
    assert "app" not in report
    assert "plugin" not in report
    assert report["streamlink"]["status"] == "up_to_date"


def update_report(plugin_digest):
    return {
        "app": {"status": "update", "behind": 2, "subject": "Fix retention"},
        "plugin": {
            "status": "update",
            "current": PLUGIN_CURRENT,
            "latest": PLUGIN_LATEST,
            "digest": plugin_digest,
        },
        "streamlink": {"status": "update", "current": STREAMLINK_CURRENT, "latest": STREAMLINK_LATEST},
    }


def apply_fakes(new_content, git_diff_rc=0, git_pull=(0, "", ""), uv_lock=(0, "", "")):
    digest = "sha256:" + hashlib.sha256(new_content).hexdigest()
    run_cmd = FakeRunCmd({
        ("git", "pull", "--ff-only"): git_pull,
        ("git", "diff", "--quiet", "--", "plugins/twitch.py"): (git_diff_rc, "", ""),
        ("uv", "lock", "--upgrade-package", "streamlink"): uv_lock,
    })
    http = FakeHttp({
        _PLUGIN_DOWNLOAD_URL.format(tag=PLUGIN_LATEST): FakeResponse(200, content=new_content),
    })
    return run_cmd, http, digest


def test_apply_applies_all_in_order(tmp_path):
    config = make_config(tmp_path)
    new_content = f'STREAMLINK_TTVLOL_VERSION = "{PLUGIN_LATEST}"\n'.encode()
    run_cmd, http, digest = apply_fakes(new_content)
    u = UpdateChecker(config, FakeNotifier(), run_cmd=run_cmd, http=http)
    results = asyncio.run(u.apply(update_report(digest)))
    assert results["app"][0] == "applied"
    assert results["plugin"][0] == "applied"
    assert results["streamlink"][0] == "applied"
    assert [cmd for cmd, _ in run_cmd.calls] == [
        ["git", "pull", "--ff-only"],
        ["git", "diff", "--quiet", "--", "plugins/twitch.py"],
        ["uv", "lock", "--upgrade-package", "streamlink"],
    ]
    assert read_plugin(tmp_path) == new_content.decode()
    assert not (tmp_path / "plugins" / "twitch.py.bak").exists()


def test_apply_dirty_plugin_backs_up(tmp_path):
    config = make_config(tmp_path)
    new_content = f'STREAMLINK_TTVLOL_VERSION = "{PLUGIN_LATEST}"\n'.encode()
    run_cmd, http, digest = apply_fakes(new_content, git_diff_rc=1)
    u = UpdateChecker(config, FakeNotifier(), run_cmd=run_cmd, http=http)
    results = asyncio.run(u.apply(update_report(digest)))
    assert results["plugin"][0] == "applied"
    assert "backed up" in results["plugin"][1]
    bak = tmp_path / "plugins" / "twitch.py.bak"
    assert bak.read_text() == f'STREAMLINK_TTVLOL_VERSION = "{PLUGIN_CURRENT}"\n'
    assert read_plugin(tmp_path) == new_content.decode()


def test_apply_sha256_mismatch_rejects_download(tmp_path):
    config = make_config(tmp_path)
    old = read_plugin(tmp_path)
    run_cmd, http, _ = apply_fakes(b"STREAMLINK_TTVLOL_VERSION = \"9.9.9\"\n")
    u = UpdateChecker(config, FakeNotifier(), run_cmd=run_cmd, http=http)
    results = asyncio.run(u.apply(update_report("sha256:" + "0" * 64)))
    assert results["plugin"] == ("failed", "sha256 mismatch — download rejected")
    assert read_plugin(tmp_path) == old
    assert results["app"][0] == "applied"
    assert results["streamlink"][0] == "applied"


def test_apply_app_failure_continues_with_others(tmp_path):
    config = make_config(tmp_path)
    new_content = f'STREAMLINK_TTVLOL_VERSION = "{PLUGIN_LATEST}"\n'.encode()
    run_cmd, http, digest = apply_fakes(
        new_content,
        git_pull=(1, "", "fatal: unable to access 'https://github.com/giou/stream-archive': Could not resolve host"),
    )
    u = UpdateChecker(config, FakeNotifier(), run_cmd=run_cmd, http=http)
    results = asyncio.run(u.apply(update_report(digest)))
    assert results["app"] == (
        "failed",
        "git pull: fatal: unable to access 'https://github.com/giou/stream-archive': Could not resolve host",
    )
    assert results["plugin"][0] == "applied"
    assert results["streamlink"][0] == "applied"


def test_changelog_lines_truncates_long_body():
    from src.stream_archive.updater import _changelog_lines

    body = "line1\n" + "word " * 300
    lines = _changelog_lines(body)
    assert lines[-1] == "…"
    assert len(" ".join(lines)) <= 620


def test_app_changelog_capped(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    commits = "\n".join(f"commit {i}" for i in range(12))
    script = app_up_to_date_script(remote="def5678", count="12", subject="commit 11", commits=commits)
    u = UpdateChecker(config, FakeNotifier(), run_cmd=FakeRunCmd(script), http=plugin_http())
    report = asyncio.run(u.check(notify=False))
    cl = report["app"]["changelog"]
    assert len(cl) == 11
    assert cl[-1] == "…and 2 more"


def test_default_client_follows_redirects(tmp_path):
    # GitHub release assets 302-redirect to release-assets.githubusercontent.com;
    # without follow_redirects the plugin download always fails.
    config = make_config(tmp_path)
    u = UpdateChecker(config, FakeNotifier(), run_cmd=FakeRunCmd({}))
    assert u._http.follow_redirects is True
    asyncio.run(u.close())


def test_run_loop_checks_immediately_then_sleeps(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    u = UpdateChecker(config, FakeNotifier(), run_cmd=FakeRunCmd({}), http=FakeHttp({}))
    checks = []
    orig = u.check

    async def wrapped(notify):
        checks.append(notify)
        return await orig(notify)

    u.check = wrapped
    slept = []

    def fake_sleep(duration):
        slept.append(duration)
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(u.run_loop())
    assert checks == [True]
    assert slept == [24 * 3600]


def test_run_loop_disabled_never_checks(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["update_check"]["enabled"] = False
    u = UpdateChecker(config, FakeNotifier(), run_cmd=FakeRunCmd({}), http=FakeHttp({}))
    checks = []
    orig = u.check

    async def wrapped(notify):
        checks.append(notify)
        return await orig(notify)

    u.check = wrapped
    slept = []

    def fake_sleep(duration):
        slept.append(duration)
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(u.run_loop())
    assert checks == []
    assert slept == [24 * 3600]
