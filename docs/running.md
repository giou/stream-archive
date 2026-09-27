# Running

The image owns all code. Your data directory owns all state. The container
root filesystem is read-only. Only the mounted data directory and `/tmp`
(tmpfs) are writable.

```sh
docker compose up -d    # start
docker compose logs -f  # follow logs
docker compose stop     # graceful shutdown: recordings stopped, broadcasts ended
```

To update the app, pull the new image:

```sh
docker compose pull && docker compose up -d
```

## Upgrading past managed tunnels

Versions before this one ran `cloudflared` and `tailscale funnel` for
you. That support is gone: the app binds loopback ports and you publish
them yourself.

- Old tunnel keys in `config.json` (`tunnel`, `cloudflare_token`,
  `cloudflare_managed`) are ignored on load. Your URLs stay.
- Publish the panel port over the tailnet (`tailscale serve`) and point
  your own public reverse proxy at the webhook port
  (`kick.webhook.listen_port`, default 8788).
- The setup wizard regenerates the proxy config for your pick, and the
  delivery test proves the new path in seconds.

## Data directory

The default data directory is the folder that holds `docker-compose.yml`, for
example `~/stream-archive-data/`. It holds `config.json`, `recordings/`,
`chat/`, `youtube_token.json`, `client_secret.json`, and
`update_state.json`. Treat the directory like a normal folder on the host.

Back it up by copying the folder. To move it to another disk, set
`STREAM_ARCHIVE_DATA` in `.env` in that folder. See the quick start in the
README.

## Container identity and time

- The container runs as the owner of the data directory. Recorded files stay
  manageable on the host. Set `USER_UID` and `USER_GID` in `.env` to force a
  specific identity. If Docker created the data directory as root, fix the
  ownership once with `sudo chown -R "$(id -u):$(id -g)" <data-dir>`. The
  entrypoint refuses to start as root: a root-owned data directory exits with
  an error that names the `chown` to run.
- Log timestamps follow the container timezone (`UTC` by default). Set
  `TZ=America/New_York` in the same `.env` to match the `timezone` setting.
- Compose rotates the logs (10 MB, 3 files).
## Docker networking

The compose file publishes two loopback ports: `127.0.0.1:8787` (panel and
control API) and `127.0.0.1:8788` (`POST /kick/webhook` alone). Set both
listen hosts to `0.0.0.0` in the settings, so the host proxies reach the
container. Publish the panel port over the tailnet (`tailscale serve`) and
point your own public reverse proxy at the webhook port. The panel never
shares a port with the public internet.

## First-time setup

```sh
docker compose run --rm stream-archive stream-archive-setup
```

Run this command in the data directory before the first start. The
wizard writes `config.json`: Twitch credentials and one control surface
(web panel, Telegram bot, or both). Channels come later, from the panel
or the bot. It also offers YouTube restream, Enable MTProto (upload to
Telegram), panel access, and Enable Kick. Run it again later to add a
feature or change a block. The single-purpose commands
`stream-archive-setup-youtube` and `stream-archive-setup-web` still work.

## Logs and shutdown

```sh
docker compose logs -f
```

`SIGTERM` and `SIGINT` trigger a graceful shutdown. All recordings stop. The
active YouTube broadcasts go to `complete`. The scheduler exits with
`[scheduler] Shutdown complete`.
