# Web panel

The browser panel controls the app without Telegram. It replaces the Telegram bot: status, channels, settings, recordings with a video player, reload, restart, update check, Kick delivery test, API key show and rotate, and password change. The Telegram bot and the web panel can run at once.

The web panel lives on the private listener at the domain root. It runs while the endpoint, the control API, or the web panel itself is on.

## Enable the web panel

The setup wizard sets the web panel up as one of its steps. It is the recommended path. To enable it by hand, do the steps that follow.

1. Turn on the private listener: enable the endpoint, the control API, or the web panel itself (see [Kick webhook](kick-webhook.md)). The endpoint alone is sufficient. Then set its public URL.
2. Set a password: open **Settings → Remote access → Web panel** in the Telegram bot and tap **Enable Web panel** (the first enable generates the password and shows it once), or run `stream-archive-setup-web` on the host. The command stores a hash, plus a session secret on first use. The password never reaches disk or logs. Docker: `docker compose exec stream-archive stream-archive-setup-web`.
3. Set `web.enabled` to `true` in `config.json` (the setup command does this) and restart.
4. Open `<endpoint.public_url>/` and log in.

Without Telegram tokens the Telegram bot stays off and the web panel controls the app. Set `telegram_user_id` to `0` and `bot_telegram_api` to `""` for that mode. With tokens set, both control surfaces work at once.

## Sessions

A login creates a server-side session of 12 hours. The app stores the session in `web_sessions.json` next to `config.json`, so the login survives restarts of the app and the container. The cookie is HttpOnly and SameSite=Lax (Secure outside local access). Every change call needs the CSRF token the login returns. Logout ends the session. A password change ends all sessions at once.

Failed logins are rate limited per address (10 per 10 minutes) and answered slowly. The log records logins, logouts, and password changes.

## Expose it safely

The web panel is powerful. Keep it off the open internet when you can.

- Best for private use: Tailscale Serve. It keeps the web panel inside your tailnet with no public ingress.
- For public access: your own reverse proxy plus identity-aware access (for example Cloudflare Tunnel plus Cloudflare Access). Access checks identity first, and the web panel password stays as a second factor.
- Keep `endpoint.listen_host` on loopback on bare metal and let the proxy forward to it. Under Docker set it to `0.0.0.0`, so the host proxy reaches the container. Never publish port 8787 directly.

## Differences from the Telegram bot

- Recordings stream and download in the browser. Finished captures are MP4 (M4A for audio-only: the recorder remuxes when the stream ends) and play inline. A file that still records shows a REC badge with no actions: it unlocks when the stream ends. Live captures cannot be deleted either. On wide screens the recordings list, the player, and the recorded chat sit side by side, and the chat follows the video position.
- The web panel cannot send a recording to your Telegram chat. Use the Telegram bot for MTProto upload.
- The web panel edits remote access too: the endpoint toggle and URL, the Kick toggle and URL, the API toggle, and the global YouTube hold. A first API enable shows the new key once. Listener binds, ports, and all secrets stay out: use the setup wizard or edit `config.json` for those.
- Every web panel change writes `config.json` atomically like a Telegram bot change. A rejected change writes nothing.

## Settings the panel edits

The panel edits the same 15 global keys as `PATCH /api/v1/settings`: `output_mode`, `preferred_quality`, `retention_days`, `max_concurrent_recordings`, `max_concurrent_youtube_streams`, `record_chat`, `kick_record_chat`, `disk_max_total_gb`, `disk_delete_oldest`, `youtube_hold_seconds`, `endpoint_enabled`, `endpoint_public_url`, `kick_webhook_enabled`, `kick_webhook_public_url`, `api_enabled`. It also adds and removes channels and edits per-channel `output_mode`, `quality`, and `youtube_hold_seconds`. Picking audio-only quality for a channel that restreams asks to confirm first: on confirm the panel switches that channel to disk output and then sets the quality, like the Telegram bot.

It cannot touch the read-only keys of the control API: listener binds and ports, `kick.client_id` and `kick.client_secret`, `web.*`, `youtube.privacy_status`, `timezone`, `monitoring_interval`, `proxy_list`, the plugin, recording, chat, and session dirs, `eventsub.enabled`, `update_check.*`, and all secrets except the API key card, which shows and rotates `api.key`. See [Control API](control-api.md) for the full read-only list.

## Related guides

- [Settings](configuration.md) lists the `web` keys.
- [Control API](control-api.md) changes the same settings over HTTP.
- [Telegram control](telegram-control.md) lists the Telegram bot commands.
