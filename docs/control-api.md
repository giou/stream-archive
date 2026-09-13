# Control API

The control API manages channels and settings over HTTP. The bot serves it
on the endpoint under `/api/v1`. Use it to make the same changes without
the Telegram app.

The API is off by default.

## Enable the API

1. Open `/settings` in Telegram.
2. Choose **Remote Access → API**.
3. Tap **On**.

The bot generates the API key and shows it with the base URL. Tap
**Show key** to show the key again, and **Rotate key** to replace it. A
rotated key stops the old key at once. Disabling the API keeps the key, so
a later enable uses the same key.

## Base URL

| Item | Value |
| --- | --- |
| Base URL | `<endpoint.public_url>/api/v1/` |
| Kick webhook URL | `<endpoint.public_url>/kick/webhook` |

The endpoint is the listener plus its tunnel. Open **Remote Access** in
Telegram to see its state and to turn it on. While the endpoint is off, the
API answers on the local address only.

## Authentication

Send the key in one of two headers:

```sh
curl -H "Authorization: Bearer $API_KEY" https://streamarchive.example.com/api/v1/status
curl -H "X-API-Key: $API_KEY" https://streamarchive.example.com/api/v1/status
```

| Answer | Meaning |
| --- | --- |
| `401` | The key is missing or wrong. |
| `404` | The API is off, or no key was generated yet. The routes behave as if they do not exist. |

Keep the key secret. Anyone who has it can change your channels and
settings.

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

A channel name carries its platform prefix, for example `twitch:example` or
`kick:example`. If your client requires it, percent-encode the colon as
`%3A` in a URL (`/api/v1/channels/kick%3Aexample`). Both forms work.

`POST` and `PATCH` take a JSON object body. The API rejects a body larger
than 64 KiB with `413`, and a body that is not a JSON object with `400`.

## Status

```sh
curl -H "Authorization: Bearer $API_KEY" https://streamarchive.example.com/api/v1/status
```

```json
{
  "version": "1.1.3",
  "channels": 6,
  "recording": [],
  "monitoring_interval_s": 60.0
}
```

`recording` lists the channels that capture a stream at this moment.
`monitoring_interval_s` is the poll interval.

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
    "tunnel": "cloudflare",
    "public_url": "https://streamarchive.example.com"
  },
  "kick_webhook": {"enabled": true},
  "api": {"enabled": true, "base_url": "https://streamarchive.example.com/api/v1/"},
  "monitoring_interval_s": 60.0
}
```

The answer never contains a secret. The API key, the bot token, and the
tunnel token stay in `config.json`.

### Write

`PATCH /api/v1/settings` accepts these keys:

| Key | Value | Effect |
| --- | --- | --- |
| `output_mode` | `disk`, `youtube`, `both` | Output of every channel without an override |
| `preferred_quality` | `best`, `1080p`, `720p`, `480p`, `360p`, `audio_only` | Quality for every channel without an override |
| `retention_days` | whole number ≥ 0 | Delete recordings older than this many days. `0` disables cleanup |
| `max_concurrent_recordings` | whole number ≥ 0 | Recording limit. `0` = unlimited |
| `max_concurrent_youtube_streams` | whole number ≥ 0 | YouTube re-stream limit. `0` = unlimited |
| `record_chat` | `true`, `false` | Record Twitch chat |
| `kick_record_chat` | `true`, `false` | Record Kick chat |
| `disk_max_total_gb` | number ≥ 0 | Archive size cap. `0` disables the cap |
| `disk_delete_oldest` | `true`, `false` | On a full disk, delete the oldest recordings (`true`) or stop new recordings (`false`) |

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

One request can hold one key or more. The API applies each key on its own,
so one bad key does not block the others. `applied` names each key that
worked, and `errors` names each key that failed.

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

The fields without `_override` are the values that the monitor uses. A
value with `_override` is set for that channel alone. `null` means "use the
global setting".

### Add

```sh
curl -X POST https://streamarchive.example.com/api/v1/channels \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"channel": "twitch:example"}'
```

The API takes a channel name with a platform prefix or a profile URL
(`https://twitch.tv/example`, `https://kick.com/example`). The answer holds
the message, the normalized channel name, and the new channel list.
Subscriptions follow at once: the bot creates the EventSub subscription for
Twitch and the webhook subscription for Kick.

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

The API stops a live recording of that channel, clears its overrides, and
deletes its webhook subscription. The last channel cannot be removed: the
config needs at least one channel.

## Error answers

A request that fails as a whole answers with `error`:

| Status | Body | Cause |
| --- | --- | --- |
| `400` | `{"error": "…"}` | Bad JSON, unknown key, or an invalid value |
| `401` | `{"error": "unauthorized"}` | Missing or wrong API key |
| `404` | `{"error": "…"}` | The API is off, or the channel is not monitored |
| `409` | `{"applied": {}, "errors": {"quality": "…"}}` | A quality change to `audio_only` for a channel that re-streams to YouTube |
| `413` | `{"error": "request body too large"}` | Body above 64 KiB |

The `409` case needs a confirm press in Telegram, because audio-only cannot
go to YouTube. The bot switches the output of that channel to `disk` only
after you confirm. The API changes nothing.

## Behavior

- Every applied change sends the admin a Telegram message with the origin
  `Control API`, so you see what an API client did.
- Every change goes through the same validation and the same atomic
  `config.json` write as a Telegram change. A rejected change changes
  nothing.
- A change applies on the next monitoring cycle. A recording that is in
  progress keeps the settings of its start.
- The API serves the same settings as the bot. It cannot change secrets,
  the endpoint, or the API itself.

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
