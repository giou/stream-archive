# StreamArchive

StreamArchive monitors Twitch and Kick channels and records live streams with streamlink. It restreams to YouTube Live when you enable that. It sends alerts and takes admin commands through the Telegram bot or the web panel.

Live signals arrive in seconds. Twitch uses an EventSub conduit. Kick uses signed webhooks. A poll loop stays as fallback. The fallback catches missed events and restarts recordings that died mid-stream.

## Setup

Use the setup wizard. It is the recommended path.

```sh
mkdir ~/stream-archive-data && cd ~/stream-archive-data
curl -LO https://github.com/giou/stream-archive/releases/latest/download/docker-compose.yml
docker compose pull
docker compose run --rm stream-archive stream-archive-setup
docker compose up -d
docker compose logs -f
```

The setup wizard writes `config.json` in the data directory. It asks for Twitch credentials and one control surface (web panel, Telegram bot, or both). Then it offers channels and each optional block: web panel access, Kick, YouTube restream, Telegram upload, and the control API. Run the setup wizard again later to add a block.

The data directory holds the settings, the recordings, the chat files, and the tokens. To move it to another disk, set `STREAM_ARCHIVE_DATA` in `.env` in that folder. See [Backup and restore](docs/backup-restore.md) before you copy or move it:

```sh
# ~/stream-archive-data/.env
STREAM_ARCHIVE_DATA=/mnt/bigdisk/stream-archive-data
```

To update the app, pull the new image:

```sh
docker compose pull && docker compose up -d
```

Two blocks need extra steps:

- [YouTube restream](docs/youtube-setup.md) for `output_mode: youtube` or `both`.
- [Kick webhook](docs/kick-webhook.md) for instant Kick signals and Kick chat.

## Features

- **Channels.** One settings file covers both platforms. A name is `twitch:<name>` or `kick:<slug>`.
- **Output modes.** `disk` writes `.ts` files (`.m4a` for audio-only). `youtube` restreams to a YouTube broadcast. `both` runs disk and `youtube` together.
- **Chat recording.** Twitch IRC chat and Kick webhook chat land in TwitchDownloader-compatible JSON. See [Chat recording](docs/chat-recording.md).
- **Retention and disk cap.** Old files expire after `retention_days`. An optional `disk.max_total_gb` cap deletes the oldest files or stops new recordings.
- **Self-healing.** Dead recordings restart on the next poll cycle. YouTube restreams restart with growing delays inside a daily broadcast budget. Quota errors fall back to disk.
- **Telegram alerts.** Live and offline alerts carry the stream facts and the file facts. Failure alerts arrive at most once per 30 minutes for each channel.
- **Telegram bot.** The admin manages channels, modes, quality, chat, limits, remote access, and uploads over the Telegram bot. See [Telegram control](docs/telegram-control.md).
- **Web panel.** The browser replaces the Telegram bot: status, channels, settings, recordings with a player, reload, restart, and update. See [Web panel](docs/web-control.md).
- **Control API.** Scripts change the same settings over HTTP with a key. See [Control API](docs/control-api.md).
- **Telegram upload.** Large recordings go to your chat through MTProto (up to 2 GB). The Bot API allows 50 MB only.

## Guides

| Guide | Content |
| --- | --- |
| [Settings](docs/configuration.md) | All keys of `config.json`, and secrets from the environment |
| [Running](docs/running.md) | Data directory, container identity, logs, shutdown |
| [Backup and restore](docs/backup-restore.md) | Data directory contents, backup, restore rules |
| [YouTube restream](docs/youtube-setup.md) | OAuth client and the one-time authorization flow |
| [Kick webhook](docs/kick-webhook.md) | Public URLs, proxy setup, delivery test |
| [Telegram control](docs/telegram-control.md) | Telegram bot menu and command list |
| [Control API](docs/control-api.md) | HTTP endpoints, accepted values, error answers |
| [Chat recording](docs/chat-recording.md) | Chat files and TwitchDownloader commands |
| [Failure handling](docs/failure-handling.md) | Behavior for each failure type |
| [Web panel](docs/web-control.md) | Browser control without Telegram |
| [Development](docs/development.md) | Project layout, commands, dependency updates |

## License

[MIT](LICENSE). The image contains the third-party `twitch.py` plugin (streamlink-ttvlol). The build downloads it from the upstream release and records its sha256 in `/app/plugins/twitch.py.sha256`. Pin reviewed bytes with the `TTVLOL_PLUGIN_SHA256` build argument. The plugin keeps its upstream license.
