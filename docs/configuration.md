# Configuration

The app reads its settings from `config.json`. The file `config.json.example`
holds all keys and is the template for a new file.

## Secrets from the environment

You can write a secret as `${ENV_VAR}`, for example:

```json
"bot_telegram_api": "${TELEGRAM_BOT_TOKEN}"
```

The app reads the value from the environment. The placeholder text stays in
`config.json`, so the app never writes the resolved secret back. A settings
file with placeholders is safe to commit or to share.

## Keys

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `telegram_user_id` | yes | — | Numeric Telegram user or chat id for alerts and bot control. `0` disables the bot (web panel control) |
| `bot_telegram_api` | yes | — | Telegram bot token from BotFather. `""` disables the bot (web panel control) |
| `twitch_client_id` | yes | — | Twitch app client id |
| `twitch_client_secret` | yes | — | Twitch app client secret |
| `channels` | yes | — | Non-empty list of channels: `twitch:<name>` or `kick:<slug>`. Bare names become `twitch:` on load |
| `proxy_list` | yes | — | Non-empty list of ad-block playlist proxies for Twitch recordings. An `httpproxy://…` entry is a ttvlol v2 proxy (user and password are optional: `httpproxy://user:pass@host:port`). An `https://…` entry is a v1 proxy |
| `monitoring_interval` | yes | — | Poll interval in seconds, more than 0 |
| `timezone` | yes | — | IANA timezone (for example `America/New_York`) for filenames and timestamps |
| `plugin_dir` | yes | — | Directory with the streamlink-ttvlol plugin. `/app/plugins` in Docker (baked into the image, read-only). Relative `plugins` for a dev run. See [Plugin override](development.md#plugin-override) |
| `recording_dir` | yes | — | Directory for `.ts`/`.m4a` recordings |
| `record_chat` | no | `true` | Record Twitch IRC chat alongside the video. The app writes the chat file while the recording runs. Kick chat has its own key |
| `chat_dir` | no | `chat` | Directory for chat JSON files (`chat_dir/<platform>/<channel>/<title>-<ts>.chat.json`). See [Chat recording](chat-recording.md) |
| `output_mode` | no | `disk` | `disk`, `youtube`, or `both` |
| `channel_output_modes` | no | `{}` | Per-channel override, for example `{"channel": "disk" \| "youtube" \| "both"}`. Channels without an entry use `output_mode` |
| `eventsub.enabled` | no | `true` | Twitch EventSub fast path through a conduit. It uses the existing app credentials and needs no extra setup. `false` = Twitch polling only |
| `kick.client_id` | yes¹ | — | Kick app client id. Required when a `kick:` channel is configured |
| `kick.client_secret` | yes¹ | — | Kick app client secret. Same requirement |
| `kick.record_chat` | no | `true` | Record Kick chat (the webhook delivers it). Requires `kick.webhook.enabled` |
| `kick.webhook.enabled` | no | `false` | Receive Kick webhooks (live, offline, and chat) and keep the subscriptions in sync. `false` = Kick polling only (no chat). While it is `false`, the receiver ignores deliveries and the app deletes the subscriptions of the monitored channels |
| `kick.webhook.setup_notified` | no | `false` | Internal: tracks the "webhook is working" confirmation for the current enable |
| `kick.webhook.public_url` | no | `""` | Separate public entry for Kick deliveries. Empty follows `endpoint.public_url`. Set it when the endpoint serves a tailnet that Kick cannot reach. The app manages no tunnel for it |
| `kick.webhook.listen_host` | no | `127.0.0.1` | Bind address of the webhook-only listener. Set `0.0.0.0` under Docker |
| `kick.webhook.listen_port` | no | `8788` | Port of the webhook-only listener. The public proxy targets this port. It serves `POST /kick/webhook` and nothing else |
| `endpoint.enabled` | no | `false` | Serve the private listener: the web panel and the control API. The Kick webhook has its own listener below |
| `endpoint.listen_host` | no | `127.0.0.1` | Bind address of the private listener. Set `0.0.0.0` under Docker, so the host proxy reaches it |
| `endpoint.listen_port` | no | `8787` | Port of the private listener. Tailnet serve and the private side of the proxy target it |
| `endpoint.public_url` | yes² | `""` | Public base URL of the endpoint, for example `https://streamarchive.example.com`. The panel lives at the root. Required when `endpoint.enabled` is true |
| `api.enabled` | no | `false` | Serve the HTTP control API under `/api/v1` on the private listener. The bot sets this key |
| `api.key` | no | `""` | API key (bearer token). The bot generates it on the first enable. Keep it secret |
| `web.enabled` | no | `false` | Serve the browser control panel at the domain root on the private listener. See [Web control panel](web-control.md) |
| `web.password_hash` | no | `""` | PBKDF2 hash of the panel password. Set it with `stream-archive-setup` or `stream-archive-setup-web`. Empty locks the panel |
| `web.session_secret` | no | `""` | HMAC secret of the panel sessions. The first boot with the panel on generates and stores one, so logins survive restarts |
| `mtproto.enabled` | no | `false` | Send recordings over MTProto (up to 2 GB). The Bot API allows 50 MB only. The bot sets this key |
| `mtproto.api_id` | yes³ | `0` | Telegram app api id from my.telegram.org. Use `${TELEGRAM_API_ID}`. Required when `mtproto.enabled` is true |
| `mtproto.api_hash` | yes³ | `""` | Telegram app api hash from my.telegram.org. Use `${TELEGRAM_API_HASH}`. Required when `mtproto.enabled` is true |
| `mtproto.session` | no | `mtproto.session` | Session file of the MTProto client, relative to the data dir. Keep it private (0600) |
| `retention_days` | no | `0` | Delete recordings older than this many days. `0` disables cleanup |
| `preferred_quality` | no | `best` | Stream quality that the app requests from streamlink (`best`, `1080p`, `720p`, …, `audio_only`) |
| `channel_preferred_qualities` | no | `{}` | Per-channel quality override, for example `{"channel": "720p"}`. Channels without an entry use `preferred_quality` |
| `max_concurrent_recordings` | no | `0` | Maximum simultaneous recordings. `0` = unlimited |
| `max_concurrent_youtube_streams` | no | `0` | Maximum simultaneous YouTube re-streams. `0` = unlimited |
| `disk.max_total_gb` | no | `0` | Delete the oldest archive files (recordings and chat) when the total exceeds this size in GB. `0` disables the cap |
| `disk.check_interval_s` | no | `60` | Seconds between disk watchdog checks |
| `disk.delete_oldest` | no | `true` | On a breach, delete the oldest archive files. `false` stops new recordings instead |
| `update_check.enabled` | no | `true` | Periodic check for a newer app release, with a Telegram notification when one is available |
| `update_check.interval_hours` | no | `24` | Hours between update checks |
| `youtube.client_secrets_file` | no | `client_secret.json` | Path to the Google OAuth client file. Only the YouTube authorization flow reads this file |
| `youtube.privacy_status` | no | `unlisted` | Privacy of created YouTube broadcasts: `public`, `unlisted`, or `private` |
| `youtube.hold_seconds` | no | `0` | Keep the broadcast open this many seconds after the source stops. A return within the delay reuses the same broadcast (no quota cost). A bundled "Reconnecting..." clip feeds the broadcast during the wait. `0` ends the broadcast immediately |
| `channel_youtube_hold_seconds` | no | `{}` | Per-channel override of `youtube.hold_seconds`, for example `{"channel": 60}`. A channel set to `0` is off. An absent entry uses the global value. Managed from the Telegram channel submenu |

¹ Required when the channel list contains a `kick:` entry.
² Required when the endpoint is enabled.
³ Required when MTProto upload is enabled.
Streamlink and the `twitch.py` plugin are not part of the update check. The
image build fetches the newest plugin release, and Dependabot opens the
streamlink updates as pull requests. Both ship in a new image.

Older files keep the listener and the public URL under
`kick.webhook`, for example `kick.webhook.listen_host`. The app moves these
keys to `endpoint` when it loads the file and keeps both features on. The
old tunnel keys (`tunnel`, `cloudflare_token`, `cloudflare_managed`) are
dropped: tunnels are user-managed now. A file
that already has an `endpoint` section is not changed. A working setup keeps
working, and the first save writes the new layout.

`output_mode: youtube` also needs the file `youtube_token.json`. See
[YouTube setup](youtube-setup.md).

## Related guides

- [Kick webhook](kick-webhook.md) sets the public URLs for deliveries.
- [Control API](control-api.md) changes channels and settings over HTTP.
- [Web control panel](web-control.md) replaces the bot in the browser.
- [Plugin override](development.md#plugin-override) replaces the plugin in the
  image.
