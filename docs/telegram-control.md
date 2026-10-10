# Telegram control

Only the admin user (`telegram_user_id`) gets replies from the Telegram bot. The Telegram bot registers a command menu (type `/`). It also offers a `/settings` reply keyboard.

The root menu holds **Channels**, **Recordings**, and **Settings**. **Settings** holds **Output mode**, **Quality**, **Chat recording**, **Storage & limits**, **Remote access**, and **MTProto upload**. **Remote access** holds the public URLs, the Kick webhook toggle and delivery test, the control API, and the web panel. A submenu holds four buttons at most, plus **Back**. A toggle button names the action that applies now, for example **Disable Kick webhook**. The current value of a preset list carries a check mark, for example **✓ 1080p**. A value outside the presets marks **Custom**. Destructive actions (remove a channel, enable delete-oldest, delete a recording) use inline confirmation buttons. The Telegram bot re-sends the settings menu after every restart, so the reply keyboard survives updates and reboots.

The app validates each change and writes it atomically to `config.json`. The change applies on the next poll cycle. A failed command leaves memory and disk untouched.

## Commands

| Command | Action |
| --- | --- |
| `/help` | List the available commands |
| `/start` | Show the available commands and open the settings menu |
| `/settings` | Open the settings menu (reply keyboard buttons) |
| `/status` | Monitored channels, output mode, retention, chat-recording state, quality, concurrency limits, disk usage and limits, update-check state, Kick webhook state, and channels that record now |
| `/channels` | Numbered list of monitored channels |
| `/add <channel\|url>` | Start monitoring a channel: `twitch:<name>`, `kick:<name>`, or a `twitch.tv`/`kick.com` profile URL. The app creates the subscriptions at once |
| `/remove <channel>` | Stop monitoring a channel. A live recording stops and the app deletes the webhook and EventSub subscriptions of the channel |
| `/retention <days>` | Set `retention_days`. `0` disables cleanup |
| `/mode [channel] <disk\|youtube\|both\|default>` | Set `output_mode`, or a per-channel override. `default` clears the override. Applies to new recordings |
| `/reload` | Re-read `config.json` from disk, then re-apply the endpoint and control API state and re-sync the webhook and EventSub subscriptions. A changed `telegram_user_id` takes effect at once. Keys that need a restart get named in the reply |
| `/restart` | Gracefully restart the app |
| `/update` | Look for a new app release now. The app downloads and applies nothing. Apply an update with `docker compose pull && docker compose up -d` |
| `/quality [channel] <value\|default>` | Show the preferred quality, or set it globally or for one channel (`best`, `1080p`, `720p`, …, `audio_only`). `default` clears the per-channel override |
| `/category [channel] <names\|default>` | Show or set the category filter of one channel. Names are comma-separated (`/category twitch:example Just Chatting, Music`). `default` clears the filter. With no channel the reply lists every filter |
| `/maxrecordings [n]` | Show or set the concurrent recording limit (`0` means unlimited) |
| `/maxyoutube [n]` | Show or set the concurrent YouTube restream limit (`0` means unlimited) |
| `/chat [on\|off] [twitch\|kick]` | Show whether chat recording is on, or set it (globally, or for one platform with `twitch`/`kick`). `off` stops chat capture in flight and finalizes it. Video recordings continue |
| `/hold <seconds>` | Set the global YouTube hold delay (`0` ends the restream at once) |
| `/recordings` | Browse stored recordings. Tap a file to send it over MTProto (up to 2 GB) or delete it |

## Notes

- `/mode` applies to new recordings. A recording in flight finishes in the mode of its start. A per-channel override wins over the global `output_mode`. `/status` lists the active overrides, and `/remove` clears the override of that channel.
- `audio_only` records sound without video. On Twitch, streamlink supplies an audio-only stream. On Kick, ffmpeg strips the video from the 480p stream. YouTube does not accept an audio-only live stream.
- When you select `audio_only` for a channel with `youtube` or `both` output, the Telegram bot asks you to confirm. If you confirm, the Telegram bot sets the quality and switches the output of that channel to `disk`. If you cancel, nothing changes. The recorder also forces `disk` for audio-only channels as a safety net. Audio-only recordings land as `.m4a` without re-encoding.
- The per-channel output mode override lives under `/settings → Channels → <channel> → Mode`. `Global` clears the override back to the global `output_mode`. The change applies to the next recording of that channel.
- The per-channel YouTube hold delay lives under `/settings → Channels → <channel> → Hold delay` (presets, `0` = off, or a custom value in seconds). `Global` clears the override back to the global `youtube.hold_seconds`. The app reads the value when a recording stops.
- The per-channel category filter lives under `/settings → Channels → <channel> → Categories`. Send comma-separated names to set it, or tap **Clear filter**. A refused change stays on the menu, so the names can be fixed at once.
- `/chat off` applies at once. The app stops and finalizes the capture in flight, and new recordings start without chat until `/chat on`. `/chat on` affects new recordings only. A platform toggle (`/chat off twitch`) affects only that platform.
- A category filter records a channel only while its live category is in the list. If the stream starts in another category, the app skips it. If the category changes mid-stream, the next poll cycle stops the recording. The poll interval sets that delay. To filter a channel, set its names with `/category <channel> <names>`. Each name must exist on Twitch or Kick: an unknown name refuses the whole change, and a close match is suggested. Names under 3 letters check against the full Kick list. To record everything again, clear the filter with `/category <channel> default`. `/remove` clears the filter of that channel.
- `/retention` and `/reload` apply at once. The cleanup loop and the monitor read the live settings every cycle. A `/reload` also stops the recording, the chat capture and the restream of a channel that left `channels` in the file.
- `/restart` replies first, then triggers the scheduler shutdown. The compose policy `restart: unless-stopped` relaunches the container.
- `Recordings` lists stored recordings newest first, five per page. A tap opens the file with **Upload** and **Delete**. A file that records now cannot be deleted. **Upload** offers **Telegram** and **YouTube**. **Telegram** needs MTProto upload (**Settings** → **MTProto upload**). The Bot API allows 50 MB. MTProto allows files under 2 GB. Files over the cap split into parts. Each part shows its own bar with a Stop button. If you press Stop, the upload stops and the files stay on disk. **YouTube** needs a YouTube login (`stream-archive-setup-youtube`). It uploads the file as a video. The title comes from the file name. The privacy follows the YouTube settings. Each upload shows a bar with ETA and a Stop button. A finished upload leaves its link on the file. Uploading again asks for confirm and replaces the link. The per-channel auto-upload lives under **Channels** → **<channel>** → **VOD upload** (`/vodupload <channel> <on|off|default>`). It uploads each finished recording at once.
- Secrets (Telegram bot token, Twitch credentials, proxy credentials, Kick credentials, MTProto api id and hash) never appear in `/status`. You cannot change them over the Telegram bot.

## Related guides

- [Kick webhook](kick-webhook.md) explains the **Remote access** menu.
- [Control API](control-api.md) makes the same changes over HTTP.
- [Web panel](web-control.md) replaces the Telegram bot in the browser. Turn it on under **Settings → Remote access → Web panel**: the first enable generates the web panel password and shows it once. **New password** replaces it and ends all browser sessions.
