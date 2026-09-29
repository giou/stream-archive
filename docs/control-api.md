# Control API

The control API manages channels and settings over HTTP. It runs on the private listener under `/api/v1`. Use it to make the same changes without the Telegram app.

The control API is off by default.

## Enable the control API

The setup wizard offers the control API as one of its steps (**Control API (remote HTTP)**). It is the recommended path. The wizard generates the key on the first enable and shows it once. To enable it by hand, do the steps that follow.

1. Open `/settings` in the Telegram bot.
2. Choose **Settings → Remote access → API**.
3. Tap **Enable API**.

The Telegram bot generates the key and shows it with the base URL. Tap **Show key** to show the key again, and **Rotate key** to replace it. The web panel has the same pair under an **API key** card. A rotated key stops the old key at once. Disabling the control API keeps the key, so a later enable uses the same key.

The Telegram bot shows the key in the private chat with the admin only. In a group chat the Telegram bot answers with a pointer to that chat, because every member of the group can read the reply.

## Base URL

| Item | Value |
| --- | --- |
| Base URL | `<endpoint.public_url>/api/v1/` |
| Kick webhook URL | `<kick.webhook.public_url or endpoint.public_url>/kick/webhook` |

The endpoint is the private listener (web panel plus control API). A tailnet serve or your own reverse proxy publishes it. Open **Settings → Remote access** in the Telegram bot to see its state and to turn it on. While the endpoint is off, the control API answers on the local address only.

## Authentication

Send the key in one of two headers:

```sh
curl -H "Authorization: Bearer $API_KEY" https://streamarchive.example.com/api/v1/status
curl -H "X-API-Key: $API_KEY" https://streamarchive.example.com/api/v1/status
```

A live web panel session works instead of the key. The browser sends its cookie. State-changing calls still need the `X-CSRF-Token` header. The web panel and scripts share the one control API at `/api/v1/`.

| Answer | Meaning |
| --- | --- |
| `401` | The key is missing or wrong. |
| `403` | A session call without (or with a wrong) CSRF token. |
| `404` | The control API is off, or no key exists yet. The routes behave as if they do not exist. |
| `429` | Too many failed key attempts from your address. Wait and try again. |

Keep the key secret. Anyone who has it can change your channels and settings.

## Endpoints

| Method | Path | Action |
| --- | --- | --- |
| `GET` | `/api/v1/status` | Version, channel count, channels that record |
| `GET` | `/api/v1/settings` | Global settings |
| `PATCH` | `/api/v1/settings` | Change global settings |
| `GET` | `/api/v1/channels` | All channels with their effective settings |
| `POST` | `/api/v1/channels` | Add a channel |
| `GET` | `/api/v1/channels/<channel>` | One channel |
| `PATCH` | `/api/v1/channels/<channel>` | Change per-channel settings |
| `DELETE` | `/api/v1/channels/<channel>` | Remove a channel |
| `POST` | `/api/v1/kick/webhook/test` | Run the Kick delivery test, report its timed result |
| `GET` | `/api/v1/api-key` | Show the key. Reads `404` with no key saved yet |
| `POST` | `/api/v1/api-key/rotate` | Replace the key, return the new one. The old key dies at once |

A channel name carries its platform prefix, for example `twitch:example` or `kick:example`. If your client encodes it, use `%3A` for the colon (`/api/v1/channels/kick%3Aexample`). Both forms work.

`POST` and `PATCH` take a JSON object body. The control API rejects a body larger than 64 KiB with `413`, and a body that is not a JSON object with `400`.

## Status

```sh
curl -H "Authorization: Bearer $API_KEY" https://streamarchive.example.com/api/v1/status
```

```json
{
  "version": "1.5.0",
  "channels": 6,
  "recording": [],
  "monitoring_interval_s": 60.0
}
```

`recording` lists the channels that capture a stream at this moment. `monitoring_interval_s` is the poll interval.

## Global settings

### Read

```sh
curl -H "Authorization: Bearer $API_KEY" https://streamarchive.example.com/api/v1/settings
```

```json
{
  "output_mode": "youtube",
  "preferred_quality": "best",
  "retention_days": 7.0,
  "max_concurrent_recordings": 0.0,
  "max_concurrent_youtube_streams": 0.0,
  "record_chat": true,
  "kick_record_chat": true,
  "youtube": {"privacy_status": "private", "hold_seconds": 0.0},
  "disk": {"max_total_gb": 0.0, "delete_oldest": false},
  "endpoint": {
    "enabled": true,
    "public_url": "https://streamarchive.example.com"
  },
  "kick_webhook": {"enabled": true, "public_url": "https://kick.example.com"},
  "api": {"enabled": true, "base_url": "https://streamarchive.example.com/api/v1/"},
  "monitoring_interval_s": 60.0
}
```

The answer never holds a secret. The key and the Telegram bot token stay in `config.json`.

### Write

`PATCH /api/v1/settings` accepts these keys:

| Key | Value | Effect |
| --- | --- | --- |
| `output_mode` | `disk`, `youtube`, `both` | Output of every channel without an override |
| `preferred_quality` | `best`, `1080p`, `720p`, `480p`, `360p`, `audio_only` | Quality for every channel without an override |
| `retention_days` | whole number ≥ 0 | Delete recordings older than this many days. `0` disables cleanup |
| `max_concurrent_recordings` | whole number ≥ 0 | Recording limit. `0` means unlimited |
| `max_concurrent_youtube_streams` | whole number ≥ 0 | YouTube restream limit. `0` means unlimited |
| `record_chat` | `true`, `false` | Record Twitch chat |
| `kick_record_chat` | `true`, `false` | Record Kick chat |
| `disk_max_total_gb` | number ≥ 0 | Archive size cap. `0` disables the cap |
| `disk_delete_oldest` | `true`, `false` | On a full disk, delete the oldest recordings (`true`) or stop new recordings (`false`) |
| `youtube_hold_seconds` | whole number ≥ 0 | Global YouTube hold delay in seconds. `0` ends the restream at once |
| `endpoint_enabled` | `true`, `false` | Publish the panel beyond this machine. Enabling needs a saved URL |
| `endpoint_public_url` | URL or bare hostname | Public address of your own proxy. Saving alone never flips the toggle. Empty clears a saved URL while the endpoint stays off. Ports must be usable (1-65535) |
| `kick_webhook_enabled` | `true`, `false` | Instant Kick signals and chat |
| `kick_webhook_public_url` | URL, bare hostname, or `""` | Kick entry. Empty follows the panel URL |
| `api_enabled` | `true`, `false` | Remote HTTP control. A first enable makes a key and shows it once |

```sh
curl -X PATCH https://streamarchive.example.com/api/v1/settings \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"output_mode": "both", "retention_days": 14}'
```

```json
{
  "applied": {
    "output_mode": "Output mode set to both",
    "retention_days": "Retention set to 14 day(s)"
  },
  "errors": {},
  "settings": {"output_mode": "both", "retention_days": 14.0}
}
```

One request can hold one key or more. The control API applies each key on its own, so one bad key does not block the others. `applied` names each key that worked, and `errors` names each key that failed.

## Channels

### List

```sh
curl -H "Authorization: Bearer $API_KEY" https://streamarchive.example.com/api/v1/channels
```

```json
{
  "channels": [
    {
      "channel": "twitch:example",
      "recording": false,
      "output_mode": "youtube",
      "output_mode_override": null,
      "quality": "best",
      "quality_override": null,
      "youtube_hold_seconds": 0.0,
      "youtube_hold_seconds_override": null
    }
  ]
}
```

The fields without `_override` are the values that the monitor uses. A value with `_override` applies to that channel alone. `null` means "use the global setting".

### Add

```sh
curl -X POST https://streamarchive.example.com/api/v1/channels \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"channel": "twitch:example"}'
```

The control API takes a channel name with a platform prefix or a profile URL (`https://twitch.tv/example`, `https://kick.com/example`). The answer holds the message, the normalized channel name, and the new channel list. Subscriptions follow at once: the app creates the EventSub subscription for Twitch and the webhook subscription for Kick.

### Change one channel

`PATCH /api/v1/channels/<channel>` accepts these keys:

| Key | Value | Effect |
| --- | --- | --- |
| `output_mode` | `disk`, `youtube`, `both`, `default` | Output override for this channel. `default` clears the override |
| `quality` | `best`, `1080p`, `720p`, `480p`, `360p`, `audio_only`, `default` | Quality override. `default` clears the override |
| `youtube_hold_seconds` | whole number ≥ 0, or `"default"` | Keep the YouTube broadcast open this long after the source stops. `"default"` clears the override |

```sh
curl -X PATCH https://streamarchive.example.com/api/v1/channels/twitch:example \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"quality": "720p", "youtube_hold_seconds": 60}'
```

```json
{
  "channel": "twitch:example",
  "applied": {
    "quality": "Quality for twitch:example set to 720p",
    "youtube_hold_seconds": "Hold delay for twitch:example set to 60s (0 = end immediately)"
  },
  "errors": {}
}
```

### Remove

```sh
curl -X DELETE https://streamarchive.example.com/api/v1/channels/twitch:example \
  -H "Authorization: Bearer $API_KEY"
```

The control API stops a live recording of that channel, clears its overrides, and deletes its webhook subscription. The last channel cannot be removed: the settings need at least one channel.

## Error answers

A request that fails as a whole answers with `error`:

| Status | Body | Cause |
| --- | --- | --- |
| `400` | `{"error": "…"}` | Bad JSON, unknown key, or an invalid value |
| `401` | `{"error": "unauthorized"}` | Missing or wrong key |
| `404` | `{"error": "…"}` | The control API is off, or the channel is not monitored |
| `409` | `{"applied": {}, "errors": {"quality": "…"}}` | A quality change to `audio_only` for a channel that restreams to YouTube |
| `413` | `{"error": "request body too large"}` | Body above 64 KiB |

The `409` case needs a confirm press in the Telegram bot, because audio-only cannot go to YouTube. The Telegram bot switches the output of that channel to `disk` only after you confirm. The control API changes nothing.

## Behavior

- Every applied change sends the admin a Telegram message with the origin. The origin is `Control API`, or `Web panel` for a web panel session call.
- Every change goes through the same validation and the same atomic `config.json` write as a Telegram bot change. A rejected change changes nothing.
- A change applies on the next poll cycle. A recording in progress keeps the settings of its start.
- The control API serves the same settings as the Telegram bot. Most keys stay read-only over the control API. The list that follows names them.

### Read-only keys

Edit these keys by hand in `config.json` or with the setup wizard:

| Key group | Content |
| --- | --- |
| `endpoint.listen_host`, `endpoint.listen_port` | Listener bind and port (setup wizard only) |
| `kick.client_id`, `kick.client_secret` | App credentials (setup wizard only, restart applies them) |
| `kick.webhook.listen_host`, `kick.webhook.listen_port` | Webhook bind and port (setup wizard only) |
| `web.*` | Panel password hash, session secret, on/off state |
| `api.key` | Key itself: the bot shows or rotates it, the wizard shows it once |
| `youtube.privacy_status` | Global privacy (setup wizard only, restart applies it) |
| `timezone`, `monitoring_interval` | Timezone and poll interval |
| `proxy_list`, `plugin_dir`, `recording_dir`, `chat_dir` | Playlist proxies and directories |
| `mtproto.*` | Uploader credentials and session (file or environment only) |
| `update_check.*` | Release check state and interval |
| `eventsub.enabled` | Twitch conduit use |
| All secrets | Telegram bot token, Twitch credentials, Kick credentials, MTProto api id and hash |

## Full example

```sh
export API_KEY=...
export BASE=https://streamarchive.example.com/api/v1

# 1. Read the service status.
curl -s -H "Authorization: Bearer $API_KEY" "$BASE/status" | jq

# 2. Add a channel.
curl -s -X POST "$BASE/channels" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"channel": "twitch:example"}' | jq

# 3. Record that channel to disk only, in 720p.
curl -s -X PATCH "$BASE/channels/twitch:example" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"output_mode": "disk", "quality": "720p"}' | jq

# 4. Keep 30 days of recordings.
curl -s -X PATCH "$BASE/settings" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"retention_days": 30}' | jq

# 5. Stop monitoring the channel.
curl -s -X DELETE "$BASE/channels/twitch:example" \
  -H "Authorization: Bearer $API_KEY" | jq
```
