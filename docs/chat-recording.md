# Chat recording

When chat recording is on, every recording also captures chat, in every output
mode. The app writes the files in the `TwitchDownloader` `ChatRoot` format to
`chat_dir/<platform>/<channel>/<title>-<ts>.chat.json`.

- **Twitch.** The app holds an IRC connection to the channel chat
  (`record_chat`, default on). Use `chatupdate -E` to embed emotes, badges, and
  avatars into a copy. Then the file is fully self-contained.
- **Kick.** The app buffers `chat.message.sent` webhook events
  (`kick.record_chat`, default on, requires the webhook). It downloads the
  emote images and embeds them as base64, so the file is self-contained.

`TwitchDownloaderCLI` consumes these files directly:

```sh
# enrich the file (embed emotes/badges/avatars) and/or render it:
TwitchDownloaderCLI chatupdate -i chat/<platform>/<channel>/<title>-<ts>.chat.json -o out.chat.json -E
TwitchDownloaderCLI chatrender -i out.chat.json -o chat.mp4
```

## Behavior

StreamArchive writes the JSON only. It does no rendering (no ffmpeg, no HTML or
MP4 generation). The app holds chat in memory during the stream and writes the
file atomically on stop. Every termination path (stream offline, disk watchdog
abort, task failure, restart, `SIGTERM`/`SIGINT`) finalizes the file, so a crash
cannot corrupt an existing `.chat.json`. The `retention_days` cleanup also
removes old `*.chat.json` files together with the recordings.

## Related guides

- [Configuration](configuration.md) lists `record_chat`, `kick.record_chat`,
  and `chat_dir`.
- [Telegram control](telegram-control.md) holds the `/chat` command.
