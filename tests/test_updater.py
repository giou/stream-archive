import asyncio
import importlib.metadata
import json

import httpx
import pytest
from conftest import make_config as valid_config

from stream_archive.updater import (
    _APP_RELEASES_URL,
    UpdateChecker,
)

APP_CURRENT = "1.0.0"
APP_LATEST = "1.1.0"


class FakeNotifier:
    def __init__(self):
        self.calls = []

    async def notify(self, message):
        self.calls.append(message)


class RaisingNotifier:
    """A notifier whose Telegram send fails."""

    def __init__(self):
        self.calls = 0

    async def notify(self, message):
        self.calls += 1
        msg = "telegram is down"
        raise RuntimeError(msg)


class FakeResponse:
    def __init__(self, status, json_data=None, content=b""):
        self.status_code = status
        self._json_data = json_data
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}"
            raise httpx.HTTPStatusError(
                msg,
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


def make_config(tmp_path):
    """Build a valid config bound to tmp_path.

    The shared defaults differ here: channel1 is the only channel, and the
    config carries a working directory and a config path.
    """
    cfg = valid_config(channels=["channel1"])
    cfg._workdir = tmp_path
    cfg._config_path = tmp_path / "config.json"
    return cfg


def app_http(app_tag=None, app_body=None):
    routes = {}
    if app_tag is not None:
        routes[_APP_RELEASES_URL] = FakeResponse(200, {"tag_name": app_tag, "body": app_body})
    return FakeHttp(routes)


@pytest.fixture
def set_app_version(monkeypatch):
    """Pin importlib.metadata.version for the app distribution."""
    real = importlib.metadata.version

    def fake(name: str) -> str:
        return APP_CURRENT if name == "stream-archive" else real(name)

    monkeypatch.setattr("importlib.metadata.version", fake)


def test_check_up_to_date_no_notify_and_records_state(tmp_path, set_app_version):
    config = make_config(tmp_path)
    http = app_http(app_tag=APP_CURRENT)
    notifier = FakeNotifier()
    u = UpdateChecker(config, notifier, http=http)
    report = asyncio.run(u.check(notify=True))
    assert report["app"]["status"] == "up_to_date"
    assert notifier.calls == []
    # Only the app release is checked: streamlink and the plugin ship with the
    # image.
    assert http.calls == [_APP_RELEASES_URL]
    state = json.loads((tmp_path / "update_state.json").read_text())
    assert state["app"] == APP_CURRENT


def test_app_update_notifies_once_then_dedups(tmp_path, set_app_version):
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(
        config,
        notifier,
        http=app_http(app_tag=APP_LATEST, app_body="Add retention cleanup\nFix proxy retry loop"),
    )
    report = asyncio.run(u.check(notify=True))
    assert report["app"]["status"] == "update"
    assert report["app"]["current"] == APP_CURRENT
    assert report["app"]["latest"] == APP_LATEST
    assert report["app"]["changelog"] == ["Add retention cleanup", "Fix proxy retry loop"]
    assert len(notifier.calls) == 1
    text = notifier.calls[0]
    assert f"• stream-archive: v{APP_CURRENT} → v{APP_LATEST}" in text
    assert "  Changelog:" in text
    assert "  • Add retention cleanup" in text
    assert "  • Fix proxy retry loop" in text
    assert "Apply: docker compose pull && docker compose up -d" in text
    state = json.loads((tmp_path / "update_state.json").read_text())
    assert state["app"] == APP_LATEST
    # A fresh instance simulates a container restart: the dedup must come from
    # update_state.json, not from memory.
    restart_notifier = FakeNotifier()
    u2 = UpdateChecker(config, restart_notifier, http=app_http(app_tag=APP_LATEST))
    second = asyncio.run(u2.check(notify=True))
    assert second["app"]["status"] == "update"
    assert restart_notifier.calls == []


def test_check_notify_false_neither_notifies_nor_writes_state(tmp_path, set_app_version):
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(config, notifier, http=app_http(app_tag=APP_LATEST))
    report = asyncio.run(u.check(notify=False))
    assert report["app"]["status"] == "update"
    assert notifier.calls == []
    assert not (tmp_path / "update_state.json").exists()


def test_app_release_failure_reports_unknown(tmp_path, set_app_version):
    config = make_config(tmp_path)
    u = UpdateChecker(
        config,
        FakeNotifier(),
        http=FakeHttp({_APP_RELEASES_URL: FakeResponse(404, {})}),
    )
    report = asyncio.run(u.check(notify=False))
    assert report["app"]["status"] == "unknown"
    assert report["app"]["current"] == APP_CURRENT
    assert report["app"]["latest"] is None


def test_app_check_no_installed_distribution(tmp_path, monkeypatch):
    monkeypatch.setattr("stream_archive.updater.installed_app_version", lambda: None)
    config = make_config(tmp_path)
    u = UpdateChecker(config, FakeNotifier(), http=app_http(app_tag=APP_LATEST))
    report = asyncio.run(u.check(notify=False))
    assert report["app"]["status"] == "unknown"
    assert report["app"]["current"] is None
    assert report["app"]["latest"] == APP_LATEST


@pytest.mark.parametrize(
    "installed,latest",
    [
        ("1.1.1.dev0", "v1.1.1"),
        ("1.2.0rc1", "v1.2.0"),
        ("1.1.0+dev.1", "v1.1.0"),
    ],
)
def test_dev_install_never_reports_an_update(tmp_path, monkeypatch, installed, latest):
    """A dev, release-candidate, or local build tracks the working tree, so it stays silent."""
    monkeypatch.setattr("stream_archive.updater.installed_app_version", lambda: installed)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(config, notifier, http=app_http(app_tag=latest))
    report = asyncio.run(u.check(notify=True))
    assert report["app"]["status"] == "up_to_date"
    assert notifier.calls == []


def test_failed_check_notifies_nothing(tmp_path, set_app_version):
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(config, notifier, http=FakeHttp({_APP_RELEASES_URL: FakeResponse(500, {})}))
    report = asyncio.run(u.check(notify=True))
    assert report["app"]["status"] == "unknown"
    assert notifier.calls == []
    assert not (tmp_path / "update_state.json").exists()


def test_changelog_lines_truncates_long_body():
    from stream_archive.updater import _changelog_lines

    body = "line1\n" + "word " * 300
    lines = _changelog_lines(body)
    assert lines[0] == "line1"
    assert lines[-1] == "…"
    # Only the short first line and the ellipsis marker survive the limit.
    assert len(lines) == 2


def test_changelog_lines_of_a_missing_or_empty_body_is_empty():
    from stream_archive.updater import _changelog_lines

    assert _changelog_lines(None) == []
    assert _changelog_lines("") == []
    assert _changelog_lines("\n  \n") == []


def test_app_update_without_release_notes_has_no_changelog(tmp_path, set_app_version):
    """A release without notes gives the version bullet and no changelog block."""
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(config, notifier, http=app_http(app_tag=APP_LATEST))

    report = asyncio.run(u.check(notify=True))

    assert report["app"]["status"] == "update"
    assert report["app"]["changelog"] == []
    assert len(notifier.calls) == 1
    text = notifier.calls[0]
    assert f"• stream-archive: v{APP_CURRENT} → v{APP_LATEST}" in text
    assert "Changelog:" not in text
    assert json.loads((tmp_path / "update_state.json").read_text())["app"] == APP_LATEST


def test_notify_failure_keeps_the_release_unrecorded(tmp_path, set_app_version):
    """A failed send must not consume the release: the next check notifies again."""
    config = make_config(tmp_path)
    notifier = RaisingNotifier()
    u = UpdateChecker(config, notifier, http=app_http(app_tag=APP_LATEST))

    # The failure of the send must not escape to the run loop.
    report = asyncio.run(u.check(notify=True))

    assert report["app"]["status"] == "update"
    assert notifier.calls == 1
    assert not (tmp_path / "update_state.json").exists()

    second = FakeNotifier()
    u2 = UpdateChecker(config, second, http=app_http(app_tag=APP_LATEST))
    asyncio.run(u2.check(notify=True))
    assert len(second.calls) == 1  # the release was still unrecorded


def test_default_client_follows_redirects(tmp_path):
    # GitHub answers 301 when a repository is renamed, so the default client
    # must follow redirects.
    config = make_config(tmp_path)
    u = UpdateChecker(config, FakeNotifier())
    assert u._http.follow_redirects is True
    asyncio.run(u.close())


def test_run_loop_checks_immediately_then_sleeps(tmp_path, monkeypatch, set_app_version):
    config = make_config(tmp_path)
    # A non-default interval proves the loop reads the config, not a constant.
    config.update_check.interval_hours = 0.5
    notifier = FakeNotifier()
    # A valid route makes the first iteration a real success path.
    u = UpdateChecker(config, notifier, http=app_http(app_tag=APP_CURRENT))
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
    assert slept == [0.5 * 3600]
    # The first check ran: it reported the up-to-date state and recorded it.
    assert notifier.calls == []
    assert json.loads((tmp_path / "update_state.json").read_text())["app"] == APP_CURRENT


def test_run_loop_disabled_never_checks(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.update_check.enabled = False
    config.update_check.interval_hours = 0.5
    u = UpdateChecker(config, FakeNotifier(), http=FakeHttp({}))
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
    assert slept == [0.5 * 3600]


def test_state_file_holding_a_list_starts_fresh(tmp_path, set_app_version):
    """A state file that is not an object must not break every later check."""
    (tmp_path / "update_state.json").write_text("[]")
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(config, notifier, http=app_http(app_tag=APP_CURRENT))

    report = asyncio.run(u.check(notify=True))

    assert report["app"]["status"] == "up_to_date"
    assert json.loads((tmp_path / "update_state.json").read_text())["app"] == APP_CURRENT
