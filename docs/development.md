# Development

The repository holds the code. At runtime all state lives in the data
directory (see [Running](running.md)).

## Project layout

```
config.json.example      # template for the runtime settings (config.json is gitignored)
docker-compose.yml       # standalone deployment: image + data-directory bind mount
pyproject.toml
.pre-commit-config.yaml  # ruff + ruff-format + mypy hooks
src/stream_archive/
  scheduler.py           # entry point (stream-archive): logging, poll loop, signal handling
  setup_youtube.py       # one-time YouTube OAuth flow (entry point: stream-archive-setup-youtube)
  monitor.py             # start/stop/restart decisions, failure alerts (Twitch + Kick)
  eventsub.py            # Twitch EventSub conduit client (stream.online/offline fast path)
  kick_webhook.py        # Kick webhook receiver (/kick/webhook), signature verification, subscription sync
  api.py                 # HTTP control API (/api/v1): channels and settings on the webhook listener
  tunnels.py             # managed public tunnels: cloudflared process, tailscale funnel, tunnel tokens
  kick_api.py            # Kick OAuth client (token, channel statuses, webhook subscriptions, public key)
  kick_chat.py           # Kick chat -> TwitchDownloader ChatRoot conversion + emote embedding
  recorder/              # streamlink capture, ffmpeg pipe, task tracking, chat finalization (core + mixins)
  chat_recorder.py       # Twitch IRC chat capture (TwitchDownloader-compatible JSON)
  youtube_streamer.py    # YouTube Live API (broadcast/stream/bind/end)
  twitch_api.py          # Twitch Helix client (token, users, streams)
  notifier.py            # Telegram messages
  telegram/              # admin-only Telegram bot commands (/add /remove /mode …) + settings menus
  config.py              # typed config (Pydantic) + ${ENV_VAR} interpolation
  updater.py             # periodic app release check (GitHub releases) and /update
  disk.py                # disk-size watchdog (max_total_gb)
docs/                    # user guides (configuration, running, control API, …)
plugins/twitch.py        # dev-only: fetched from streamlink-ttvlol releases (baked into the image at build)
tests/                   # pytest suite (config, recorder, monitor, eventsub, kick api/webhook/chat, telegram, …)
```

## Commands

```sh
uv sync        # installs dev group (pytest, ruff, mypy, pre-commit)
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pre-commit run --all-files
```

The pre-commit hooks run those same tools through `uv`. Thus a commit checks
the versions in `uv.lock`, exactly like CI.

## Dependency updates

Dependabot checks three ecosystems every week and opens one pull request for
each update:

- `uv`: the Python dependencies in `pyproject.toml` and `uv.lock`, including
  streamlink.
- `docker`: the base images of the Dockerfile: python, cloudflared, and uv.
  Cloudflared and uv sit in their own build stages, so their tags stay
  visible to Dependabot.
- `github-actions`: the workflows in `.github/workflows`.

CI runs the five release gates on every pull request. Thus a merge cannot
break the lockfile or the tests.

The streamlink-ttvlol plugin is not pinned. Each image build fetches the
newest plugin release, so a rebuilt image carries the current plugin. The
publish workflow resolves the release tag and passes it as
`TTVLOL_PLUGIN_VERSION`. Dependabot cannot watch a release asset, and the tag
in the build log shows which release an image contains. To pin a plugin
release for a reproducible build, pass the tag yourself:

```sh
docker build --build-arg TTVLOL_PLUGIN_VERSION=8.3.0-20260701 .
```

Users of the image pull a new release and change no file. The bot reports app
releases only in `/update`.

## Plugin override

To test a plugin release without a new image, mount a directory over
`/app/plugins` and keep `plugin_dir` at `/app/plugins`:

```yaml
services:
  stream-archive:
    volumes:
      - ./plugins:/app/plugins   # hosts twitch.py; overrides the copy in the image
```

The overlay holds the code that the recorder runs. Remove the mount to return
to the plugin that ships with the image.
