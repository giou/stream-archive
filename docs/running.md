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

## Data directory

The default data directory is the folder that holds `docker-compose.yml`, for
example `~/stream-archive-data/`. It holds `config.json`, `recordings/`,
`chat/`, `youtube_token.json`, `client_secret.json`, `cloudflared/`, and
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

A host tailscale funnel forwards to the host loopback. The compose file
publishes the listener on `127.0.0.1:8787`. Set `endpoint.listen_host` to
`0.0.0.0` in the settings. Then the host tunnel reaches the container. The
image ships `cloudflared` and the tailscale CLI. The app mounts the host
tailscale directory (`/var/run/tailscale`) into the container. Set
`TAILSCALE_RUN_DIR` in `.env` when the host keeps its socket elsewhere. The
mount is optional: the stack starts without Tailscale.

## One-time YouTube OAuth

```sh
docker compose run --rm stream-archive stream-archive-setup-youtube
```

The browser opens on your host. Paste the full URL from the address bar at the
prompt, because the localhost redirect cannot reach the container. See
[YouTube setup](youtube-setup.md) for the full procedure.

## Logs and shutdown

```sh
docker compose logs -f
```

`SIGTERM` and `SIGINT` trigger a graceful shutdown. All recordings stop. The
active YouTube broadcasts go to `complete`. The scheduler exits with
`[scheduler] Shutdown complete`.
