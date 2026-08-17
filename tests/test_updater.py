import asyncio
import importlib.metadata
import json

import httpx
import pytest

from stream_archive.config import AppConfig
from stream_archive.updater import (
    _APP_RELEASES_URL,
    _PLUGIN_RELEASES_URL,
    _PYPI_STREAMLINK_URL,
    _STREAMLINK_RELEASE_NOTES_URL,
    UpdateChecker,
)

PLUGIN_CURRENT = "8.3.0-20260701"
PLUGIN_LATEST = "9.0.0-20260801"
STREAMLINK_CURRENT = "8.4.0"
STREAMLINK_LATEST = "8.5.0"
APP_CURRENT = "1.0.0"
APP_LATEST = "1.1.0"


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
    cfg = AppConfig.model_validate(config)
    cfg._workdir = tmp_path
    cfg._config_path = tmp_path / "config.json"
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "twitch.py").write_text(f'STREAMLINK_TTVLOL_VERSION = "{PLUGIN_CURRENT}"\n')
    return cfg


def plugin_http(
    plugin_tag=PLUGIN_CURRENT,
    streamlink_version=STREAMLINK_CURRENT,
    plugin_body=None,
    streamlink_body=None,
    app_tag=None,
    app_body=None,
):
    routes = {
        _PLUGIN_RELEASES_URL: FakeResponse(200, {"tag_name": plugin_tag, "assets": [], "body": plugin_body}),
        _PYPI_STREAMLINK_URL: FakeResponse(200, {"info": {"version": streamlink_version}}),
    }
    if app_tag is not None:
        routes[_APP_RELEASES_URL] = FakeResponse(200, {"tag_name": app_tag, "body": app_body})
    if streamlink_body is not None:
        routes[_STREAMLINK_RELEASE_NOTES_URL.format(tag=streamlink_version)] = FakeResponse(
            200, {"body": streamlink_body}
        )
    return FakeHttp(routes)


@pytest.fixture
def set_installed_versions(monkeypatch):
    """Pin importlib.metadata.version per distribution name; other names hit the real metadata."""
    real = importlib.metadata.version
    overrides: dict[str, str] = {}

    def _set(name: str, version: str) -> None:
        overrides[name] = version

    def fake(name: str) -> str:
        if name in overrides:
            return overrides[name]
        return real(name)

    monkeypatch.setattr("importlib.metadata.version", fake)
    return _set


@pytest.fixture
def set_streamlink_version(set_installed_versions):
    def _set(version):
        set_installed_versions("streamlink", version)

    return _set


@pytest.fixture
def set_installed_app_version(set_installed_versions):
    def _set(version):
        set_installed_versions("stream-archive", version)

    return _set


def test_check_all_up_to_date_no_notify_and_records_state(tmp_path, set_streamlink_version, set_installed_app_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    set_installed_app_version(APP_CURRENT)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(config, notifier, http=plugin_http(app_tag=APP_CURRENT))
    report = asyncio.run(u.check(notify=True))
    assert report["app"]["status"] == "up_to_date"
    assert report["streamlink"]["status"] == "up_to_date"
    assert report["plugin"]["status"] == "up_to_date"
    assert notifier.calls == []
    state = json.loads((tmp_path / "update_state.json").read_text())
    assert state["app"] == APP_CURRENT
    assert state["streamlink"] == STREAMLINK_CURRENT
    assert state["plugin"] == PLUGIN_CURRENT


def test_plugin_update_notifies_once_then_dedups(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(
        config,
        notifier,
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
    assert "docker compose pull" not in text
    assert "No action needed — plugin/streamlink updates ship in a future image release." in text
    state = json.loads((tmp_path / "update_state.json").read_text())
    assert state["plugin"] == PLUGIN_LATEST
    asyncio.run(u.check(notify=True))
    assert len(notifier.calls) == 1


def test_check_notify_false_neither_notifies_nor_writes_state(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(config, notifier, http=plugin_http(plugin_tag=PLUGIN_LATEST))
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


def test_app_update_notifies_with_pull_footer(tmp_path, set_streamlink_version, set_installed_app_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    set_installed_app_version(APP_CURRENT)
    config = make_config(tmp_path)
    notifier = FakeNotifier()
    u = UpdateChecker(
        config,
        notifier,
        http=plugin_http(app_tag=APP_LATEST, app_body="Add retention cleanup\nFix proxy retry loop"),
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
    asyncio.run(u.check(notify=True))
    assert len(notifier.calls) == 1


def test_app_release_failure_reports_unknown(tmp_path, set_installed_app_version):
    set_installed_app_version(APP_CURRENT)
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
    monkeypatch.setattr("stream_archive.updater._installed_app_version", lambda: None)
    config = make_config(tmp_path)
    u = UpdateChecker(config, FakeNotifier(), http=plugin_http(app_tag=APP_LATEST))
    report = asyncio.run(u.check(notify=False))
    assert report["app"]["status"] == "unknown"
    assert report["app"]["current"] is None
    assert report["app"]["latest"] == APP_LATEST


def test_all_unknown_no_notify(tmp_path, set_installed_app_version):
    set_installed_app_version(APP_CURRENT)
    config = make_config(tmp_path)
    config.update_check.check_streamlink = False
    config.update_check.check_plugin = False
    notifier = FakeNotifier()
    u = UpdateChecker(
        config,
        notifier,
        http=FakeHttp({_APP_RELEASES_URL: FakeResponse(500, {})}),
    )
    report = asyncio.run(u.check(notify=True))
    assert report["app"]["status"] == "unknown"
    assert notifier.calls == []
    assert not (tmp_path / "update_state.json").exists()


def test_check_disabled_sources_left_out(tmp_path, set_streamlink_version):
    set_streamlink_version(STREAMLINK_CURRENT)
    config = make_config(tmp_path)
    config.update_check.check_app = False
    config.update_check.check_plugin = False
    u = UpdateChecker(config, FakeNotifier(), http=plugin_http())
    report = asyncio.run(u.check(notify=False))
    assert "app" not in report
    assert "plugin" not in report
    assert report["streamlink"]["status"] == "up_to_date"


def test_changelog_lines_truncates_long_body():
    from stream_archive.updater import _changelog_lines

    body = "line1\n" + "word " * 300
    lines = _changelog_lines(body)
    assert lines[-1] == "…"
    assert len(" ".join(lines)) <= 620


def test_default_client_follows_redirects(tmp_path):
    # GitHub release assets 302-redirect to release-assets.githubusercontent.com,
    # so redirects must be followed.
    config = make_config(tmp_path)
    u = UpdateChecker(config, FakeNotifier())
    assert u._http.follow_redirects is True
    asyncio.run(u.close())


def test_run_loop_checks_immediately_then_sleeps(tmp_path, monkeypatch):
    config = make_config(tmp_path)
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
    assert checks == [True]
    assert slept == [24 * 3600]


def test_run_loop_disabled_never_checks(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.update_check.enabled = False
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
    assert slept == [24 * 3600]
