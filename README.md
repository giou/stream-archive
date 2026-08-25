# StreamArchive

Monitors Twitch and Kick channels and records every live stream with
[streamlink](https://streamlink.github.io/). Live/offline signals arrive
within seconds on both platforms. A poll at `monitoring_interval` stays as
the fallback. The poll catches missed events, starts channels that are
already live at boot, and restarts recordings that died mid-stream.

Signals take two fast paths:

- **Twitch**: EventSub `stream.online` / `stream.offline` events over one
  conduit WebSocket shard, authenticated with the existing app credentials.
  Recordings run through ad-block playlist proxies (vendored
  `streamlink-ttvlol` plugin), so streams that need an ad-block workaround
  still record.
- **Kick**: signature-verified `livestream.status.updated` and
  `chat.message.sent` webhooks from the Kick Developer API. Recordings use
  the built-in streamlink Kick plugin, which talks to the Kick API directly.

Failures are logged, alerted (rate-limited), and retried on the next poll
cycle. The app can also re-stream recordings to
[YouTube Live](https://www.youtube.com/live) and send alerts and admin
commands over a Telegram bot.

## Features

- One config for both platforms. Channels are identified as `twitch:<name>`
  or `kick:<slug>`.
- Three output modes. `disk` writes recordings to `recording_dir/<channel>/`
  (`.ts`, or `.m4a` for audio-only channels).
  `youtube` pipes the stream through ffmpeg to a YouTube broadcast. `both`
  runs disk and youtube together.
- Live chat recording. The app saves Twitch IRC chat and Kick webhook chat as
  TwitchDownloader-compatible JSON in `chat_dir/<platform>/<channel>/`.
- Retention cleanup. The app deletes recordings older than `retention_days`
  at startup and then daily. An optional `disk.max_total_gb` cap deletes the
  oldest recordings or stops new ones.
- Self-healing. Recording tasks that die mid-stream restart on the next poll.
  YouTube re-streams restart with growing delays. A rolling 24-hour budget of
  10 broadcast creations guards the YouTube daily limit. YouTube quota errors
  fall back to disk recording.
- Telegram alerts. Live (title, game, URL), offline (file size, YouTube
  link), start failures, Kick anti-bot blocks, webhook problems, and service
  lifecycle messages. Repeated failure alerts are limited to one per
  30 minutes per channel.
- Telegram control. The admin manages the recorder over the bot. Commands
  cover channels, retention, output mode, quality, chat recording, limits,
  the Kick webhook, status, reload, and restart. Other users get no reply.
  Every change is validated and written atomically to `config.json`, and
  applies on the next poll cycle.

## Architecture

The scheduler runs the poll loop, signal handling, and retention cleanup.
Each cycle the monitor compares the configured channels against the Twitch
Helix API and the Kick API, then starts, stops, or restarts recording tasks.
The recorder captures with streamlink, writes `.ts`/`.m4a` files or pipes
through ffmpeg to YouTube, and finalizes chat files and broadcasts when a task ends.
The notifier sends Telegram messages.

Two extra services feed the monitor directly. The EventSub client holds one
authenticated WebSocket for Twitch events. The Kick webhook receiver
verifies and deduplicates incoming HTTP events and keeps subscriptions in
sync. The Telegram package runs alongside as an admin-only polling bot. It
validates changes on a copy, writes `config.json` atomically, and applies
them on the next cycle. See [Project layout](#project-layout) for the module
map.

## Requirements

- Docker with the compose plugin (Docker Engine 20.10+ or Docker Desktop).
- Twitch app credentials. Register at <https://dev.twitch.tv/console>.
- Kick app credentials. Only needed for `kick:` channels. Create an app in
  the Kick Developer portal (client id + client secret).
- A Telegram bot token from [BotFather](https://t.me/BotFather). Note your
  user/chat id.
- A Google Cloud OAuth client (`client_secret.json`). Only for
  `output_mode: youtube` or `both` (see [YouTube setup](#youtube-setup)).
- `cloudflared` and/or Tailscale. Only for the Kick webhook tunnel. Both
  ship in the image. The Tailscale funnel option also needs tailscale on the
  host (the tailscaled socket is mounted into the container).

## Quick start

```sh
mkdir ~/stream-archive-data && cd ~/stream-archive-data
curl -LO https://github.com/giou/stream-archive/releases/latest/download/docker-compose.yml
curl -LO https://github.com/giou/stream-archive/releases/latest/download/config.json.example
cp config.json.example config.json
# fill in every key, see the config reference below
docker compose up -d
docker compose logs -f   # follow startup
```

`STREAM_ARCHIVE_DATA` moves the data dir (recordings, chat, tokens,
state) to another disk. Put the variable in `.env` in that folder:

```sh
# ~/stream-archive-data/.env
STREAM_ARCHIVE_DATA=/mnt/bigdisk/stream-archive-data
```

Updates are image pulls:

```sh
docker compose pull && docker compose up -d
```

### YouTube setup

Only needed when `output_mode` is `youtube` or `both`:

1. Create a Google Cloud project and enable the **YouTube Data API v3**.
2. Download an OAuth desktop client as `client_secret.json` (see
   [the guide from Google](https://developers.google.com/youtube/registering_an_application)).
3. Publish the OAuth consent screen: **Google Cloud Console → APIs &
   Services → OAuth consent screen → Audience tab → Publishing status →
   Publish app**. While the app is in *Testing*, refresh tokens expire after
   **7 days** and only test users can authorize. Publishing keeps the token
   valid.
4. Run the one-time authorization flow:

   ```sh
   docker compose run --rm stream-archive stream-archive-setup-youtube
   ```

   The command opens the authorization page in your browser and completes
   automatically. After you authorize, the redirect page shows
   "Authorization successful!" and the command saves the token to
   `youtube_token.json`. If the redirect page cannot load (SSH session,
   Docker, headless host), copy the **full URL** from the address bar.
   Paste the URL at the prompt. Under Docker the localhost redirect cannot
   reach the container, so always paste the full URL. The token refreshes
   automatically while it is refreshable. If it expires irrecoverably, run
   the command again.

## Config reference

All keys from `config.json.example`:

Secrets can come from the environment. You can write a secret as
`${ENV_VAR}`, for example `"bot_telegram_api": "${TELEGRAM_BOT_TOKEN}"`.
The placeholder text stays in `config.json`, so the resolved secret is
never written back. A config with placeholders is safe to commit or share.

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `telegram_user_id` | yes | — | Numeric Telegram user/chat id for alerts and bot control |
| `bot_telegram_api` | yes | — | Telegram bot token from BotFather |
| `twitch_client_id` | yes | — | Twitch app client id |
| `twitch_client_secret` | yes | — | Twitch app client secret |
| `channels` | yes | — | Non-empty list of channels: `twitch:<name>` or `kick:<slug>`. Bare names become `twitch:` on load |
| `proxy_list` | yes | — | Non-empty list of ad-block playlist proxies (Twitch recordings only). `httpproxy://…` entries are ttvlol v2 proxies (optional `httpproxy://user:pass@host:port`). `https://…` entries are v1 |
| `monitoring_interval` | yes | — | Poll interval in seconds, more than 0 |
| `timezone` | yes | — | IANA timezone (for example `America/New_York`) used for filenames and timestamps |
| `plugin_dir` | yes | — | Directory with the streamlink-ttvlol plugin. `/app/plugins` in Docker (baked into the image, read-only). Relative `plugins` for dev runs |
| `recording_dir` | yes | — | Directory for `.ts`/`.m4a` recordings |
| `record_chat` | no | `true` | Record Twitch IRC chat alongside the video. Kick chat has its own flag |
| `chat_dir` | no | `chat` | Directory for chat JSON files (`chat_dir/<platform>/<channel>/<title>-<ts>.chat.json`) |
| `output_mode` | no | `disk` | `disk`, `youtube`, or `both` |
| `channel_output_modes` | no | `{}` | Per-channel override, for example `{"channel": "disk" \| "youtube" \| "both"}`. Channels without an entry use `output_mode` |
| `eventsub.enabled` | no | `true` | Twitch EventSub fast path via conduit. Uses the existing app credentials (no extra setup). `false` = Twitch polling only |
| `kick.client_id` | yes¹ | — | Kick app client id. Required when a `kick:` channel is configured |
| `kick.client_secret` | yes¹ | — | Kick app client secret. Same requirement |
| `kick.record_chat` | no | `true` | Record Kick chat (delivered by the webhook). Requires `kick.webhook.enabled` |
| `kick.webhook.enabled` | no | `false` | Receive Kick webhooks (live/offline + chat) and keep subscriptions in sync. `false` = Kick polling only (no chat) |
| `kick.webhook.listen_host` | no | `127.0.0.1` | Bind address of the receiver. Set to `0.0.0.0` under Docker so the host tunnel reaches it |
| `kick.webhook.listen_port` | no | `8787` | Port of the receiver. The tunnels forward to it |
| `kick.webhook.public_url` | yes² | `""` | Public URL that Kick POSTs to (a host-root URL gets `/kick/webhook` appended). Required when `kick.webhook.enabled` is true |
| `kick.webhook.tunnel` | no | `""` | `cloudflare` or `tailscale` when the bot manages the tunnel. The bot sets this key |
| `kick.webhook.cloudflare_token` | no | `""` | cloudflared tunnel token for a managed Cloudflare tunnel |
| `kick.webhook.cloudflare_managed` | no | `false` | True when the bot started the Cloudflare tunnel itself. Restored on boot |
| `kick.webhook.setup_notified` | no | `false` | Internal: tracks the "webhook is working" confirmation for the current enable |
| `retention_days` | no | `0` | Delete recordings older than this many days. `0` disables cleanup |
| `preferred_quality` | no | `best` | Stream quality requested from streamlink (`best`, `1080p`, `720p`, …, `audio_only`) |
| `channel_preferred_qualities` | no | `{}` | Per-channel quality override, for example `{"channel": "720p"}`. Channels without an entry use `preferred_quality` |
| `max_concurrent_recordings` | no | `0` | Maximum simultaneous recordings. `0` = unlimited |
| `max_concurrent_youtube_streams` | no | `0` | Maximum simultaneous YouTube re-streams. `0` = unlimited |
| `disk.max_total_gb` | no | `0` | Delete oldest recordings when the archive exceeds this size in GB. `0` disables the cap |
| `disk.check_interval_s` | no | `60` | Seconds between disk watchdog checks |
| `disk.delete_oldest` | no | `true` | On breach, delete the oldest recordings. `false` stops new recordings instead |
| `update_check.enabled` | no | `true` | Periodic checks for app, streamlink, and plugin updates, with a Telegram notification when one is available |
| `update_check.interval_hours` | no | `24` | Hours between update checks |
| `update_check.check_app` | no | `true` | Check GitHub releases for a newer release of this app |
| `update_check.check_streamlink` | no | `true` | Check PyPI for a newer `streamlink` release |
| `update_check.check_plugin` | no | `true` | Check the `streamlink-ttvlol` GitHub releases for a newer `twitch.py`. Plugin updates ship in a future image |
| `youtube.client_secrets_file` | no | `client_secret.json` | Path to the Google OAuth client file. Only the YouTube authorization flow uses this file |
| `youtube.privacy_status` | no | `unlisted` | Privacy of created YouTube broadcasts: `public`, `unlisted`, or `private` |
| `youtube.hold_seconds` | no | `0` | Keep the broadcast open this many seconds after the source stops. A return within the delay reuses the same broadcast (no quota cost). A bundled "Reconnecting..." clip feeds the broadcast during the wait. `0` ends the broadcast immediately |
| `channel_youtube_hold_seconds` | no | `{}` | Per-channel override of `youtube.hold_seconds`, for example `{"channel": 60}`. A channel set to `0` is off. Absent = global value. Managed from the Telegram channel submenu |

¹ Required when the channel list contains a `kick:` entry.
² Required when the webhook is enabled.

`output_mode: youtube` additionally requires `youtube_token.json` (see
[YouTube setup](#youtube-setup)).

## Kick webhook setup

The webhook gives near-instant live/offline signals and Kick chat. The poll
alone cannot deliver chat (Kick has no chat replay). Open `/settings` in
Telegram and choose **Kick webhook**, then choose a tunnel option:

- **Cloudflare tunnel**: a *Quick tunnel* (no account, temporary URL) or a
  *Named tunnel* (paste the `cloudflared service install <TOKEN>` command or
  token, pick a hostname). The bot writes the ingress config for a named
  tunnel. It creates the DNS record if you provide a Cloudflare API token,
  and runs cloudflared.
- **Tailscale funnel**: the bot runs `tailscale funnel <port>`. The host
  must run tailscale (under Docker the tailscaled socket is mounted into
  the container).
- **Your own tunnel**: paste the public URL of a tunnel you already run.

The bot probes the URL for reachability and persists its state to
`config.json`. Then register the URL in the Kick app: **Kick → Settings →
Developer → your app → Enable webhooks**, and paste the URL there. The
first verified event from Kick triggers a "Kick webhook is working"
confirmation. The bot tears down its tunnels when you disable the webhook
or switch to another provider. A managed Cloudflare tunnel comes back
automatically on service restart. Its trycloudflare URL can change, and you
get a new notification when it does.

Internals: the receiver is `POST /kick/webhook` on
`kick.webhook.listen_host:listen_port`. Every request is verified against
the published signing key of Kick. Requests with a timestamp outside a
5-minute freshness window get rejected. A captured request cannot be
replayed. Verified events are deduplicated by message id within that
window. Key rotation refetches are rate-limited, and per-client-IP rate
limiting plus a concurrency cap bound floods. Failed requests get `401` and
a log entry. The subscription sync loop reconciles
`livestream.status.updated` + `chat.message.sent` subscriptions against the
monitored Kick channels every poll cycle, and immediately on `/add`,
`/remove`, `/reload`, or enabling. If sync fails (usually the URL is not
registered in the Kick app), one Telegram alert is sent until it recovers.
Webhook delivery is best-effort: the poll covers missed live/offline
events, and chat gaps stay absent from the chat file.

## Live chat recording

When enabled, every recording also captures chat, in every output mode.
Files are written in the `TwitchDownloader` `ChatRoot` format to
`chat_dir/<platform>/<channel>/<title>-<ts>.chat.json`:

- **Twitch**: an IRC connection to the channel chat (`record_chat`, default
  on). Use `chatupdate -E` to embed emotes/badges/avatars into a copy for a
  fully self-contained file.
- **Kick**: buffered `chat.message.sent` webhook events (`kick.record_chat`,
  default on, requires the webhook). Emote images are downloaded and
  embedded as base64, so the file is self-contained.

`TwitchDownloaderCLI` consumes these files directly:

```sh
# enrich the file (embed emotes/badges/avatars) and/or render it:
TwitchDownloaderCLI chatupdate -i chat/<platform>/<channel>/<title>-<ts>.chat.json -o out.chat.json -E
TwitchDownloaderCLI chatrender -i out.chat.json -o chat.mp4
```

StreamArchive only writes the JSON. It does no rendering (no ffmpeg, no
HTML/MP4 generation). Chat is held in memory during the stream and written
atomically on stop. Every termination path (stream offline, disk watchdog
abort, task failure, restart, `SIGTERM`/`SIGINT`) finalizes the file, so a
crash cannot corrupt an existing `.chat.json`. `retention_days` cleanup
also removes old `*.chat.json` files together with the recordings.

## Telegram control

Only the admin user (`telegram_user_id`) gets replies from the bot. The bot
registers a command menu (type `/`). It also offers a `/settings` reply
keyboard. The submenus cover channels, chat recording, output mode,
quality, retention, recording and disk limits, the YouTube hold delay, and
the Kick webhook. Destructive actions (remove
a channel, enable delete-oldest) use inline confirmation buttons. The bot
re-sends the settings menu after every restart, so the reply keyboard
survives updates and reboots. Every change is validated, written atomically
to `config.json`, and applies on the next poll cycle. A failed command
leaves memory and disk untouched.

| Command | Action |
| --- | --- |
| `/help` | List the available commands |
| `/start` | Show the available commands and open the settings menu |
| `/settings` | Open the settings menu (reply keyboard buttons) |
| `/status` | Monitored channels, output mode, retention, chat-recording state, quality, concurrency limits, disk usage/limits, update-check state, Kick webhook state, and channels currently recording |
| `/channels` | Numbered list of monitored channels |
| `/add <channel\|url>` | Start monitoring a channel: `twitch:<name>`, `kick:<name>`, or a `twitch.tv`/`kick.com` profile URL. Subscriptions are created immediately |
| `/remove <channel>` | Stop monitoring a channel. A live recording is stopped (offline notification sent) and its webhook/EventSub subscriptions are deleted |
| `/retention <days>` | Set `retention_days`. `0` disables cleanup |
| `/mode [channel] <disk\|youtube\|both\|default>` | Set `output_mode`, or a per-channel override. `default` clears the override. Applies to new recordings |
| `/reload` | Re-read `config.json` from disk and re-sync webhook/EventSub subscriptions |
| `/restart` | Gracefully restart the service |
| `/update` | Check for updates now (app, streamlink, plugin). Check-only: nothing is downloaded or applied. Apply an app update with `docker compose pull && docker compose up -d` |
| `/quality [channel] <value\|default>` | Show the preferred quality, or set it globally or per channel (`best`, `1080p`, `720p`, …, `audio_only`). `default` clears the per-channel override |
| `/maxrecordings [n]` | Show or set the concurrent recording limit (`0` = unlimited) |
| `/maxyoutube [n]` | Show or set the concurrent YouTube re-stream limit (`0` = unlimited) |
| `/disk` | Show disk limits |
| `/disk <maxsize\|delete_oldest> <value>` | Set a disk limit. `maxsize` takes GB. `delete_oldest` takes `on`/`off` |
| `/chat [on\|off] [twitch\|kick]` | Show whether chat recording is enabled, or enable/disable it (globally, or per platform with `twitch`/`kick`). `off` stops and finalizes in-flight chat capture (video recordings continue) |

Notes:

- `/mode` applies to new recordings. An in-flight recording finishes in the
  mode it started with. A per-channel override wins over the global
  `output_mode`. `/status` lists active overrides, and `/remove` clears the
  override of that channel.
- `audio_only` records sound without video. On Twitch, streamlink supplies an
  audio-only stream. On Kick, ffmpeg strips the video from the 480p stream.
  YouTube does not accept an audio-only live stream. When you select
  `audio_only` for a channel with `youtube` or `both` output, the bot asks
  you to confirm. If you confirm, the bot sets the quality and switches the
  output of that channel to `disk`. If you cancel, nothing changes. The
  recorder also forces `disk` for audio-only channels as a safety net.
  Audio-only recordings are saved as `.m4a`: ffmpeg remuxes the AAC track
  into a fragmented MP4 without re-encoding.
- The per-channel YouTube hold delay lives under
  `/settings → Channels → <channel> → Hold delay` (presets, `0` = off, or a
  custom value in seconds). `Default` clears the override back to the global
  `youtube.hold_seconds`. The value is read when a recording stops, so it
  applies to the next stop immediately.
- `/chat off` applies immediately. In-flight capture is stopped and
  finalized, and new recordings start without chat until `/chat on`.
  `/chat on` affects new recordings only. A platform toggle
  (`/chat off twitch`) affects only that platform, and the other platform
  keeps running.
- `/retention` and `/reload` apply immediately. The cleanup loop and the
  monitor read the live config every cycle.
- `/restart` replies first, then triggers the scheduler shutdown. The
  compose policy `restart: unless-stopped` relaunches the container.
- Secrets (bot token, Twitch credentials, proxy credentials, Kick
  credentials, tunnel tokens) are never printed by `/status` and cannot be
  changed over Telegram.

## Running

The image owns all code. Your data dir owns all state. The container root
filesystem is read-only. Only the mounted data dir and `/tmp` (tmpfs) are
writable.

```sh
docker compose up -d    # start
docker compose logs -f  # follow logs
docker compose stop     # graceful shutdown: recordings stopped, broadcasts ended
```

- The data dir (default: the folder containing `docker-compose.yml`, for
  example `~/stream-archive-data/`) holds `config.json`, `recordings/`, `chat/`,
  `youtube_token.json`, `client_secret.json`, `cloudflared/`, and
  `update_state.json`. Treat it like a normal folder on the host.
  Back up the folder by copying it. Move it with `STREAM_ARCHIVE_DATA` in
  `.env` (see [Quick start](#quick-start)).
- The container runs as the owner of the data dir. Recorded files stay
  manageable on the host. Set `USER_UID` /
  `USER_GID` in `.env` to force a specific identity. If Docker auto-created
  the data dir as root, fix the ownership once with
  `sudo chown -R "$(id -u):$(id -g)" <data-dir>`.
- Log timestamps follow the container timezone (`UTC` by default). Set
  `TZ=America/New_York` in the same `.env` to match the `timezone` in
  config.
- Compose rotates logs (10 MB × 3 files).
- Kick webhook under Docker: a host tailscale funnel forwards to the host
  loopback, and the compose file publishes the receiver on
  `127.0.0.1:8787`. Set `kick.webhook.listen_host` to `"0.0.0.0"` in
  config. The image ships `cloudflared` and the tailscale CLI, and the
  tailscaled socket is mounted from the host.
- One-time YouTube OAuth:
  `docker compose run --rm stream-archive stream-archive-setup-youtube`.
  The browser opens on your host. Paste the full address-bar URL at the
  prompt (the localhost redirect cannot reach the container).

### Logs

```sh
docker compose logs -f
```

`SIGTERM`/`SIGINT` trigger a graceful shutdown. All recordings stop, active
YouTube broadcasts transition to `complete`, and the scheduler exits with
`[scheduler] Shutdown complete`.

## Failure handling

| Failure | Behavior |
| --- | --- |
| All ad-block proxies fail for a live Twitch channel | Channel skipped. One alert (rate-limited to 30 min/channel). Retry on the next cycle |
| Kick blocks recording requests (anti-bot challenge / `403`) | Channel skipped. One alert (rate-limited) with a hint to install a browser on the host. Retry on the next cycle |
| YouTube rate limit / `403` / quota error at broadcast creation | Automatic fallback to disk recording. The live alert still goes out |
| Other YouTube broadcast-creation error | The task fails with an error. Restart on the next cycle |
| Recording dies mid-stream (ffmpeg killed, disk write error, proxy death, stalled feed) | Entry removed (chat + broadcast finalized). Restart on the next cycle. No alert if recovery succeeds, rate-limited alert only if the restart also fails |
| YouTube re-stream keeps ending shortly after start (flaky feed) | Restart delays grow per channel. They double from 120 s to a cap of 30 min. A rolling 24 h budget of 10 broadcast creations across all channels blocks further restarts until a slot frees (one alert with the next-slot time). Guards the daily broadcast limit of YouTube (`userBroadcastsExceedLimit`) |
| Source drops briefly with a hold delay configured | The broadcast stays open, fed by a bundled pre-encoded "Reconnecting..." clip. A return within the delay reuses the same broadcast (no new creation, no quota cost). On expiry the broadcast ends as usual |
| Transient Twitch/Kick API error (token/request) | Logged. Nothing acted on. Retry on the next cycle |
| Kick channel slug not found | Warned once per channel. Treated as offline. Never crashes the poll |
| EventSub connection lost / conduit shard disabled | Auto-reconnect with backoff. The shard re-associates with the new session. Missed events covered by the poll |
| Kick webhook signature verification failed | Request answered `401` and logged. Valid requests keep flowing |
| Kick webhook subscription sync fails (URL not registered in the Kick app) | One alert until the sync recovers. Polling still covers live/offline |
| Stream reported for an unknown user id | Skipped with a warning. The poll never crashes |

Alerts are sent at most once per 30 minutes per channel
(`FAILURE_NOTIFY_INTERVAL` in `src/stream_archive/monitor.py`).

## Project layout

```
config.json.example      # template for runtime config (config.json is gitignored)
docker-compose.yml       # standalone deployment: image + data-dir bind mount
pyproject.toml
.pre-commit-config.yaml  # ruff + ruff-format + mypy hooks
src/stream_archive/
  scheduler.py           # entry point (stream-archive): logging, poll loop, signal handling
  setup_youtube.py       # one-time YouTube OAuth flow (entry point: stream-archive-setup-youtube)
  monitor.py             # start/stop/restart decisions, failure alerts (Twitch + Kick)
  eventsub.py            # Twitch EventSub conduit client (stream.online/offline fast-path)
  kick_webhook.py        # Kick webhook receiver (/kick/webhook), signature verification, subscription sync
  kick_api.py            # Kick OAuth client (token, channel statuses, webhook subscriptions, public key)
  kick_chat.py           # Kick chat -> TwitchDownloader ChatRoot conversion + emote embedding
  recorder/              # streamlink capture, ffmpeg pipe, task tracking, chat finalization (core + mixins)
  chat_recorder.py       # Twitch IRC chat capture (TwitchDownloader-compatible JSON)
  youtube_streamer.py    # YouTube Live API (broadcast/stream/bind/end)
  twitch_api.py          # Twitch Helix client (token, users, streams)
  notifier.py            # Telegram messages
  telegram/              # admin-only Telegram bot commands (/add /remove /mode …) + settings menus
  config.py              # typed config (Pydantic) + ${ENV_VAR} interpolation
  updater.py             # periodic update checks (app / streamlink / plugin)
  disk.py                # disk-size watchdog (max_total_gb)
plugins/twitch.py        # dev-only: fetched from streamlink-ttvlol releases (baked into the image at build)
tests/                   # pytest suite (config, recorder, monitor, eventsub, kick api/webhook/chat, telegram, …)
```

At runtime all state lives in the data dir (see [Running](#running)). The
repository itself is only needed for development.

## Development

```sh
uv sync        # installs dev group (pytest, ruff, mypy, pre-commit)
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pre-commit run --all-files
```

## License

[MIT](LICENSE). The image contains the third-party `twitch.py` plugin
(streamlink-ttvlol). The plugin is fetched at build from upstream releases
and keeps its upstream license.
