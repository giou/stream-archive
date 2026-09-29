# Running

The image owns all code. Your data directory owns all state. The container root filesystem is read-only. Only the mounted data directory and `/tmp` are writable.

```sh
docker compose up -d    # start
docker compose logs -f  # follow logs
docker compose stop     # graceful shutdown: recordings stop, broadcasts end
```

To update the app, pull the new image:

```sh
docker compose pull && docker compose up -d
```

## First-time setup

Run the setup wizard in the data directory before the first start. It is the recommended path.

```sh
docker compose run --rm stream-archive stream-archive-setup
```

The setup wizard writes `config.json`: Twitch credentials, one control surface (web panel, Telegram bot, or both), and the first channels. It also offers YouTube restream, Telegram upload, web panel access, Kick, and the control API. Run it again later to add a block or change one. The single-purpose commands `stream-archive-setup-youtube` and `stream-archive-setup-web` still work.

## Data directory

The default data directory is the folder that holds `docker-compose.yml`, for example `~/stream-archive-data/`. It holds `config.json`, `recordings/`, `chat/`, `youtube_token.json`, `client_secret.json`, and `update_state.json`.

Back it up by copying the folder. See [Backup and restore](backup-restore.md) for the full contents and the restore rules. To move it to another disk, set `STREAM_ARCHIVE_DATA` in `.env` in that folder. See the setup in the README.

## Container identity and time

- The container runs as the owner of the data directory. Recorded files stay manageable on the host. Set `USER_UID` and `USER_GID` in `.env` to force a specific identity. If Docker created the data directory as root, fix the ownership once with `sudo chown -R "$(id -u):$(id -g)" <data-dir>`. The entrypoint refuses to start as root.
- Log timestamps follow the container timezone (`UTC` by default). Set `TZ=America/New_York` in the same `.env` to match the `timezone` setting.
- Compose rotates the logs (10 MB, 3 files).

## Docker networking

The compose file publishes two loopback ports: `127.0.0.1:8787` (web panel and control API) and `127.0.0.1:8788` (`POST /kick/webhook` alone). Set both listen hosts to `0.0.0.0` in the settings, so the host proxies reach the container. Publish the web panel port over the tailnet (`tailscale serve`) and point your own public reverse proxy at the webhook port. The web panel never shares a port with the public internet.

## Logs and shutdown

```sh
docker compose logs -f
```

`SIGTERM` and `SIGINT` trigger a graceful shutdown. All recordings stop. The active YouTube broadcasts go to `complete`. The scheduler exits with `[scheduler] Shutdown complete`.

## Health and readiness

`/healthz` on port 9100 answers `200` while the process runs. It says nothing about the work: a full disk or dead credentials still read healthy. `/readyz` on the same port answers `200` with `{"ready": true, "degraded": [...]}` once the clients exist, `503` while starting. The `degraded` list names present problems (`disk_full`, `twitch_auth`, `kick_auth`, `youtube_auth`). Point an orchestrator at `/readyz` when it must tell starting apart from broken.
