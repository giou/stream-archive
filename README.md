# Twitch Monitor & Recorder

## Overview

Polls the Twitch Helix API for live channels and records them via streamlink using ad-block playlist proxies (vendored `streamlink-ttvlol` plugin). Can optionally re-stream recordings to YouTube Live and sends Telegram alerts on live/offline events and start failures.

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` (stream recording and YouTube re-stream)
- `chromium` (needed by the plugin's client-integrity token acquisition)
- Twitch app credentials — register at https://dev.twitch.tv/console
- Telegram bot token — create one with [BotFather](https://t.me/BotFather)

## Setup

```sh
uv sync
cp config.json.example config.json
# fill in every key in config.json (see table below)
```

Run `python setup_youtube.py` **only** if `output_mode` is `youtube` or `both`. It requires `client_secret.json` (Google Cloud OAuth client) and an OAuth consent screen; it saves the resulting token to `youtube_token.json`.

## Configuration reference

| Key | Description |
| --- | --- |
| `channels` | List of channel names to monitor |
| `monitoring_interval` | Poll interval in seconds; must be greater than 0 (default 60) |
| `timezone` | IANA timezone used for recording filenames and timestamps |
| `proxy_list` | Ad-block playlist proxies; `httpproxy://user:pass@host:port` entries or https URLs with `[channel]` placeholder support |
| `output_mode` | `disk` (default), `youtube`, or `both` |
| `retention_days` | Delete recordings older than this many days; `0` disables cleanup (default 0) |
| `recording_dir` | Directory where `.ts` recordings are stored |
| `plugin_dir` | Directory containing the vendored streamlink plugin |
| `youtube.privacy_status` | `public`, `unlisted` (default), or `private` |
| `youtube.client_secrets_file` | Path to the Google Cloud OAuth client secrets JSON |

## Running

Foreground:

```sh
uv run python main.py
```

As a systemd user unit:

```sh
mkdir -p ~/.config/systemd/user
cp twitch-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now twitch-monitor
```

Logs:

```sh
journalctl --user -u twitch-monitor -f
```

## Behavior notes

- **Proxy failure**: if all ad-block proxies fail for a live channel, the channel is skipped, one Telegram alert is sent (rate-limited to once per 30 minutes per channel), and recording is retried automatically on the next poll cycle.
- **YouTube rate limit**: if YouTube Live refuses a broadcast, the recorder automatically falls back to disk recording (existing behavior).
- **Retention cleanup**: expired recordings are deleted at startup and then once per day when `retention_days > 0`.

## Plugin maintenance

`plugins/twitch.py` is vendored from [streamlink-ttvlol](https://github.com/2bc4/streamlink-ttvlol); the banner logs `STREAMLINK_TTVLOL_VERSION`. To refresh, replace the file from upstream and bump the version constant. Upstream issues go to https://github.com/2bc4/streamlink-ttvlol/issues.

## Development

```sh
uv run pytest
```
