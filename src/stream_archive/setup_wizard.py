"""Guided setup for first-time users and later feature setup.

Run ``stream-archive-setup`` from the data directory. A first run writes
a new ``config.json`` with credentials and reach only: channels live in
the panel and the bot only. A later run loads the current file and adds
or changes one block at a time. Each finished step saves at once, so an
interrupted run keeps what it completed.

Blocks:

* Twitch credentials (required).
* Control surface: the web panel, the Telegram bot, or both (one is required).
* Panel access (optional): reach the panel from outside the machine.
* Enable Kick (optional): app credentials, then chat and faster
  events through a public entry you publish yourself. Without a
  public entry Kick still works through polling (slower signals, no chat).
* YouTube restream (optional, runs the OAuth flow of ``setup_youtube``).
* Enable MTProto (upload to Telegram) (optional).
* Reset config wipes config.json (kept as config.json.bak) and starts over.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stream_archive.config import (
    AppConfig,
    apply_config_change,
    get_config,
    normalize_endpoint_url,
    telegram_enabled,
    webhook_public_url,
)
from stream_archive.setup_youtube import main as youtube_main
from stream_archive.tunnels import (
    parse_public_hostname,
    write_ingress_config,
    write_nginx_config,
)
from stream_archive.webui import MIN_PASSWORD_LEN, hash_password

#: Default playlist proxies for a fresh config. They match config.json.example.
_DEFAULT_PROXIES = [
    "httpproxy://firefox.api.cdn-perfprod.com:2023",
    "httpproxy://chromium.api.cdn-perfprod.com:2023",
    "https://eu2.luminous.dev",
    "https://eu.luminous.dev",
    "https://lb-eu3.cdn-perfprod.com",
]


def _read(prompt: str, *, secret: bool = False, default: str | None = None) -> str:
    """One answer from the terminal. Blank returns ``default`` (or "").

    Secrets never echo. A closed stdin exits with a plain error instead
    of a traceback, like the other setup commands do.
    """
    shown = f"{prompt} [{default}]: " if default is not None else f"{prompt}: "
    try:
        raw = getpass.getpass(shown) if secret else input(shown)
    except KeyboardInterrupt:
        # Abandon the prompt. The menu loop decides: back to the menu
        # from a step, out of the program from the menu itself.
        print()
        raise _BackToMenu from None
    except EOFError, OSError:
        print("ERROR: no input received. Run the setup in an interactive terminal.", file=sys.stderr)
        raise SystemExit(1) from None
    raw = raw.strip()
    return raw if raw else (default or "")


def _choose(prompt: str, count: int) -> int:
    """A menu pick from 1 to ``count``. A bad pick asks again."""
    while True:
        raw = _read(f"{prompt} (1-{count})")
        try:
            pick = int(raw)
        except ValueError:
            print(f"Type a number from 1 to {count}.")
            continue
        if 1 <= pick <= count:
            return pick
        print(f"Type a number from 1 to {count}.")


def _yes(prompt: str, *, default: bool = True) -> bool:
    """A yes/no answer. Anything but y/yes/n/no asks again."""
    hint = "Y/n" if default else "y/N"
    while True:
        raw = _read(f"{prompt} ({hint})").lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Answer y or n.")


def _save(config: AppConfig, mutate: Any, what: str) -> bool:
    """Write one step. A rejected change prints the cause and saves nothing."""
    try:
        apply_config_change(config, mutate)
    except ValueError as e:
        print(f"Cannot save {what}: {e}")
        return False
    print(f"{what} saved.")
    return True


def _mask(value: str) -> str:
    """ "set" when a secret holds a value, else "missing". Secrets never print."""
    return "set" if value.strip() else "missing"


def _local_timezone() -> str:
    """Zone name of this machine, without asking.

    The TZ variable wins when it names a real zone. Otherwise the
    /etc/localtime link gives the zone (it sits under a zoneinfo/
    directory). UTC is the fallback.
    """
    tz = os.environ.get("TZ", "").strip()
    if tz:
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError, KeyError:
            pass
        else:
            return tz
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return "UTC"
    marker = "zoneinfo/"
    if marker not in target:
        return "UTC"
    name = target.split(marker, 1)[1]
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError, KeyError:
        return "UTC"
    return name


def _normalize_public_url(raw: str) -> str | None:
    """Endpoint URL with a scheme, or None when the text is unusable.

    A bare hostname gets https://, so ``example.com`` works. The result
    drops a trailing slash and any webhook path.
    """
    text = raw.strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    parts = urlparse(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname
    if not all(char.isalnum() or char in ".-" for char in host) or not host.strip(".-"):
        return None
    return normalize_endpoint_url(text)


def _listen_host_default(host: str) -> str:
    """Suggested listen host.

    A proxy reaches the app through the published host port, so a
    container that still binds its own loopback gets no traffic. Bare
    metal keeps the stored host.
    """
    if host.strip() == "127.0.0.1" and os.path.exists("/.dockerenv"):
        return "0.0.0.0"
    return host


def _step_twitch(config: AppConfig) -> None:
    """Set the Twitch app credentials. Blank keeps a stored value."""
    print("\n-- Twitch credentials --")
    print(f"Client id: {_mask(config.twitch_client_id)}")
    print(f"Client secret: {_mask(config.twitch_client_secret)}")
    client_id = _read("Twitch client id", default=config.twitch_client_id or None)
    secret = _read("Twitch client secret", secret=True, default="***" if config.twitch_client_secret else None)
    if secret == "***":
        secret = config.twitch_client_secret
    if not client_id and not secret:
        print("Twitch credentials unchanged.")
        return

    def mutate(candidate: AppConfig) -> None:
        if client_id:
            candidate.twitch_client_id = client_id
        if secret:
            candidate.twitch_client_secret = secret

    _save(config, mutate, "Twitch credentials")


def _prompt_kick_creds(config: AppConfig) -> tuple[str, str] | None:
    """Kick app credentials, or None when they are already stored."""
    if config.kick.client_id.strip() and config.kick.client_secret.strip():
        return None
    print("Kick needs a Kick app: id and secret from the Kick Developer portal.")
    while True:
        client_id = _read("Kick client id", default=config.kick.client_id or None)
        if client_id:
            break
        print("The Kick client id is required.")
    while True:
        secret = _read("Kick client secret", secret=True, default="***" if config.kick.client_secret else None)
        if secret == "***":
            secret = config.kick.client_secret
        if secret:
            break
        print("The Kick client secret is required.")
    return client_id, secret


def _control_label(config: AppConfig) -> str:
    """One-line state of the control surfaces."""
    parts: list[str] = []
    if config.web.enabled:
        parts.append("web panel")
    if telegram_enabled(config):
        parts.append("Telegram bot")
    return " + ".join(parts) if parts else "none"


def _step_control(config: AppConfig, *, required: bool) -> None:
    """Pick the web panel, the Telegram bot, or both. One of them is required."""
    print("\n-- Control surface --")
    print(f"Active now: {_control_label(config)}")
    print("  1. Web panel")
    print("  2. Telegram bot")
    print("  3. Both")
    pick = _choose("Control surface", 3)
    want_web = pick in (1, 3)
    want_bot = pick in (2, 3)
    password = ""
    replace_hash = config.web.password_hash
    if want_web:
        if replace_hash and not required:
            if not _yes("Replace the panel password", default=False):
                password = ""
            else:
                replace_hash = ""
        if want_web and not replace_hash:
            while True:
                first = _read("New panel password (12+ characters)", secret=True)
                if len(first) < MIN_PASSWORD_LEN:
                    print(f"Password must hold at least {MIN_PASSWORD_LEN} characters.")
                    continue
                second = _read("Repeat the password", secret=True)
                if first != second:
                    print("Passwords differ.")
                    continue
                password = first
                break
    user_id = config.telegram_user_id
    token = config.bot_telegram_api
    if want_bot:
        while True:
            raw = _read("Telegram user id", default=str(user_id) if user_id > 0 else None)
            if not raw and not required and user_id > 0:
                break
            try:
                user_id = int(raw)
            except ValueError:
                print("The user id must be a number.")
                continue
            if user_id <= 0:
                print("The user id must be more than 0.")
                continue
            break
        while True:
            entered = _read("Bot token from BotFather", secret=True, default="***" if token.strip() else None)
            if entered == "***":
                break
            if entered or not required:
                token = entered
                break
            print("The bot token is required.")
    if want_web and not replace_hash and not password:
        print("The panel needs a password. Nothing saved.")
        return
    if want_bot and (user_id <= 0 or not token.strip()):
        print("The bot needs a user id and a token. Nothing saved.")
        return
    hashed = hash_password(password) if password else config.web.password_hash

    def mutate(candidate: AppConfig) -> None:
        candidate.web.enabled = want_web
        candidate.web.password_hash = hashed if want_web else candidate.web.password_hash
        if want_web and not candidate.web.session_secret.strip():
            candidate.web.session_secret = secrets.token_urlsafe(32)
        if want_bot:
            candidate.telegram_user_id = user_id
            candidate.bot_telegram_api = token
        elif not required:
            candidate.telegram_user_id = 0
            candidate.bot_telegram_api = ""

    _save(config, mutate, "Control surface")


def _step_youtube(config: AppConfig) -> None:
    """Pick the output mode and run the YouTube OAuth flow when needed."""
    print("\n-- YouTube restream (optional) --")
    token_path = config.workdir / "youtube_token.json"
    print(f"Output mode: {config.output_mode}")
    print(f"Saved token: {'yes' if token_path.exists() else 'no'}")
    print("  1. Disk only")
    print("  2. YouTube only")
    print("  3. Both")
    pick = _choose("Output mode", 3)
    modes: tuple[Literal["disk"], Literal["youtube"], Literal["both"]] = ("disk", "youtube", "both")
    mode = modes[pick - 1]

    def mutate(candidate: AppConfig) -> None:
        candidate.output_mode = mode

    if not _save(config, mutate, "Output mode"):
        return
    if mode == "disk":
        return
    secrets_path = config.workdir / config.youtube.client_secrets_file
    if not secrets_path.exists():
        print(f"{config.youtube.client_secrets_file} is missing from the data directory.")
        print("Create a Google Cloud OAuth desktop client and download it under that name.")
        print("Publish the consent screen to In production. Then run this setup again.")
        return
    if not secrets_path.is_file():
        print(f"{config.youtube.client_secrets_file} is not a file. Nothing to authorize with.")
        return
    print("The browser opens for the Google authorization. Paste the redirect URL when asked.")
    try:
        youtube_main()
    except SystemExit as e:
        if e.code not in (None, 0):
            print("Authorization did not complete. The mode is saved. Run this setup again to retry.")
        return
    print("YouTube authorization complete.")


def _step_mtproto(config: AppConfig) -> None:
    """Enable the MTProto uploader for recordings past the 50 MB bot cap."""
    print("\n-- Enable MTProto (upload to Telegram) (optional) --")
    print(f"State: {'on' if config.mtproto.enabled else 'off'}")
    print(f"api id: {_mask(str(config.mtproto.api_id) if config.mtproto.api_id else '')}")
    print(f"api hash: {_mask(config.mtproto.api_hash)}")
    print("Get both at https://my.telegram.org. ${VAR_NAME} reads the value from the environment.")
    if not _yes("Enable MTProto (upload to Telegram)", default=config.mtproto.enabled):

        def mutate_off(candidate: AppConfig) -> None:
            candidate.mtproto.enabled = False

        _save(config, mutate_off, "MTProto")
        return
    while True:
        raw_id = _read("api id", default=str(config.mtproto.api_id) if config.mtproto.api_id else None)
        if not raw_id and config.mtproto.api_id:
            api_id = config.mtproto.api_id
            break
        try:
            api_id = int(raw_id)
        except ValueError:
            print("The api id must be a number.")
            continue
        if api_id <= 0:
            print("The api id must be more than 0.")
            continue
        break
    entered = _read("api hash", secret=True, default="***" if config.mtproto.api_hash.strip() else None)
    api_hash = config.mtproto.api_hash if entered == "***" else entered
    if not api_hash.strip():
        print("The api hash is required. Nothing saved.")
        return

    def mutate_on(candidate: AppConfig) -> None:
        candidate.mtproto.api_id = api_id
        candidate.mtproto.api_hash = api_hash
        candidate.mtproto.enabled = True

    _save(config, mutate_on, "MTProto")


def _endpoint_address_label(config: AppConfig) -> tuple[str, str]:
    """Label and value of the endpoint address for display."""
    return "Public URL", config.endpoint.public_url or "none"


def _panel_access_state(config: AppConfig) -> str:
    """One-line state of panel access: off or on."""
    return "on" if config.endpoint.enabled else "off"


def _read_port(prompt: str, default: int) -> int:
    """A port number from the terminal. A bad value asks again."""
    while True:
        raw = _read(prompt, default=str(default))
        try:
            port = int(raw)
        except ValueError:
            print("The port must be a number.")
            continue
        if not 1 <= port <= 65535:
            print("The port must be from 1 to 65535.")
            continue
        return port


def _kick_entry_usable(config: AppConfig) -> bool:
    """True when Kick has a public internet entry for deliveries.

    A separate Kick entry counts on its own. Otherwise the endpoint must
    be on with a public URL. A tailnet-only address never delivers to
    Kick: the enable step warns about it.
    """
    if config.kick.webhook.public_url.strip():
        return True
    ep = config.endpoint
    return bool(ep.enabled and ep.public_url.strip())


def _step_remote(config: AppConfig) -> None:
    """Reach the panel from outside this machine.

    The panel works on this machine with nothing set. The wizard saves a
    public URL you publish yourself: a tailnet address (tailscale serve,
    private to your devices) or an internet address (your own reverse
    proxy). An internet address also lets Kick deliver events.
    """
    print("\n-- Panel access (optional) --")
    print(f"Endpoint: {'on' if config.endpoint.enabled else 'off'}")
    addr_label, addr_value = _endpoint_address_label(config)
    print(f"{addr_label}: {addr_value}")
    if not _yes("Expose the panel beyond this machine", default=config.endpoint.enabled):

        def mutate_off(candidate: AppConfig) -> None:
            candidate.endpoint.enabled = False

        _save(config, mutate_off, "Panel access")
        return
    print("  1. Tailnet only (tailscale serve - private to your devices)")
    print("  2. Internet (your own reverse proxy)")
    tailnet = _choose("Reach", 2) == 1
    host_default = _listen_host_default(config.endpoint.listen_host)
    host = _read("Listen host", default=host_default)
    if not host.strip():
        host = host_default
    port = _read_port("Listen port", config.endpoint.listen_port)
    url: str | None = ""
    if tailnet:
        print(f"Run on the host: tailscale serve --bg --yes {port}")
        print("Then paste the tailnet address it serves.")
        while True:
            raw_url = _read("Tailnet URL", default=config.endpoint.public_url or None)
            url = _normalize_public_url(raw_url)
            if url:
                break
            print("Type a URL like https://host.tailnet.ts.net.")
    else:
        while True:
            raw_url = _read("Public URL", default=config.endpoint.public_url or None)
            url = _normalize_public_url(raw_url)
            if url:
                break
            print("Type a URL like https://example.com or a bare hostname like example.com.")
    if host.strip() == "127.0.0.1":
        print("Note: under Docker set the listen host to 0.0.0.0, or the proxy cannot reach the app.")

    def mutate_on(candidate: AppConfig) -> None:
        ce = candidate.endpoint
        # The URL goes first: the model requires an http(s) URL the moment
        # enabled flips to True, and assignment validates every set.
        ce.public_url = url
        ce.listen_host = host.strip()
        ce.listen_port = port
        ce.enabled = True

    if _save(config, mutate_on, "Panel access") and tailnet:
        if config.kick.webhook.public_url.strip():
            print(f"Kick deliveries keep using {webhook_public_url(config)}.")
        else:
            print("Note: the Kick webhook stays unavailable here: Kick cannot deliver events to a tailnet address.")


def _setup_kick_entry(config: AppConfig) -> None:
    """Pick the public entry for Kick deliveries and save it.

    The proxy choice leads: the wizard generates the proxy config for the
    pick and prints the command that runs it. A pasted URL or the panel
    address stays available for existing setups. Following a panel with
    no public address leaves Kick on polling: the step says so instead
    of implying that deliveries work.
    """
    wh = config.kick.webhook
    webhook_port = wh.listen_port
    print(f"Panel uses: {config.endpoint.public_url or 'none'}")
    print(f"Kick deliveries use: {webhook_public_url(config) or 'none'}")
    print(f"The Kick entry must reach the webhook listener on port {webhook_port}.")
    print("  1. Cloudflare tunnel (you run cloudflared)")
    print("  2. nginx (you run nginx)")
    print("  3. Own reverse proxy URL (already running)")
    print("  4. Follow the panel address")
    pick = _choose("Kick entry", 4)
    stored = ""
    if pick == 1:
        while True:
            raw_host = _read("Kick hostname", default=None)
            host = parse_public_hostname(raw_host)
            if host is not None:
                break
            print("Type a public hostname like kick.example.com.")
        token = _read("Cloudflare token (blank to skip the filename)", secret=True)
        services = [(host, webhook_port)]
        panel_host = parse_public_hostname(config.endpoint.public_url or "")
        if panel_host and panel_host != host:
            services.append((panel_host, config.endpoint.listen_port))
        try:
            path = write_ingress_config(config.workdir, token or "x", tuple(services))
        except ValueError as e:
            print(f"\u274c {e}")
            print("Nothing changed.")
            return
        print(f"Wrote {path}.")
        print(f"Run your tunnel with: cloudflared tunnel --config {path} run")
        print(f"Create this DNS record: CNAME {host} -> <tunnel-id>.cfargotunnel.com (proxied).")
        stored = f"https://{host}"
    elif pick == 2:
        while True:
            raw_host = _read("Kick hostname", default=None)
            host = parse_public_hostname(raw_host)
            if host is not None:
                break
            print("Type a public hostname like kick.example.com.")
        api_port = None
        if _yes("Also publish /api/v1 here for remote control", default=False):
            api_port = config.endpoint.listen_port
        try:
            path = write_nginx_config(config.workdir, host, webhook_port, api_port)
        except ValueError as e:
            print(f"\u274c {e}")
            print("Nothing changed.")
            return
        print(f"Wrote {path}.")
        print("Symlink it from sites-enabled, get a certificate (certbot), then: nginx -s reload")
        stored = f"https://{host}"
    elif pick == 3:
        while True:
            raw_url = _read("Public kick URL", default=config.kick.webhook.public_url or None)
            stored = _normalize_public_url(raw_url) or ""
            if stored:
                break
            print("Type a URL like https://example.com or a bare hostname like example.com.")
        print("Point your own reverse proxy at the webhook listener port.")
        print("Then paste this URL in the Kick app (Settings, Developer, your app, Enable webhooks).")

    was_enabled = config.kick.webhook.enabled

    def mutate(candidate: AppConfig) -> None:
        if stored != config.kick.webhook.public_url:
            candidate.kick.webhook.public_url = stored
        # A usable entry turns deliveries on; without one they stay off.
        # Otherwise the app holds an entry it never uses and chat stays
        # empty. This also repairs entries saved before the step did this.
        panel_ok = bool(candidate.endpoint.enabled and candidate.endpoint.public_url.strip())
        candidate.kick.webhook.enabled = bool(stored.strip() or panel_ok)
        if stored != config.kick.webhook.public_url or (candidate.kick.webhook.enabled and not was_enabled):
            # A new entry proves delivery again on the next event.
            candidate.kick.webhook.setup_notified = False

    _save(config, mutate, "Kick endpoint")
    if not _kick_entry_usable(config):
        print("The panel has no public address, so Kick stays on polling until one is set.")
        return
    print(f"Kick webhook URL: {webhook_public_url(config)}")
    print("Paste it in the Kick app (Settings, Developer, your app, Enable webhooks).")
    if _yes("Test Kick delivery now", default=True):
        _verify_kick_delivery(config)


def _app_reachable(config: AppConfig) -> bool:
    """True when the app listens on the endpoint port of this machine."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", config.endpoint.listen_port), timeout=3):
            return True
    except OSError:
        return False


def _verify_request(
    base: str, path: str, headers: dict[str, str], body: dict[str, Any] | None, opener: Any = None
) -> Any:
    """One JSON call against the running app. Returns the decoded body."""
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    open_call = opener.open if opener is not None else urllib.request.urlopen
    with open_call(request, timeout=210) as response:
        return json.loads(response.read().decode())


def _verify_kick_delivery(config: AppConfig) -> None:
    """Run the in-app delivery test through the control API and print it.

    The app receives real Kick events for a throwaway subscription and
    reports the elapsed time. The wizard only triggers it: everything
    observable stays inside the app, so no reload dance is needed.
    """
    if not config.kick.client_id.strip() or not config.kick.client_secret.strip():
        print("Save Kick credentials first: the test subscribes with them.")
        return
    if not _app_reachable(config):
        print("Start the app first: nothing listens on the endpoint port.")
        return
    base = f"http://127.0.0.1:{config.endpoint.listen_port}"
    headers: dict[str, str] = {}
    opener: Any = None
    if config.api.enabled and config.api.key:
        headers = {"Authorization": f"Bearer {config.api.key}"}
    elif config.web.enabled:
        import http.cookiejar
        import urllib.request

        password = _read("Panel password", secret=True)
        if not password:
            print("Nothing tested.")
            return
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            body = _verify_request(
                base, "/api/login", {"Content-Type": "application/json"}, {"password": password}, opener
            )
        except Exception:
            print("Panel login failed: wrong password or panel unavailable.")
            return
        csrf = body.get("csrf", "") if isinstance(body, dict) else ""
        if not csrf:
            print("Panel login failed: wrong password or panel unavailable.")
            return
        headers = {"X-CSRF-Token": csrf}
    else:
        print("The test needs the API key or the panel password: enable one of them first.")
        return
    print("Testing delivery (up to 3 minutes on a cold setup)...")
    try:
        body = _verify_request(base, "/api/v1/kick/webhook/test", headers, {}, opener)
    except Exception as e:
        print(f"Test call failed: {e}")
        return
    if not isinstance(body, dict):
        print("Test call failed: bad reply from the app.")
        return
    print(("Delivery works: " if body.get("ok") else "Delivery failed: ") + str(body.get("message", "")))


def _step_kick(config: AppConfig) -> None:
    """Kick app credentials, then chat and faster events through a public entry.

    Channels themselves live in the panel and the bot only: this step
    never touches them. Credentials alone give polling (slower signals,
    no chat). The public entry adds instant signals and chat.
    """
    print("\n-- Enable Kick (optional) --")
    print("Kick adds live signals and chat for channels you monitor in the panel or the bot.")
    creds = _prompt_kick_creds(config)
    if creds is not None:

        def mutate_creds(candidate: AppConfig) -> None:
            candidate.kick.client_id = creds[0]
            candidate.kick.client_secret = creds[1]

        _save(config, mutate_creds, "Kick credentials")
    if not _kick_entry_usable(config):
        print("Kick needs a public internet address for instant signals and chat.")
        print("Without one it still works through polling (slower signals, no chat).")
        if not _yes("Enable kick chat and faster events", default=False):
            return
    _setup_kick_entry(config)


def _require_writable_dir(workdir: Path) -> None:
    """Exit when config.json cannot be written here.

    Docker creates a missing mounted directory as root, so the app user
    cannot write into it. Fail before the first prompt, with the fix.
    """
    if os.access(workdir, os.W_OK):
        return
    print(f"ERROR: the data directory is not writable: {workdir}", file=sys.stderr)
    print(
        "Create it as your user first (mkdir -p), or fix ownership "
        f"(sudo chown -R {os.getuid()}:{os.getgid()} {workdir}).",
        file=sys.stderr,
    )
    raise SystemExit(1)


class _ResetRequested(Exception):
    """Bubble to main(): wipe config.json and start the setup over."""


class _BackToMenu(Exception):
    """Bubble to the menu loop: abandon the current step, keep saved steps."""


def _step_reset(config: AppConfig) -> None:
    """Delete config.json (kept as config.json.bak) and start over."""
    print("\n-- Reset config --")
    print("This deletes config.json and starts over.")
    print("Recordings, chat files, and tokens stay.")
    if not _yes("Delete config.json and start over", default=False):
        return
    source = config.config_path
    backup = source.with_name("config.json.bak")
    try:
        backup.write_bytes(source.read_bytes())
        source.unlink()
    except OSError as e:
        print(f"Cannot reset: {e}")
        return
    print(f"Old config kept as {backup}.")
    raise _ResetRequested


def _missing_required(config: AppConfig) -> list[str]:
    """Names of the required blocks that are still incomplete."""
    missing: list[str] = []
    if not config.twitch_client_id.strip() or not config.twitch_client_secret.strip():
        missing.append("Twitch credentials")
    if _control_label(config) == "none":
        missing.append("control surface (web panel, Telegram bot, or both)")
    return missing


def _menu_loop(config: AppConfig) -> None:
    """Run steps until the user quits with every required block complete."""
    steps = (
        ("Twitch credentials", _step_twitch),
        ("Control surface", lambda config: _step_control(config, required=False)),
        ("Panel access", _step_remote),
        ("Enable Kick", _step_kick),
        ("YouTube restream", _step_youtube),
        ("Enable MTProto (upload to Telegram)", _step_mtproto),
        ("Reset config", _step_reset),
    )
    done = str(len(steps) + 1)
    while True:
        print("\n== StreamArchive setup ==")
        print(f"Control: {_control_label(config)}")
        print(f"Channels: {', '.join(config.channels) if config.channels else 'none'}")
        print(f"Output mode: {config.output_mode}")
        print(f"MTProto (Telegram upload): {'on' if config.mtproto.enabled else 'off'}")
        print(f"Panel access: {_panel_access_state(config)}")
        for number, (label, _) in enumerate(steps, start=1):
            print(f"  {number}. {label}")
        print(f"  {done}. Done")
        try:
            raw = _read("Choose a step").strip().lower()
        except _BackToMenu:
            print("Setup closed.")
            return
        if raw in ("q", "quit", "exit", "done", done):
            missing = _missing_required(config)
            if missing:
                print("Still missing: " + "; ".join(missing) + ".")
                continue
            print("Setup complete. Start the app with `docker compose up -d`.")
            return
        try:
            pick = int(raw)
        except ValueError:
            print(f"Type a number from 1 to {done}, or q to finish.")
            continue
        if not 1 <= pick <= len(steps):
            print(f"Type a number from 1 to {done}, or q to finish.")
            continue
        try:
            steps[pick - 1][1](config)
        except _BackToMenu:
            print("Back to the menu.")
            continue


def _fresh_run() -> AppConfig:
    """Build a new config.json from answers, then hand it to the menu loop."""
    workdir = Path("config.json").resolve().parent
    _require_writable_dir(workdir)
    print("No config.json here. This wizard writes one in the current directory.")
    print("Each step saves at once. Run the wizard again later to add features.")
    print("\n-- Twitch credentials --")
    while True:
        client_id = _read("Twitch client id")
        if client_id:
            break
        print("The client id is required.")
    while True:
        client_secret = _read("Twitch client secret", secret=True)
        if client_secret:
            break
        print("The client secret is required.")
    timezone = _local_timezone()
    data: dict[str, Any] = {
        "telegram_user_id": 0,
        "bot_telegram_api": "",
        "twitch_client_id": client_id,
        "twitch_client_secret": client_secret,
        "channels": [],
        "proxy_list": list(_DEFAULT_PROXIES),
        "monitoring_interval": 60,
        "timezone": timezone,
        "plugin_dir": "/app/plugins",
        "recording_dir": "recordings",
    }
    try:
        config = AppConfig.model_validate(data)
    except ValueError as e:
        print(f"ERROR: cannot build config.json: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    config._workdir = workdir
    config._config_path = workdir / "config.json"
    try:
        from stream_archive.config import save_config

        save_config(config)
    except ValueError as e:
        print(f"ERROR: cannot write config.json: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    print(f"Wrote {workdir / 'config.json'}. Now pick a control surface.")
    _step_control(config, required=True)
    return config


def main() -> None:
    """Console entry point for ``stream-archive-setup``."""
    while True:
        try:
            try:
                config = get_config()
            except FileNotFoundError:
                config = _fresh_run()
            except ValueError as e:
                print(f"ERROR: config.json is invalid: {e}", file=sys.stderr)
                raise SystemExit(1) from e
            if not os.access(config.workdir, os.W_OK):
                print(
                    f"WARNING: {config.workdir} is not writable, steps cannot save. Fix ownership from the host.",
                    file=sys.stderr,
                )
            _menu_loop(config)
        except _ResetRequested:
            continue
        except _BackToMenu:
            # Abandoned before anything was saved (Ctrl+C in the first
            # run): start over instead of exiting.
            continue
        return
