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
MP4 generation).

The app writes chat to disk while the recording runs, so process memory stays
flat when chat volume is high. The comments land in `<name>.chat.json.tmp`
first. Every termination path (stream offline, disk watchdog abort, task
failure, restart, `SIGTERM`/`SIGINT`) writes the closing keys and renames the
file into place. A crash cannot corrupt an existing `.chat.json`. A crash
leaves a partial `.tmp` file, and the `retention_days` cleanup removes stale
`.tmp` files together with the recordings.

The Kick emote step is bounded per recording: 1024 distinct emote ids, 512 KiB
for one image, and 16 MiB in total. An over-limit emote keeps its text token,
so TwitchDownloader renders plain text there.

Chat files count toward `disk.max_total_gb` together with the recordings. The
disk watchdog measures the total of both. When the total is over the cap and
`disk.delete_oldest` is true, the app deletes the oldest archive files,
including chat files. It never deletes a file that a recording still writes.

If a chat write fails (for example, the disk is full), chat capture stops for
that recording and the app sends a Telegram notification. The video keeps
recording. The partial chat stays in the `.tmp` file.

## Related guides

- [Configuration](configuration.md) lists `record_chat`, `kick.record_chat`,
  and `chat_dir`.
- [Telegram control](telegram-control.md) holds the `/chat` command.
