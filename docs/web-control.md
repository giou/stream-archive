# Web control panel

The browser panel controls the app without Telegram. It replaces the bot:
status, channels, settings, recordings with a video player, reload,
restart, update check, and password change. The bot and the panel can run
at once.

The panel lives on the shared listener at the domain root. It runs while the
endpoint, the control API, or the panel itself is on.

## Enable the panel

1. Enable the endpoint and set its public URL (see [Kick webhook](kick-webhook.md)).
2. Set a password: open **Settings → Remote access → Web panel** in Telegram
   and tap **Enable Web panel** (the first enable generates the password and
   shows it once), or run `stream-archive-setup-web` on the host.
   The command stores a hash, plus a session secret on first use. The password never reaches disk or logs.
   Docker: `docker compose exec stream-archive stream-archive-setup-web`.
3. Set `web.enabled` to `true` in `config.json` (the setup command does
   this) and restart.
4. Open `<endpoint.public_url>/` and log in.

Without Telegram tokens the bot stays off and the panel controls the app.
Set `telegram_user_id` to `0` and `bot_telegram_api` to `""` for that
mode. With tokens set, both control surfaces work at once.

## Sessions

A login creates a server-side session of 12 hours. The app stores the
session in `web_sessions.json` next to `config.json`, so the login
survives restarts of the app and the container. The cookie is HttpOnly
and SameSite=Lax (Secure outside local access). Every change call needs
the CSRF token the login returns. Logout ends the session. A password
change ends all sessions at once.

Failed logins are rate limited per address (10 per 10 minutes) and
answered slowly. The log records logins, logouts, and password changes.

## Expose it safely

The panel is powerful. Keep it off the open internet when you can.

* Best for private use: Tailscale Serve. It keeps the panel inside your
  tailnet with no public ingress. Use Funnel only when you need public
  access.
* For public access: Cloudflare Tunnel plus Cloudflare Access. Access
  checks identity first, and the panel password stays as a second factor.
* Keep `endpoint.listen_host` on loopback and let the tunnel forward to
  it. Never publish port 8787 directly.

## Differences from the bot

* Recordings stream and download in the browser. Finished captures are MP4
  (M4A for audio-only: the recorder remuxes when the stream ends) and play
  inline. A file that still records shows a REC badge with no actions: it
  unlocks when the stream ends. Live captures cannot be deleted either.
  On wide screens the recordings list, the player, and the recorded chat
  sit side by side, and the chat follows the video position.
* The panel cannot send a recording to your Telegram chat. Use the bot for
  MTProto upload.
* The panel cannot manage tunnels, the Kick webhook, or the control API
  itself. Use the bot or edit `config.json` for those.
* Every panel change writes `config.json` atomically like a bot change.
  A rejected change writes nothing.

## Related guides

* [Configuration](configuration.md) lists the `web` keys.
* [Control API](control-api.md) changes the same settings over HTTP.
* [Telegram control](telegram-control.md) lists the bot commands.
