# Telegram control

Only the admin user (`telegram_user_id`) gets replies from the bot. The bot
registers a command menu (type `/`). It also offers a `/settings` reply
keyboard.

The root menu holds **Channels**, **Output mode**, **Quality**, **Chat
recording**, **Storage & limits** (retention, disk, and the two concurrency
limits), and **Remote access** (the public URL tunnels, the Kick webhook
toggle, and the HTTP control API). A submenu holds four buttons at most, plus
**Back**. A toggle button names the action that applies now, for example
**Disable Kick webhook**. The current value of a preset list carries a check
mark, for example **✓ 1080p**. A value outside the presets marks **Custom**.
The channel list keeps **Back** in the first row, because the list can grow
long. Destructive actions (remove a channel, enable delete-oldest) use inline
confirmation buttons. The bot re-sends the settings menu after every restart,
so the reply keyboard survives updates and reboots.

The app validates each change and writes it atomically to `config.json`. The
change applies on the next poll cycle. A failed command leaves memory and disk
untouched.

## Commands

| Command | Action |
| --- | --- |
| `/help` | List the available commands |
| `/start` | Show the available commands and open the settings menu |
| `/settings` | Open the settings menu (reply keyboard buttons) |
| `/status` | Monitored channels, output mode, retention, chat-recording state, quality, concurrency limits, disk usage and limits, update-check state, Kick webhook state, and channels that record now |
| `/channels` | Numbered list of monitored channels |
| `/add <channel\|url>` | Start monitoring a channel: `twitch:<name>`, `kick:<name>`, or a `twitch.tv`/`kick.com` profile URL. The app creates the subscriptions immediately |
| `/remove <channel>` | Stop monitoring a channel. A live recording stops (the offline notification goes out) and the app deletes the webhook and EventSub subscriptions of the channel |
| `/retention <days>` | Set `retention_days`. `0` disables cleanup |
| `/mode [channel] <disk\|youtube\|both\|default>` | Set `output_mode`, or a per-channel override. `default` clears the override. Applies to new recordings |
| `/reload` | Re-read `config.json` from disk, then re-apply the endpoint and API state and re-sync the webhook and EventSub subscriptions |
| `/restart` | Gracefully restart the app |
| `/update` | Check for a new app release now. Check-only: the app downloads and applies nothing. Apply an update with `docker compose pull && docker compose up -d` |
| `/quality [channel] <value\|default>` | Show the preferred quality, or set it globally or for one channel (`best`, `1080p`, `720p`, …, `audio_only`). `default` clears the per-channel override |
| `/maxrecordings [n]` | Show or set the concurrent recording limit (`0` = unlimited) |
| `/maxyoutube [n]` | Show or set the concurrent YouTube re-stream limit (`0` = unlimited) |
| `/disk` | Show disk limits |
| `/disk <maxsize\|delete_oldest> <value>` | Set a disk limit. `maxsize` takes GB. `delete_oldest` takes `on`/`off` |
| `/chat [on\|off] [twitch\|kick]` | Show whether chat recording is on, or set it (globally, or for one platform with `twitch`/`kick`). `off` stops chat capture in flight and finalizes it. Video recordings continue |

## Notes

- `/mode` applies to new recordings. A recording in flight finishes in the
  mode of its start. A per-channel override wins over the global `output_mode`.
  `/status` lists the active overrides, and `/remove` clears the override of
  that channel.
- `audio_only` records sound without video. On Twitch, streamlink supplies an
  audio-only stream. On Kick, ffmpeg strips the video from the 480p stream.
  YouTube does not accept an audio-only live stream.
- When you select `audio_only` for a channel with `youtube` or `both` output,
  the bot asks you to confirm. If you confirm, the bot sets the quality and
  switches the output of that channel to `disk`. If you cancel, nothing
  changes. The recorder also forces `disk` for audio-only channels as a safety
  net. Audio-only recordings are saved as `.m4a`: ffmpeg remuxes the AAC track
  into a fragmented MP4 without re-encoding.
- The per-channel output mode override lives under
  `/settings → Channels → <channel> → Mode`. `Global` clears the override back
  to the global `output_mode`. The change applies to the next recording of that
  channel.
- The per-channel YouTube hold delay lives under
  `/settings → Channels → <channel> → Hold delay` (presets, `0` = off, or a
  custom value in seconds). `Global` clears the override back to the global
  `youtube.hold_seconds`. The app reads the value when a recording stops, so it
  applies to the next stop immediately.
- `/chat off` applies immediately. The app stops and finalizes the capture in
  flight, and new recordings start without chat until `/chat on`. `/chat on`
  affects new recordings only. A platform toggle (`/chat off twitch`) affects
  only that platform, and the other platform keeps running.
- `/retention` and `/reload` apply immediately. The cleanup loop and the
  monitor read the live settings every cycle.
- `/restart` replies first, then triggers the scheduler shutdown. The compose
  policy `restart: unless-stopped` relaunches the container.
- Secrets (bot token, Twitch credentials, proxy credentials, Kick credentials,
  tunnel tokens) are never printed by `/status`. You cannot change them over
  Telegram.

## Related guides

- [Kick webhook](kick-webhook.md) explains the **Remote access** menu.
- [Control API](control-api.md) makes the same changes over HTTP.
