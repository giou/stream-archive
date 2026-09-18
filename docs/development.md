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

The recorder imports the streamlink-ttvlol plugin into its own process. Each
image build downloads the plugin release, checks that GitHub served it from
that project, parses it, and records its sha256 in the build log and in
`/app/plugins/twitch.py.sha256`, so a running image can be identified without
a build log:

```sh
docker run --rm --entrypoint cat ghcr.io/giou/stream-archive:latest /app/plugins/twitch.py.sha256
```

A plain `docker build` needs no arguments. To install only bytes you reviewed,
pass `TTVLOL_PLUGIN_SHA256`; the build then refuses any other content:

```sh
TAG=8.3.0-20260701
curl -fsSL -o /tmp/twitch.py \
  "https://github.com/2bc4/streamlink-ttvlol/releases/download/${TAG}/twitch.py"
sha256sum /tmp/twitch.py
docker build \
  --build-arg TTVLOL_PLUGIN_VERSION="${TAG}" \
  --build-arg TTVLOL_PLUGIN_SHA256=<digest from the line above> .
```

A tag without a digest pins the release, not the bytes: a GitHub release asset
is mutable, so the same tag can serve different content later. The digest is
what pins content, and passing it is also what invalidates a cached plugin
layer. The publish workflow always passes both values, so a published image
matches the digest in its release log.

Dependabot cannot watch a release asset, so bump the plugin by hand with the
commands above.

Users of the image pull a new release and change no file. The bot reports app
releases only in `/update`.

## Development container

The image installs the app in editable mode from `/app/src`. Thus a bind
mount runs the working tree, and a code change needs no new build.

```sh
cd ~/stream-archive-data
docker compose -f docker-compose.yml -f ~/stream-archive/docker-compose.dev.yml up -d
```

`docker-compose.dev.yml` mounts `src/stream_archive` read-only over the copy
in the image. It also sets `restart: "no"`, so a broken import stops the
container instead of a restart loop. Set `STREAM_ARCHIVE_SRC` when the
checkout is not next to the data directory. Compose resolves a relative path
against the project directory, that is the directory of the first `-f` file.

A change to the Dockerfile, `entrypoint.sh`, the dependencies, or the plugin
needs a build. Build the image, then uncomment the `image:` line in the
overlay. The build downloads and records the plugin on its own, so no
arguments are needed:

```sh
docker build -t stream-archive:dev .
```

To pin the plugin to bytes you reviewed, pass the tag and the digest as in
[Dependency updates](#dependency-updates).

To return to the published image, run `docker compose up -d`. The plain
command without `-f` always uses the released image.

## Plugin override

To test a plugin release without a new image, mount a directory over
`/app/plugins` and keep `plugin_dir` at `/app/plugins`. The mounted file is
used as it is: no digest check runs, and `twitch.py.sha256` describes the
image's copy, not this one:

```yaml
services:
  stream-archive:
    volumes:
      - ./plugins:/app/plugins   # hosts twitch.py; overrides the copy in the image
```

The overlay holds the code that the recorder runs. Remove the mount to return
to the plugin that ships with the image.
