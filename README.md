# StreamArchive

StreamArchive monitors Twitch and Kick channels and records every live stream
with [streamlink](https://streamlink.github.io/). The app can also re-stream
recordings to [YouTube Live](https://www.youtube.com/live) and send alerts and
admin commands over a Telegram bot.

Live and offline signals arrive within seconds on both platforms. A poll at
`monitoring_interval` stays as the fallback. The poll catches missed events,
starts channels that are already live at boot, and restarts recordings that
died mid-stream. The app logs each failure, sends an alert, and retries on the
next poll cycle.

Two fast paths deliver the signals:

- **Twitch.** The EventSub client holds one conduit WebSocket shard for the
  `stream.online` and `stream.offline` events, and authenticates with the app
  credentials. Recordings run through ad-block playlist proxies (the vendored
  `streamlink-ttvlol` plugin). Thus streams that need an ad-block workaround
  still record.
- **Kick.** The app receives signature-verified `livestream.status.updated`
  and `chat.message.sent` webhooks from the Kick Developer API. Recordings use
  the built-in streamlink Kick plugin, which talks to the Kick API directly.

## Features

- **One settings file for both platforms.** A channel name is `twitch:<name>`
  or `kick:<slug>`.
- **Three output modes.** `disk` writes recordings to `recording_dir/<channel>/`
  as `.ts`, or as `.m4a` for audio-only channels. `youtube` pipes the stream
  through ffmpeg to a YouTube broadcast. `both` runs disk and youtube together.
- **Chat recording.** The app saves Twitch IRC chat and Kick webhook chat as
  TwitchDownloader-compatible JSON in `chat_dir/<platform>/<channel>/`. It
  writes each file while the recording runs.
  See [Chat recording](docs/chat-recording.md).
- **Retention cleanup.** The app deletes recordings and chat files older than
  `retention_days` at startup and then daily. An optional `disk.max_total_gb`
  cap deletes the oldest archive files or stops new recordings.
- **Self-healing.** Recording tasks that die mid-stream restart on the next
  poll cycle. YouTube re-streams restart with growing delays. A rolling 24-hour
  budget of 10 broadcast creations guards the YouTube daily limit. YouTube
  quota errors fall back to disk recording.
- **Telegram alerts.** A live alert holds the stream title, the game, and the
  URL. An offline alert holds the file size and the YouTube link. Alerts also
  cover start failures, Kick anti-bot blocks, webhook problems, and app
  lifecycle messages. Repeated failure alerts are limited to one per 30 minutes
  for each channel.
- **Telegram control.** The admin manages the recorder over the bot. Commands
  cover channels, retention, output mode, quality, chat recording, limits, the
  Kick webhook, status, reload, and restart. Other users get no reply. The
  **Remote access** menu holds the Kick webhook and the HTTP control API. The
  app validates each change and writes it atomically to `config.json`. The
  change applies on the next poll cycle. The [web panel](docs/web-control.md)
  replaces the bot in the browser and needs no Telegram token.

## Architecture

The scheduler runs the poll loop, the signal handling, and the retention
cleanup. Each cycle the monitor compares the configured channels against the
Twitch Helix API and the Kick API. Then it starts, stops, or restarts recording
tasks. The recorder captures with streamlink, writes `.ts`/`.m4a` files, or
pipes the stream through ffmpeg to YouTube. When a task ends, it finalizes the
chat file and the broadcast. The notifier sends Telegram messages.

Two services feed the monitor directly. The EventSub client holds one
authenticated WebSocket for Twitch events. The Kick webhook receiver verifies
and deduplicates incoming HTTP events and keeps the subscriptions in sync. Its
listener also serves the control API under `/api/v1` and the web panel under
`/web/`. The Telegram bot runs
alongside as an admin-only polling bot. It validates each change on a copy,
writes `config.json` atomically, and applies the change on the next cycle. The
control API and the web panel call the same command layer, so all paths behave in the same way.
See [Development](docs/development.md) for the module map.

## Requirements

- Docker with the compose plugin (Docker Engine 20.10 or later, or Docker
  Desktop).
- Twitch app credentials from <https://dev.twitch.tv/console>.
- Kick app credentials for `kick:` channels. Create an app in the Kick
  Developer portal (client id and client secret).
- A Telegram bot token from [BotFather](https://t.me/BotFather), and your user
  or chat id. Optional: set the id to `0` and the token to `""` to disable
  the bot and use the [web panel](docs/web-control.md) instead.
- A Google Cloud OAuth client (`client_secret.json`) for `output_mode: youtube`
  or `both`. See [YouTube setup](docs/youtube-setup.md).
- A public address for the Kick webhook and, optionally, the panel.
  You publish it yourself: `tailscale serve` for tailnet-only panel
  access, or your own reverse proxy (cloudflared you run, nginx, or
  anything with TLS) for the public entries. The setup wizard generates
  the proxy config.

## Quick start

```sh
mkdir ~/stream-archive-data && cd ~/stream-archive-data
curl -LO https://github.com/giou/stream-archive/releases/latest/download/docker-compose.yml
docker compose pull
docker compose run --rm stream-archive stream-archive-setup
docker compose up -d
docker compose logs -f   # follow startup
```

The setup wizard writes `config.json` in the data directory. It asks
for the Twitch credentials and one control surface (web panel, Telegram
bot, or both). Channels come later, from the panel or the bot. It also
offers the optional features: YouTube restream, Enable MTProto (upload
to Telegram), panel access, and Enable Kick. Run the wizard again later
to add a feature. See `docs/configuration.md` for every key.

The data directory is the folder with `docker-compose.yml`
(`~/stream-archive-data/` in this example). It holds the settings, the
recordings, the chat files, and the tokens. To move it to another disk, put
`STREAM_ARCHIVE_DATA` in `.env` in that folder:

```sh
# ~/stream-archive-data/.env
STREAM_ARCHIVE_DATA=/mnt/bigdisk/stream-archive-data
```

To update the app, pull the new image:

```sh
docker compose pull && docker compose up -d
```

The wizard covers the optional features. For the full procedure behind each one:

- [YouTube setup](docs/youtube-setup.md) for `output_mode: youtube` or `both`.
- [Kick webhook](docs/kick-webhook.md) for instant signals and Kick chat.

## Documentation

| Guide | Content |
| --- | --- |
| [Configuration](docs/configuration.md) | All keys of `config.json`, and secrets from the environment |
| [Running](docs/running.md) | Start, data directory, container identity, logs, shutdown |
| [YouTube setup](docs/youtube-setup.md) | OAuth client and the one-time authorization flow |
| [Kick webhook](docs/kick-webhook.md) | Public URLs, proxy setup, delivery test, receiver internals |
| [Telegram control](docs/telegram-control.md) | Bot menu and command list |
| [Control API](docs/control-api.md) | HTTP endpoints, accepted values, error answers |
| [Chat recording](docs/chat-recording.md) | Chat files and TwitchDownloader commands |
| [Failure handling](docs/failure-handling.md) | Behavior for each failure type |
| [Development](docs/development.md) | Project layout, commands, dependency updates, plugin override |

## License

[MIT](LICENSE). The image contains the third-party `twitch.py` plugin
(streamlink-ttvlol). The recorder imports that file into its own process, so
the build downloads it from the upstream release, records its sha256 in
`/app/plugins/twitch.py.sha256`, and can be pinned to reviewed bytes with the
`TTVLOL_PLUGIN_SHA256` build argument. The plugin keeps its upstream license.
