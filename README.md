# StreamArchive

Polls the Twitch Helix API for your followed channels and records every live
stream via [streamlink](https://streamlink.github.io/), using ad-block playlist
proxies (vendored `streamlink-ttvlol` plugin) so streams playable only via
ad-block workaround still record. Optionally re-streams recordings to
[YouTube Live](https://www.youtube.com/live) and sends Telegram alerts on
live/offline events and start failures. The admin can also manage the
recorder over Telegram — add/remove monitored channels, set retention and
output mode, view status, reload, or restart — with no other user able to
issue commands.

The system is designed to be set-and-forget: failures are logged, alerted
(rate-limited), and retried automatically on the next poll cycle — including
recording processes that die mid-stream.

## Features

- **Multi-channel monitoring** — polls Helix every `monitoring_interval`
  seconds and starts/stops recordings as channels go live/offline.
- **Ad-block proxy support** — playlist URLs from the vendored `streamlink-ttvlol`
  plugin, with `httpproxy://user:pass@host:port` entries for upstream proxies.
- **Three output modes**:
  - `disk` — record `.ts` files into `recording_dir/<channel>/`
  - `youtube` — pipe the stream through `ffmpeg` to a YouTube Live broadcast
  - `both` — disk recording and YouTube re-stream simultaneously
- **Telegram alerts** — live (with title/game/URL), offline (with file size and
  YouTube link), and start-failure (rate-limited to once per 30 minutes per
  channel).
- **Telegram control** — the admin (`telegram_user_id`) can manage the recorder
  over the bot: add/remove monitored channels, set retention and output mode,
  view status, reload `config.json`, or restart the service. Every change is
  validated and persisted atomically, then applied live on the next poll cycle;
  non-admin senders get no reply.
- **Self-healing**:
  - Recording tasks that die mid-stream (ffmpeg crash, disk error, proxy death)
    are detected and restarted on the next poll cycle.
  - YouTube rate-limit / `403` / quota errors fall back to disk recording.
  - Transient Twitch API errors are logged and retried next cycle; unknown user
    ids in a stream response are skipped instead of crashing the poll.
- **Retention cleanup** — optional automatic deletion of recordings older than
  `retention_days`, run at startup and then daily.
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

    Scheduler -->|"every monitoring_interval"| Monitor
    Monitor -->|"resolve user ids + live streams"| Twitch["Twitch Helix API"]
    Monitor -->|"start / stop / restart"| Recorder
    Recorder -->|"proxied playlist → stream"| Streamlink["streamlink"]
    Recorder -->|disk| Disk[".ts files"]
    Recorder -->|youtube| Ffmpeg["ffmpeg<br/>pipe → RTMP"]
    Ffmpeg -->|"re-stream"| YouTube["YouTube Live API"]
    Recorder -->|"live / offline / failures"| Notifier
    Scheduler -->|starts| Telegram
    Telegram -->|"persists atomically"| Config["config.json"]
    Telegram -->|"/add /remove /mode /reload /restart"| Recorder
    Telegram -->|"/remove"| Monitor
```

Recording tasks are tracked; a task that fails raises, its channel entry is
removed, and the monitor restarts the recording on the next poll cycle.

Control plane: `telegram_control` runs alongside the scheduler as a polling
bot. Commands are gated to `telegram_user_id`, validated on a copy, written
atomically to `config.json`, and applied to the running scheduler /
recorder / monitor on the next poll cycle — see
[Telegram control](#telegram-control).

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) — dependency management and the systemd
  unit uses `uv run`
- `ffmpeg` — required for YouTube re-streaming (and used for the pipe)
- `chromium` — required by the vendored plugin for client-integrity token
  acquisition
- **Twitch** app credentials — register at
  <https://dev.twitch.tv/console> (client id + client secret)
- **Telegram** bot token — create one with
  [BotFather](https://t.me/BotFather) and note your user/chat id
- **Google Cloud OAuth client** (`client_secret.json`) — only for
  `output_mode: youtube` or `both`; see
  [YouTube setup](#youtube-setup)

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
2. Run the one-time authorization flow:

   ```sh
   uv run python setup_youtube.py
   ```

   It prints a URL, asks for the authorization code, and saves the token to
   `youtube_token.json` (chmod 600). The token is refreshed automatically
   while it is still refreshable; if it expires irrecoverably, run
   `setup_youtube.py` again.

## Configuration reference

All keys from `config.json.example`:

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `telegram_user_id` | yes | — | Numeric Telegram user/chat id for alerts; sole authorized user of the bot's control commands |
| `bot_telegram_api` | yes | — | Telegram bot token from BotFather |
| `twitch_client_id` | yes | — | Twitch app client id |
| `twitch_client_secret` | yes | — | Twitch app client secret |
| `channels` | yes | — | Non-empty list of channel names to monitor (1–25 chars; first char `[a-zA-Z0-9]`, then `[a-zA-Z0-9_]`) |
| `proxy_list` | yes | — | Non-empty list of ad-block playlist proxies: `httpproxy://user:pass@host:port` or `https://…` URLs |
| `monitoring_interval` | yes | — | Poll interval in seconds; must be > 0 |
| `timezone` | yes | — | IANA timezone (e.g. `Europe/Madrid`) used for filenames and timestamps |
| `plugin_dir` | yes | — | Directory containing the vendored streamlink plugin (`plugins`) |
| `recording_dir` | yes | — | Directory where `.ts` recordings are stored |
| `output_mode` | no | `disk` | `disk`, `youtube`, or `both` |
| `channel_output_modes` | no | `{}` | Per-channel override: `{"channel": "disk" \| "youtube" \| "both"}`; falls back to `output_mode` when absent |
| `retention_days` | no | `0` | Delete recordings older than this many days; `0` disables cleanup |
| `youtube.privacy_status` | no | `unlisted` | `public`, `unlisted`, or `private` |
| `youtube.client_secrets_file` | no | `client_secret.json` | Path to the Google OAuth client secrets JSON |

`output_mode: youtube` additionally requires `youtube_token.json` (see
[YouTube setup](#youtube-setup)).

## Telegram control

The admin user (`telegram_user_id`) can manage the recorder by messaging the
bot; anyone else gets no reply at all. Every change is validated before being
written atomically to `config.json` and takes effect on the next poll cycle —
a failed command leaves both memory and disk untouched.

| Command | Action |
| --- | --- |
| `/help` | List the available commands |
| `/status` | Monitored channels, output mode, retention, monitoring interval, and channels currently recording |
| `/channels` | Numbered list of monitored channels |
| `/add <channel>` | Start monitoring a channel (validated against the channel-name rules) |
| `/remove <channel>` | Stop monitoring a channel; if it is live, stops the recording (sending the offline notification) |
| `/retention <days>` | Set `retention_days`; `0` disables cleanup |
| `/mode [channel] <disk\|youtube\|both\|default>` | Set `output_mode` (no channel) or a per-channel override; `default` clears the override; applies to new recordings |
| `/reload` | Re-read `config.json` from disk |
| `/restart` | Gracefully restart the service |

Notes:

- `/mode` applies to new recordings; an in-flight recording finishes in the
  mode it started with. A per-channel override (`/mode <channel> <mode>`) wins
  over the global `output_mode`; `/status` lists active overrides, and
  `/remove <channel>` clears the channel's override.
- `/retention` and `/reload` apply immediately — the cleanup loop and the
  monitor read the live config every cycle.
- `/restart` replies first, then triggers the scheduler shutdown; the systemd
  unit's `Restart=always` relaunches the service after `RestartSec`. In a
  foreground run it simply exits.
- Secrets (bot token, Twitch credentials, proxy credentials) are never printed
  by `/status` and cannot be changed over Telegram.

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
`WorkingDirectory` and `ExecStart` if you clone elsewhere.

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
| All ad-block proxies fail for a live channel | Channel skipped, one Telegram alert (rate-limited to 30 min/channel), retried next cycle |
| YouTube rate limit / `403` / quota error at broadcast creation | Automatic fallback to disk recording; live alert still sent |
| Other YouTube broadcast-creation error | Task fails loudly; channel restarted next cycle |
| Recording dies mid-stream (ffmpeg killed, disk write error, proxy death) | Entry removed, `Recording task failed` logged, monitor restarts within one poll cycle; no alert if recovery succeeds, alert (rate-limited) only if the restart also fails |
| Transient Twitch API error (token/request) | Logged, nothing acted on, retried next cycle |
| Stream reported for an unknown user id | Skipped with a warning; the poll cycle never crashes |

Alerts are sent at most once per 30 minutes per channel (`FAILURE_NOTIFY_INTERVAL`
in `src/stream_archive/monitor.py`).

## Project layout

```
config.json.example      # template for runtime config (config.json is gitignored)
main.py                  # entrypoint: logging setup + asyncio.run(scheduler)
setup_youtube.py         # one-time YouTube OAuth flow
stream-archive.service   # systemd user unit
pyproject.toml
src/stream_archive/
  scheduler.py           # poll loop, signal handling, daily retention cleanup
  monitor.py             # start/stop/restart decisions, failure alerts
  recorder.py            # streamlink capture, ffmpeg pipe, task tracking
  youtube_streamer.py    # YouTube Live API (broadcast/stream/bind/end)
  twitch_api.py          # Twitch Helix client (token, users, streams)
  notifier.py            # Telegram messages
  telegram_control.py    # admin-only Telegram bot commands (/add /remove /mode …)
  config.py              # config loading + validation
plugins/twitch.py        # vendored streamlink-ttvlol plugin
tests/                   # pytest suite (recorder, monitor, notifier, config, telegram_control)
```

## Plugin maintenance

`plugins/twitch.py` is vendored from
[streamlink-ttvlol](https://github.com/2bc4/streamlink-ttvlol)
(currently version `8.3.0-20260701`, constant `STREAMLINK_TTVLOL_VERSION`).
To refresh, replace the file from upstream and bump the constant; the plugin
logs its version at load. Upstream bugs go to
<https://github.com/2bc4/streamlink-ttvlol/issues>.

## Development

```sh
uv sync        # installs dev group (pytest)
uv run pytest
```

## License

[MIT](LICENSE). The vendored `plugins/twitch.py` retains its own upstream
license.
