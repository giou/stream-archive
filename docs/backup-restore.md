# Backup and restore

The data directory holds all state of the app. Back it up by copying the folder while the app is stopped.

## What the data directory holds

| Entry | Content |
| --- | --- |
| `config.json` | Settings and secrets. Mode `0600`. The app tightens the mode to `0600` on write |
| `config.json.bak` | Copy of the prior `config.json`. The reset step of the setup wizard writes it |
| `recordings/` | Video and audio captures (`.ts`, `.m4a`, `.mp4`) |
| `chat/` | Chat files (`.chat.json`), plus in-progress `.chat.json.tmp` files |
| `youtube_token.json` | YouTube OAuth token. Mode `0600` |
| `client_secret.json` | Google OAuth client file. You place it here before the authorization flow |
| `update_state.json` | Last notified app release. A stale copy re-fires the update alert |
| `web_sessions.json` | Live web panel sessions |
| `mtproto.session` | MTProto account auth. Never share it. A journal file sits next to it |
| `events.jsonl` | Recent event feed for the web panel and the restart path |
| `cloudflared/<tunnel-id>.yml` | Generated tunnel ingress file. Back it up with the rest |
| `nginx/<host>.conf` | Generated nginx server block. Back it up with the rest |
| `.cache/thumbnails/` | Cached recording thumbnails. Safe to drop: the app rebuilds them |

Do not back up `*.tmp` files. They are partial writes from an interrupted run. The app ignores or rebuilds them on boot.

## Restore

1. Stop the app (`docker compose stop`).
2. Copy the backup into the data directory.
3. Keep the `0600` modes on `config.json`, `youtube_token.json`, and `mtproto.session`.
4. Start the app (`docker compose up -d`).

Restore rules:

- Stop the app first. A running app overwrites restored files on its next write.
- Keep the `0600` modes. Loose modes expose secrets to other users on the host.
- A restore without `web_sessions.json` plus `web.session_secret` logs everyone out. Users log in again with the panel password. Nothing else breaks.
- `mtproto.session` is account auth. Never share it and never post it. A leaked session lets a stranger act as the account.
- A stale `update_state.json` re-fires the update alert for the recorded release. Delete the file to silence it. The next check writes a fresh one.

## Related guides

- [Running](running.md) names the data directory and the move path.
- [Settings](configuration.md) lists every key of `config.json`.
- [Web panel](web-control.md) explains sessions and the password change.
- [Kick webhook](kick-webhook.md) explains the generated proxy files.
