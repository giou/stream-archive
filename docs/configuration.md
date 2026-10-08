# Settings

The app reads its settings from `config.json`. The file `config.json.example` holds all keys and is the template for a new file. The setup wizard writes the file for you. It is the recommended path.

## Secrets from the environment

You can write a secret as `${ENV_VAR}`, for example:

```json
"bot_telegram_api": "${TELEGRAM_BOT_TOKEN}"
```

The app reads the value from the environment. The placeholder text stays in `config.json`, so the app never writes the resolved secret back. A settings file with placeholders is safe to commit or to share.

## Keys

Keys marked **hand edit** stay read-only over the Telegram bot, the web panel, and the control API. Change them by hand in `config.json` or with the setup wizard. See [Control API](control-api.md) for the full read-only list.

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `telegram_user_id` | yes | — | Numeric Telegram user or chat id for alerts and Telegram bot control. `0` disables the Telegram bot |
| `bot_telegram_api` | yes | — | Telegram bot token from BotFather. `""` disables the Telegram bot |
| `twitch_client_id` | yes | — | Twitch app client id |
| `twitch_client_secret` | yes | — | Twitch app client secret |
| `channels` | yes | — | Non-empty list of channels: `twitch:<name>` or `kick:<slug>`. Bare names become `twitch:` on load. The same rule applies to the keys of the per-channel maps below: a bare key such as `{"mykick": ...}` silently becomes `twitch:mykick`. Always write the prefix |
| `proxy_list` | yes, hand edit | — | Non-empty list of ad-block playlist proxies for Twitch recordings |
| `monitoring_interval` | yes, hand edit | — | Poll interval in seconds, more than 0 |
| `timezone` | yes, hand edit | — | IANA timezone (for example `America/New_York`) for filenames and timestamps |
| `plugin_dir` | yes, hand edit | — | Directory with the streamlink-ttvlol plugin. `/app/plugins` in Docker. Relative `plugins` for a dev run. See [Plugin override](development.md#plugin-override) |
| `recording_dir` | yes | — | Directory for `.ts`/`.m4a` recordings |
| `record_chat` | no | `true` | Record Twitch IRC chat alongside the video |
| `chat_dir` | no | `chat` | Directory for chat JSON files (`chat_dir/<platform>/<channel>/<title>-<ts>.chat.json`). See [Chat recording](chat-recording.md) |
| `output_mode` | no | `disk` | `disk`, `youtube`, or `both` |
| `channel_output_modes` | no | `{}` | Per-channel override, for example `{"twitch:channel": "disk"}`. Channels without an entry use `output_mode`. A bare key means `twitch:` |
| `eventsub.enabled` | no | `true` | Twitch EventSub fast path through a conduit. It uses the app credentials and needs no extra setup. `false` gives Twitch polling only |
| `kick.client_id` | yes¹, hand edit | — | Kick app client id. Required with a `kick:` channel |
| `kick.client_secret` | yes¹, hand edit | — | Kick app client secret. Same requirement |
| `kick.record_chat` | no | `true` | Record Kick chat. Requires `kick.webhook.enabled` |
| `kick.webhook.enabled` | no, hand edit | `false` | Receive Kick webhooks (live, offline, and chat). `false` gives Kick polling only, without chat |
| `kick.webhook.setup_notified` | no, hand edit | `false` | Internal: tracks the "webhook is working" confirmation for the current enable |
| `kick.webhook.public_url` | no, hand edit | `""` | Separate public entry for Kick deliveries. Empty follows `endpoint.public_url` |
| `kick.webhook.listen_host` | no, hand edit | `127.0.0.1` | Bind address of the webhook-only listener. Set `0.0.0.0` under Docker |
| `kick.webhook.listen_port` | no, hand edit | `8788` | Port of the webhook-only listener. It serves `POST /kick/webhook` and nothing else |
| `endpoint.enabled` | no, hand edit | `false` | Serve the private listener: the web panel and the control API |
| `endpoint.listen_host` | no, hand edit | `127.0.0.1` | Bind address of the private listener. Set `0.0.0.0` under Docker |
| `endpoint.listen_port` | no, hand edit | `8787` | Port of the private listener |
| `endpoint.public_url` | yes², hand edit | `""` | Public base URL of the endpoint, for example `https://streamarchive.example.com`. Required when `endpoint.enabled` is true |
| `api.enabled` | no, hand edit | `false` | Serve the control API under `/api/v1` on the private listener |
| `api.key` | no, hand edit | `""` | Control API key (bearer token). The setup wizard generates it on the first enable. Keep it secret |
| `web.enabled` | no, hand edit | `false` | Serve the web panel at the domain root on the private listener. See [Web panel](web-control.md) |
| `web.password_hash` | no, hand edit | `""` | PBKDF2 hash of the web panel password. Set it with the setup wizard or `stream-archive-setup-web`. Empty locks the web panel |
| `web.session_secret` | no, hand edit | `""` | HMAC secret of the web panel sessions. The first boot with the web panel on generates and stores one |
| `mtproto.enabled` | no | `false` | Send recordings over MTProto (up to 2 GB). The Bot API allows 50 MB only |
| `mtproto.api_id` | yes³ | `0` | Telegram app api id from my.telegram.org. Use `${TELEGRAM_API_ID}`. Required when `mtproto.enabled` is true |
| `mtproto.api_hash` | yes³ | `""` | Telegram app api hash from my.telegram.org. Use `${TELEGRAM_API_HASH}`. Same requirement |
| `mtproto.session` | no, hand edit | `mtproto.session` | Session file of the MTProto client, relative to the data dir. Keep it private (0600) |
| `retention_days` | no | `0` | Delete recordings older than this many days. `0` disables cleanup |
| `preferred_quality` | no | `best` | Stream quality that the app requests from streamlink (`best`, `1080p`, `720p`, …, `audio_only`) |
| `channel_preferred_qualities` | no | `{}` | Per-channel quality override, for example `{"twitch:channel": "720p"}`. Channels without an entry use `preferred_quality`. A bare key means `twitch:` |
| `channel_categories` | no | `{}` | Per-channel category filter, for example `{"twitch:channel": ["Just Chatting", "Music"]}`. Channels without an entry record every live stream. A bare key means `twitch:`. The match ignores case. A blank live category never matches a filter. Hand edits skip the platform check below: the Telegram bot and the control API check each name and refuse unknown names |
| `max_concurrent_recordings` | no | `0` | Maximum simultaneous recordings. `0` means unlimited |
| `max_concurrent_youtube_streams` | no | `0` | Maximum simultaneous YouTube restreams. `0` means unlimited |
| `disk.max_total_gb` | no | `0` | Delete the oldest archive files when the total exceeds this size in GB. `0` disables the cap |
| `disk.check_interval_s` | no, hand edit | `60` | Seconds between disk watchdog checks |
| `disk.delete_oldest` | no | `true` | On a breach, delete the oldest archive files. `false` stops new recordings instead |
| `update_check.enabled` | no, hand edit | `true` | Periodic check for a newer app release, with a Telegram notification when one is available |
| `update_check.interval_hours` | no, hand edit | `24` | Hours between update checks |
| `youtube.client_secrets_file` | no, hand edit | `client_secret.json` | Path to the Google OAuth client file |
| `youtube.privacy_status` | no, hand edit | `unlisted` | Privacy of created YouTube broadcasts: `public`, `unlisted`, or `private` |
| `youtube.hold_seconds` | no, hand edit | `0` | Keep the broadcast open this many seconds after the source stops. A return within the delay reuses the same broadcast. `0` ends the broadcast at once |
| `channel_youtube_hold_seconds` | no | `{}` | Per-channel override of `youtube.hold_seconds`, for example `{"twitch:channel": 60}`. An absent entry uses the global value. A bare key means `twitch:` |

¹ Required when the channel list holds a `kick:` entry.
² Required when the endpoint is enabled.
³ Required when MTProto upload is enabled.

`output_mode: youtube` also needs the file `youtube_token.json`. See [YouTube restream](youtube-setup.md).

## Related guides

- [Kick webhook](kick-webhook.md) sets the public URLs for deliveries.
- [Control API](control-api.md) changes channels and settings over HTTP.
- [Web panel](web-control.md) replaces the Telegram bot in the browser.
