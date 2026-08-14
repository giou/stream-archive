# StreamArchive

Monitors Twitch **and** Kick channels and records every live stream via
[streamlink](https://streamlink.github.io/), with near-instant live/offline
signals on both platforms:

- **Twitch** — EventSub `stream.online` / `stream.offline` delivered over a
  conduit WebSocket shard (authenticated with the existing app credentials,
  covers ALL configured channels). Twitch streams are recorded through
  ad-block playlist proxies (vendored `streamlink-ttvlol` plugin) so streams
  playable only via an ad-block workaround still record.
- **Kick** — signature-verified webhooks (`livestream.status.updated` and
  `chat.message.sent`) via the Kick Developer API. Kick streams are recorded
  with streamlink's built-in Kick plugin, which talks to Kick's API directly —
  no proxy loop needed.

On both platforms a poll at `monitoring_interval` stays as the
reconciliation/fallback: it catches events the fast paths missed (neither has
replay), picks up channels already live at boot, and restarts recordings that
died mid-stream. Optionally re-streams recordings to
[YouTube Live](https://www.youtube.com/live) and sends Telegram alerts on
live/offline events and start failures. The admin can also manage the
recorder over Telegram — add/remove monitored channels, set retention and
output mode, toggle chat recording (per platform), set up the Kick webhook
tunnel, view status, reload, or restart — with no other user able to issue
commands.

The system is designed to be set-and-forget: failures are logged, alerted
(rate-limited), and retried automatically on the next poll cycle — including
recording processes that die mid-stream.

## Features

- **Multi-platform monitoring** — one config, both platforms; channels are
  identified as `twitch:<name>` or `kick:<slug>`. Twitch EventSub and Kick
  webhooks start/stop recordings within seconds; the poll reconciles state,
  covers outages, boot-time already-live channels, and recordings that died
  mid-stream.
- **Kick webhooks** — `livestream.status.updated` (live/offline) and
  `chat.message.sent` (chat) v1 events, verified against Kick's published
  signing key (RSA PKCS1v15/SHA-256 over `message_id.timestamp.body`, with a
  key-rotation refetch) and protected against replay by a signed-timestamp
  window. Subscriptions are auto-created for monitored Kick
  channels and reconciled every poll cycle; the bot also drives the whole
  setup — tunnel included — from `/settings` (see
  [Kick webhook setup](#kick-webhook-setup)).
- **Ad-block proxy support (Twitch)** — playlist URLs from the vendored
  `streamlink-ttvlol` plugin, with `httpproxy://user:pass@host:port` entries
  for upstream proxies. Kick recordings use streamlink's built-in Kick plugin
  instead (it solves Kick's anti-bot challenge automatically when a browser
  is installed on the host).
- **Three output modes**:
  - `disk` — record `.ts` files into `recording_dir/<channel>/`
  - `youtube` — pipe the stream through `ffmpeg` to a YouTube Live broadcast
  - `both` — disk recording and YouTube re-stream simultaneously
- **Telegram alerts** — live (with title/game/URL), offline (with file size
  and YouTube link), start-failure (rate-limited to once per 30 minutes per
  channel), Kick anti-bot blocks, webhook problems, and service lifecycle
  messages (startup with the monitored channels and app version, and
  shutdown/restart).
- **Telegram control** — the admin (`telegram_user_id`) can manage the
  recorder over the bot: add/remove monitored channels (both platforms), set
  retention and output mode, toggle chat recording per platform, set up the
  Kick webhook, view status, reload `config.json`, or restart the service.
  Every change is validated and persisted atomically, then applied live on
  the next poll cycle; non-admin senders get no reply.
- **Self-healing**:
  - Recording tasks that die mid-stream (ffmpeg crash, disk error, proxy
    death) are detected and restarted on the next poll cycle.
  - YouTube rate-limit / `403` / quota errors fall back to disk recording.
  - Transient Twitch/Kick API errors are logged and retried next cycle;
    unknown ids in a response are skipped instead of crashing the poll.
- **Live chat recording** — Twitch IRC chat and Kick webhook chat are both
  captured while a stream is being recorded and written as
  TwitchDownloader-compatible chat JSON into `chat_dir/<platform>/<channel>/`
  (usable directly with `TwitchDownloaderCLI chatupdate` / `chatrender`).
- **Retention cleanup** — optional automatic deletion of recordings older
  than `retention_days`, run at startup and then daily; optional
  `disk.max_total_gb` cap that deletes oldest recordings or stops new ones.
- **YouTube Live integration** — private/unlisted/public broadcasts, DVR
  enabled, automatic start/stop, and clean broadcast ending on shutdown.

## Architecture

```mermaid
flowchart TD
    Scheduler["scheduler<br/>poll loop · signal handling · retention cleanup"]
    Monitor["monitor"]
    Recorder["recorder"]
    Notifier["notifier"]
    Telegram["telegram_control<br/>admin-only bot commands"]
    EventSub["eventsub<br/>conduit WebSocket"]
    KickWebhook["kick_webhook<br/>HTTP receiver · subscription sync"]
    KickAPI["kick_api<br/>OAuth client · channel statuses · subscriptions"]

    Scheduler -->|"every monitoring_interval"| Monitor
    Scheduler -->|starts| EventSub
    EventSub -->|"stream.online / stream.offline"| Monitor
    KickWebhook -->|"livestream.status.updated"| Monitor
    KickWebhook -->|"chat.message.sent"| Recorder
    Monitor -->|"resolve user ids + live streams"| Twitch["Twitch Helix API"]
    Monitor -->|"channel statuses"| KickAPI
    KickWebhook -->|"create / list / delete subscriptions"| KickAPI
    Monitor -->|"start / stop / restart"| Recorder
    Recorder -->|"proxied playlist → stream"| Streamlink["streamlink"]
    Recorder -->|"kick.com URL → stream"| Streamlink
    Recorder -->|disk| Disk[".ts files"]
    Recorder -->|youtube| Ffmpeg["ffmpeg<br/>pipe → RTMP"]
    Ffmpeg -->|"re-stream"| YouTube["YouTube Live API"]
    Recorder -->|"live / offline / failures"| Notifier
    Scheduler -->|starts| Telegram
    Telegram -->|"persists atomically"| Config["config.json"]
    Telegram -->|"/add /remove /mode /reload /restart"| Recorder
    Telegram -->|"/remove"| Monitor
    Telegram -->|"webhook setup · tunnel mgmt"| KickWebhook
```

Recording tasks are tracked; a task that fails raises, its channel entry is
removed, and the monitor restarts the recording on the next poll cycle.

Control plane: `telegram_control` runs alongside the scheduler as a polling
bot. Commands are gated to `telegram_user_id`, validated on a copy, written
atomically to `config.json`, and applied to the running scheduler /
recorder / monitor / webhook on the next poll cycle — see
[Telegram control](#telegram-control).

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) — dependency management and the systemd
  unit uses `uv run`
- `ffmpeg` — required for YouTube re-streaming (and used for the pipe)
- **Twitch** app credentials — register at
  <https://dev.twitch.tv/console> (client id + client secret)
- **Kick** app credentials — only when `kick:` channels are configured;
  create an app in Kick's Developer portal (client id + client secret)
- **Telegram** bot token — create one with
  [BotFather](https://t.me/BotFather) and note your user/chat id
- **Google Cloud OAuth client** (`client_secret.json`) — only for
  `output_mode: youtube` or `both`; see
  [YouTube setup](#youtube-setup)
- **cloudflared** and/or **Tailscale** — only for the Kick webhook tunnel;
  the bot runs whichever you choose from `/settings`

## Quick start

```sh
uv sync
cp config.json.example config.json
# fill in every key — see the configuration reference below
uv run python main.py
```

The config file is looked up in the current directory and then in the
repository root, so run from anywhere inside the checkout.

### YouTube setup

Only needed when `output_mode` is `youtube` or `both`:

1. Create a Google Cloud project, enable the **YouTube Data API v3**, and
   download an OAuth desktop client as `client_secret.json` (see
   [Google's guide](https://developers.google.com/youtube/registering_an_application)).
2. Publish the OAuth consent screen so the token does not expire:
   **Google Cloud Console → APIs & Services → OAuth consent screen → Audience
   tab → Publishing status → Publish app** (set to *In production*). While
   the app is *Testing*, refresh tokens expire after **7 days** (you would
   have to re-run `setup_youtube.py` weekly) and only test users can
   authorize.
3. Run the one-time authorization flow:

   ```sh
   uv run python setup_youtube.py
   ```

   It opens the authorization page in your browser and completes
   automatically: after you authorize, the redirect page shows
   "Authorization successful!" and the token is saved to `youtube_token.json`
   (chmod 600). If the redirect page cannot load — SSH session, Docker,
   headless box — copy the **full URL** from the address bar and paste it
   when prompted. The token is refreshed automatically while it is still
   refreshable; if it expires irrecoverably, run `setup_youtube.py` again.

## Configuration reference

All keys from `config.json.example`:

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `telegram_user_id` | yes | — | Numeric Telegram user/chat id for alerts; sole authorized user of the bot's control commands |
| `bot_telegram_api` | yes | — | Telegram bot token from BotFather |
| `twitch_client_id` | yes | — | Twitch app client id |
| `twitch_client_secret` | yes | — | Twitch app client secret |
| `channels` | yes | — | Non-empty list of channel identities: `twitch:<name>` or `kick:<slug>` (bare names are normalized to `twitch:` on load) |
| `proxy_list` | yes | — | Non-empty list of ad-block playlist proxies: `httpproxy://…` entries are ttvlol v2 proxies (optionally `httpproxy://user:pass@host:port`), `https://…` entries are v1; Twitch recordings only |
| `monitoring_interval` | yes | — | Poll interval in seconds; must be > 0 |
| `timezone` | yes | — | IANA timezone (e.g. `America/New_York`) used for filenames and timestamps |
| `plugin_dir` | yes | — | Directory containing the vendored streamlink plugin (`plugins`) |
| `recording_dir` | yes | — | Directory where `.ts` recordings are stored |
| `record_chat` | no | `true` | Record Twitch IRC chat alongside the video; `false` disables it (Kick chat has its own flag) |
| `chat_dir` | no | `chat` | Directory where chat JSON files are stored (`chat_dir/<platform>/<channel>/<title>-<ts>.chat.json`) |
| `output_mode` | no | `disk` | `disk`, `youtube`, or `both` |
| `channel_output_modes` | no | `{}` | Per-channel override: `{"channel": "disk" \| "youtube" \| "both"}`; falls back to `output_mode` when absent |
| `eventsub.enabled` | no | `true` | Twitch EventSub fast-path via conduit (uses the existing app credentials; no extra setup); `false` = Twitch polling only |
| `kick.client_id` | yes¹ | — | Kick app client id — **required when any `kick:` channel is configured** |
| `kick.client_secret` | yes¹ | — | Kick app client secret — same requirement |
| `kick.record_chat` | no | `true` | Record Kick chat (delivered via webhook) alongside the video; requires `kick.webhook.enabled` |
| `kick.webhook.enabled` | no | `false` | Receive Kick webhooks (live/offline + chat) and keep subscriptions in sync; `false` = Kick polling only (no chat) |
| `kick.webhook.listen_host` | no | `127.0.0.1` | Address the receiver binds (`0.0.0.0` under Docker so the host's tunnel can reach it) |
| `kick.webhook.listen_port` | no | `8787` | Port the receiver binds; the bot's tunnels forward to it |
| `kick.webhook.public_url` | yes² | `""` | Public URL Kick POSTs to (a host-root URL gets `/kick/webhook` appended) — **required when `kick.webhook.enabled` is true** |
| `kick.webhook.tunnel` | no | `""` | `cloudflare` or `tailscale` when the bot manages the tunnel; set by the bot, not by hand |
| `kick.webhook.cloudflare_token` | no | `""` | cloudflared tunnel token when the bot runs a managed Cloudflare tunnel |
| `kick.webhook.cloudflare_managed` | no | `false` | True when the bot started the Cloudflare tunnel itself (restored on boot) |
| `kick.webhook.setup_notified` | no | `false` | Internal: whether the "webhook is working" confirmation has been sent for the current enable |
| `retention_days` | no | `0` | Delete recordings older than this many days; `0` disables cleanup |
| `preferred_quality` | no | `best` | Stream quality to request from streamlink (`best`, `1080p`, `720p`, …); falls back to `best` |
| `max_concurrent_recordings` | no | `0` | Maximum simultaneous recordings; `0` = unlimited |
| `max_concurrent_youtube_streams` | no | `0` | Maximum simultaneous YouTube re-streams; `0` = unlimited |
| `disk.max_total_gb` | no | `0` | Delete oldest recordings when the archive exceeds this (GB); `0` = disabled |
| `disk.check_interval_s` | no | `60` | How often the disk watchdog re-checks the archive size |
| `disk.delete_oldest` | no | `true` | Delete oldest recordings when `disk.max_total_gb` is exceeded; `false` stops new recordings instead |
| `update_check.enabled` | no | `true` | Periodically check the app, streamlink, and the vendored plugin for updates and send a Telegram notification when one is available |
| `update_check.interval_hours` | no | `24` | How often to run the update check (hours) |
| `update_check.check_app` | no | `true` | Check the app repo (`git fetch origin`) for new commits |
| `update_check.check_streamlink` | no | `true` | Check PyPI for a newer `streamlink` release |
| `update_check.check_plugin` | no | `true` | Check the `streamlink-ttvlol` GitHub releases for a newer `plugins/twitch.py`; a download is applied only when the release publishes a matching sha256 digest, and the file must be valid Python declaring the new version |
| `youtube.privacy_status` | no | `unlisted` | `public`, `unlisted`, or `private` |
| `youtube.client_secrets_file` | no | `client_secret.json` | Path to the Google OAuth client secrets JSON |

¹ Required when the channel list contains a `kick:` entry. ² Required when
the webhook is enabled.

`output_mode: youtube` additionally requires `youtube_token.json` (see
[YouTube setup](#youtube-setup)).

## Kick webhook setup

The webhook gives near-instant live/offline signals **and** Kick chat — the
poll alone cannot deliver chat (Kick has no chat replay). Setup is driven
from Telegram: `/settings → Kick webhook`, then choose:

- **Cloudflare tunnel** — *Quick tunnel* (no account, temporary URL) or
  *Named tunnel* (paste the `cloudflared service install <TOKEN>` command or
  token, pick a hostname; the bot writes the ingress config, creates the DNS
  record if you give it a Cloudflare API token, and runs cloudflared).
- **Tailscale funnel** — the bot runs `tailscale funnel <port>` (the host
  must run tailscale; under Docker the tailscaled socket is mounted into the
  container).
- **Your own tunnel** — paste the public URL of a tunnel you already run.

The bot probes the URL for reachability, persists the state to `config.json`,
and then you register it in the Kick app: **Kick → Settings → Developer →
your app → Enable webhooks** and paste the URL there. The first
signature-verified event from Kick sends a "Kick webhook is working"
confirmation. App-managed tunnels are torn down when the webhook is disabled
or switched to another provider, and a managed Cloudflare tunnel is restored
automatically on service restart (its trycloudflare URL may have changed —
you get a new notification if so).

Internals: the receiver is `POST /kick/webhook` on
`kick.webhook.listen_host:listen_port`. Every request is verified against
Kick's published signing key (the `Kick-Event-Signature` header, over
`Kick-Event-Message-Id.Kick-Event-Message-Timestamp.<body>`) and rejected
unless the signed timestamp is within a 5-minute freshness window — so a
captured request cannot be replayed to kill recordings or forge chat lines.
Verified events are deduplicated by message id within that window, the
public-key rotation refetch is rate-limited, and per-client-IP rate limiting
plus a concurrency cap bound floods; failures get `401` and are logged. The
subscription sync loop reconciles
`livestream.status.updated` + `chat.message.sent` subscriptions against the
monitored Kick channels every poll cycle (and immediately on `/add`,
`/remove`, `/reload`, or enabling). If sync fails — usually the URL is not
registered in the Kick app — one Telegram alert is sent until it recovers.
Webhook delivery is best-effort: missed events are covered by the polling
cycle (live/offline) and chat gaps are simply absent from the chat file.

## Live chat recording

When enabled (Twitch: `record_chat`, default on; Kick: `kick.record_chat`,
default on, requires the webhook), every recording — in any output mode
(`disk`, `youtube`, or `both`) — also captures chat and writes a chat JSON in
the `TwitchDownloader` `ChatRoot` format to
`chat_dir/<platform>/<channel>/<title>-<ts>.chat.json`:

- **Twitch** — an IRC connection to the channel's chat (`chat/twitch/<name>/`).
- **Kick** — `chat.message.sent` webhook events buffered during the stream
  (`chat/kick/<slug>/`); emote images are downloaded and embedded into the
  file as base64 `embeddedData.firstParty` so the result is self-contained.

It is the format `TwitchDownloaderCLI` consumes directly:

```sh
# enrich the file (embed emotes/badges/avatars) and/or render it:
TwitchDownloaderCLI chatupdate -i chat/<platform>/<channel>/<title>-<ts>.chat.json -o out.chat.json -E
TwitchDownloaderCLI chatrender -i out.chat.json -o chat.mp4
```

StreamArchive only **saves the JSON** — it performs no rendering (no ffmpeg,
no HTML/MP4 generation). Emotes and badges are parsed into the file's
`fragments`/`emoticons`/`user_badges` fields; use `chatupdate -E` to embed the
artwork into a copy for Twitch chat if you want a fully self-contained file.
Chat is held in memory for the stream and written atomically on stop (every
termination path — stream going offline, disk watchdog abort, recording-task
failure, restart, `SIGTERM`/`SIGINT` — finalizes the file), so a crash can
never corrupt an existing `.chat.json`. `retention_days` cleanup also removes
old `*.chat.json` files alongside the `.ts` recordings.

## Telegram control

The admin user (`telegram_user_id`) can manage the recorder by messaging the
bot; anyone else gets no reply at all. The bot registers a command menu (type
`/`) for the admin and a `/settings` reply keyboard (buttons above the input
bar) that covers every setting in submenus — channels, chat recording
(Twitch/Kick), output mode, quality, retention, recording limits, disk
limits, and the Kick webhook setup wizard; inline buttons are used only for
destructive confirmations (removing a channel, enabling delete-oldest).
The bot re-sends the settings menu after every restart, so the reply keyboard
keeps working across code updates and reboots without re-typing `/settings`.
Every change is validated before being
written atomically to `config.json` and takes effect on the next poll cycle —
a failed command leaves both memory and disk untouched.

| Command | Action |
| --- | --- |
| `/help` | List the available commands |
| `/start` | Show the available commands and open the settings menu |
| `/settings` | Open the settings menu (reply keyboard buttons): channels, chat recording, output mode, quality, retention, recording limits, disk limits, Kick webhook |
| `/status` | Monitored channels, output mode, retention, chat-recording state, quality, concurrency limits, disk usage/limits, update-check state, Kick webhook state, and channels currently recording |
| `/channels` | Numbered list of monitored channels |
| `/add <channel\|url>` | Start monitoring a channel: `twitch:<name>`, `kick:<name>`, or a `twitch.tv`/`kick.com` profile URL (stored canonically; Kick webhook/EventSub subscriptions are created immediately) |
| `/remove <channel>` | Stop monitoring a channel; if it is live, stops the recording (sending the offline notification) and deletes its webhook/EventSub subscriptions |
| `/retention <days>` | Set `retention_days`; `0` disables cleanup |
| `/mode [channel] <disk\|youtube\|both\|default>` | Set `output_mode` (no channel) or a per-channel override; `default` clears the override; applies to new recordings |
| `/reload` | Re-read `config.json` from disk (re-syncs webhook/EventSub subscriptions) |
| `/restart` | Gracefully restart the service |
| `/update` | Check for updates now and apply any available; restarts after app/plugin changes, and in Docker reports when an image rebuild is required |
| `/quality [value]` | Show the preferred quality, or set it (`best`, `1080p`, `720p`, …) |
| `/maxrecordings [n]` | Show or set the concurrent recording limit (`0` = unlimited) |
| `/maxyoutube [n]` | Show or set the concurrent YouTube re-stream limit (`0` = unlimited) |
| `/disk` | Show disk limits |
| `/disk <maxsize\|delete_oldest> <value>` | Set a disk limit (`maxsize` takes GB, `delete_oldest` takes `on`/`off`) |
| `/chat [on\|off] [twitch\|kick]` | Show whether chat recording is enabled, or enable/disable it (per platform with `twitch` or `kick`); `off` also stops and finalizes in-flight chat capture (the video recordings continue) |

Notes:

- `/mode` applies to new recordings; an in-flight recording finishes in the
  mode it started with. A per-channel override (`/mode <channel> <mode>`) wins
  over the global `output_mode`; `/status` lists active overrides, and
  `/remove <channel>` clears the channel's override.
- `/chat off` applies immediately: in-flight chat capture is stopped and
  finalized (the video recordings continue), and new recordings start without
  chat until `/chat on`; `/chat on` affects new recordings only.
- `/chat off twitch` / `/chat off kick` (and the `on` variants) toggle only
  that platform: Twitch IRC chat (`record_chat`) or Kick webhook chat
  (`kick.record_chat`). The other platform's in-flight capture keeps running.
- `/retention` and `/reload` apply immediately — the cleanup loop and the
  monitor read the live config every cycle.
- `/restart` replies first, then triggers the scheduler shutdown; the systemd
  unit's `Restart=always` relaunches the service after `RestartSec`. In a
  foreground run it simply exits.
- Secrets (bot token, Twitch credentials, proxy credentials, Kick
  credentials, tunnel tokens) are never printed by `/status` and cannot be
  changed over Telegram.

## Running

### Foreground

```sh
uv run python main.py
```

### As a systemd user unit

```sh
mkdir -p ~/.config/systemd/user
cp stream-archive.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now stream-archive
```

The unit hard-codes the checkout at `~/stream-archive` — adjust
`WorkingDirectory` and `ExecStart` if you clone elsewhere. For the Kick
webhook's Tailscale funnel, tailscale must be installed and running on the
host.

### Docker

Any clone of the repo works — the checkout is bind-mounted read-write at
`/app`, so the container uses your `config.json`, `recordings/`, plugins, and
tokens exactly like a host run. App code changes need no image rebuild.

```sh
cp config.json.example config.json   # fill in every key — see Configuration reference
docker compose up -d --build         # build the image and start
docker compose logs -f               # follow logs
docker compose stop                  # graceful shutdown: recordings stopped, broadcasts ended
```

- `config.json`, `client_secret.json`, `youtube_token.json`, and recordings
  never enter the image (`.dockerignore`) — they live only in your checkout.
- The container runs as uid/gid 1000 by default so recorded files stay
  manageable on the host. If your uid differs, create a `.env` next to the
  compose file with `USER_UID=<uid>` and `USER_GID=<gid>`.
- Log timestamps follow the container timezone (`UTC` by default); set
  `TZ=America/New_York` in the same `.env` to match `config.json`'s `timezone`.
- Kick webhook under Docker: the host's tailscale funnel forwards to the
  host loopback, and the compose file publishes the receiver on
  `127.0.0.1:8787` — set `kick.webhook.listen_host` to `"0.0.0.0"` in
  `config.json`. The image ships `cloudflared` and the tailscale CLI; the
  tailscaled socket is mounted from the host.
- Updates: app and plugin changes from Telegram `/update` apply immediately
  (the mounted code runs directly; the service restarts after them).
  Streamlink differs: on a host (systemd) run `/update` also runs `uv sync`,
  so the new streamlink is active after the restart. Inside the container the
  image is the source of truth for the venv (`/opt/venv`), so `/update` only
  rewrites `uv.lock` and the reply tells you to run `docker compose up -d
  --build` — the running streamlink is unchanged until you rebuild.
- One-time YouTube OAuth: `docker compose run --rm stream-archive setup_youtube.py`
  (the browser opens on your host; the localhost redirect can't reach the
  container, so paste the full address-bar URL when prompted)

### Logs

```sh
journalctl --user -u stream-archive -f
```

`SIGTERM`/`SIGINT` trigger a graceful shutdown: all recordings stop, active
YouTube broadcasts are transitioned to `complete`, and the scheduler exits
with `[scheduler] Shutdown complete`.

## Failure handling & recovery

| Failure | Behavior |
| --- | --- |
| All ad-block proxies fail for a live Twitch channel | Channel skipped, one Telegram alert (rate-limited to 30 min/channel), retried next cycle |
| Kick blocks recording requests (anti-bot challenge / `403`) | Channel skipped, one Telegram alert (rate-limited to 30 min/channel) with a hint to install a browser on the host, retried next cycle |
| YouTube rate limit / `403` / quota error at broadcast creation | Automatic fallback to disk recording; live alert still sent |
| Other YouTube broadcast-creation error | Task fails loudly; channel restarted next cycle |
| Recording dies mid-stream (ffmpeg killed, disk write error, proxy death) | Entry removed, `Recording task failed` logged, monitor restarts within one poll cycle; no alert if recovery succeeds, alert (rate-limited) only if the restart also fails |
| Transient Twitch API error (token/request) | Logged, nothing acted on, retried next cycle |
| Transient Kick API error (token/request) | Logged, nothing acted on, retried next cycle |
| Kick channel slug not found | Warned once per channel, treated as offline; never crashes the poll |
| EventSub connection lost / conduit shard disabled | Auto-reconnect with backoff; shard re-associated with the new WebSocket session; missed events covered by the polling cycle |
| Kick webhook signature verification failed | Request answered `401`, logged; valid requests keep flowing |
| Kick webhook subscription sync fails (e.g. URL not registered in the Kick app) | One Telegram alert until the sync recovers; polling still covers live/offline |
| Stream reported for an unknown user id | Skipped with a warning; the poll cycle never crashes |

Alerts are sent at most once per 30 minutes per channel (`FAILURE_NOTIFY_INTERVAL`
in `src/stream_archive/monitor.py`).

## Project layout

```
config.json.example      # template for runtime config (config.json is gitignored)
main.py                  # entrypoint: logging setup + asyncio.run(scheduler)
setup_youtube.py         # one-time YouTube OAuth flow (auto-captures the code, or paste the redirect URL)
stream-archive.service   # systemd user unit
pyproject.toml
src/stream_archive/
  scheduler.py           # poll loop, signal handling, daily retention cleanup
  monitor.py             # start/stop/restart decisions, failure alerts (Twitch + Kick)
  eventsub.py            # Twitch EventSub conduit client (stream.online/offline fast-path)
  kick_webhook.py        # Kick webhook receiver (/kick/webhook), signature verification, subscription sync
  kick_api.py            # Kick OAuth client (token, channel statuses, webhook subscriptions, public key)
  kick_chat.py           # Kick chat -> TwitchDownloader ChatRoot conversion + emote embedding
  recorder.py            # streamlink capture, ffmpeg pipe, task tracking, chat finalization
  chat_recorder.py       # Twitch IRC chat capture (TwitchDownloader-compatible JSON)
  youtube_streamer.py    # YouTube Live API (broadcast/stream/bind/end)
  twitch_api.py          # Twitch Helix client (token, users, streams)
  notifier.py            # Telegram messages
  telegram_control.py    # admin-only Telegram bot commands (/add /remove /mode …) + settings menus
  config.py              # config loading + validation
  updater.py             # periodic update checks (app / streamlink / plugin)
  disk.py                # disk-size watchdog (max_total_gb)
plugins/twitch.py        # vendored streamlink-ttvlol plugin
tests/                   # pytest suite (config, recorder, monitor, eventsub, kick api/webhook/chat, telegram_control, …)
```

## Development

```sh
uv sync        # installs dev group (pytest)
uv run pytest
```

## License

[MIT](LICENSE). The vendored `plugins/twitch.py` retains its own upstream
license.
