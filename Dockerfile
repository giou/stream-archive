# cloudflared: the bot runs the cloudflare quick/named tunnel itself. The
# official image is multi-arch, so no architecture case is needed. The tag is
# a plain image reference, so Dependabot opens the version bumps.
FROM cloudflare/cloudflared:2026.9.1 AS cloudflared

# Pinned uv version used to build the image. A separate stage keeps the tag
# visible to Dependabot.
FROM ghcr.io/astral-sh/uv:0.12.15 AS uv

FROM python:3.14.7-slim

# Runtime dependencies. Chromium is not needed. The recorder plays live streams
# through the ttvlol playlist proxies (config proxy_list -> plugin option
# proxy-playlist). These proxies handle client integrity on the server side.
# plugins/twitch.py contains a browser (CDP) fallback for token acquisition,
# but this app never triggers it. The recorder sets neither
# proxy-playlist-exclude nor proxy-playlist-fallback.
#
# Tailscale CLI. The Telegram bot enables `tailscale funnel` for the public
# endpoint. It talks to the host's tailscaled through the socket that
# docker-compose mounts at /var/run/tailscale/tailscaled.sock. We install from
# the official pkgs.tailscale.com repo with install.sh. The Alpine base was
# considered and rejected because Tailscale publishes no official Alpine
# packages (install.sh falls back to the community-maintained apk there).
# install.sh runs as root, and Tailscale does not version it, so the build
# verifies it against the digest below and fails closed on a mismatch. The
# download also goes to a file first, so a failed fetch stops the build here
# instead of hiding behind the status of a `curl ... | sh` pipeline.
# To bump: curl -fsSL https://tailscale.com/install.sh | sha256sum and update
# the argument below in the same pull request.
#
# curl exists for install.sh only, which needs it (or wget) to add the apt repo,
# and the purge drops the CLI from the runtime image in the same layer.
ARG TAILSCALE_INSTALL_SHA256=4207f322e10ad26b3054abe7c99dfb54da09843c8d0a50d0a82f8eff4972a0d1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates curl \
 && curl -fsSL -o /tmp/tailscale-install.sh https://tailscale.com/install.sh \
 && printf '%s  tailscale-install.sh\n' "${TAILSCALE_INSTALL_SHA256}" > /tmp/tailscale-install.sh.sha256 \
 && (cd /tmp && sha256sum -c tailscale-install.sh.sha256) \
 && sh /tmp/tailscale-install.sh \
 && rm -f /tmp/tailscale-install.sh /tmp/tailscale-install.sh.sha256 \
 && apt-get purge -y curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared

WORKDIR /app

# twitch.py plugin (2bc4/streamlink-ttvlol, BSD-2-Clause). The recorder imports
# this file into its own process, so it runs with the app's authority: the bot
# token, the Twitch and Kick client secrets, the YouTube token, and the data
# directory.
#
# The file is vendored under vendor/streamlink-ttvlol/<tag>/. The build reads it
# from the context, so it needs no network, and the bytes that ship are the
# bytes a reviewer read in the pull request. TTVLOL_PLUGIN_VERSION names that
# directory. See vendor/streamlink-ttvlol/README.md.
#
# TTVLOL_PLUGIN_SHA256 is the digest of that file. The build verifies the copy
# against it and records it in /app/plugins/twitch.py.sha256, so a running image
# can be identified without a build log. The digest, not the tag, pins the
# content: a GitHub release asset is mutable. CI checks the vendored file
# against this value, so an edit fails until both move together.
#
# To bump: the scheduled workflow .github/workflows/ttvlol-bump.yml opens a
# pull request that adds the new file, updates both arguments, and shows the
# upstream diff.
ARG TTVLOL_PLUGIN_VERSION=8.3.0-20260701
ARG TTVLOL_PLUGIN_SHA256=4d465380159ec59f7caef6cb6a28368bbbbd3abcf80886138182184c30f2fad0
COPY vendor/streamlink-ttvlol/${TTVLOL_PLUGIN_VERSION}/twitch.py /app/plugins/twitch.py
RUN printf '%s  twitch.py\n' "${TTVLOL_PLUGIN_SHA256}" > /app/plugins/twitch.py.sha256 \
 && (cd /app/plugins && sha256sum -c twitch.py.sha256) \
 && python -c "import ast; ast.parse(open('/app/plugins/twitch.py').read())" \
 && echo "TTVLOL plugin: ${TTVLOL_PLUGIN_VERSION} sha256 ${TTVLOL_PLUGIN_SHA256}"

# Two-stage dependency install so source edits do not invalidate the dep layer.
# Stage 1 resolves and installs third-party deps only. Build caches it until
# uv.lock or project metadata changes. Stage 2 adds the project itself from
# src/. Both stages mount the uv binary instead of copying it: nothing at
# runtime runs uv, and the mount keeps the binary out of the image. Each stage
# drops the uv cache it writes, so the layers hold /opt/venv only.
COPY pyproject.toml uv.lock README.md ./

# The venv lives outside /app so a read-only /app cannot hide it. It sits on the
# image rootfs: under `read_only: true` it is read-only too, which is fine
# because the app only reads it, and PYTHONDONTWRITEBYTECODE stops Python from
# writing __pycache__ there. Runtime writes go to the data dir and /tmp.
# PTB 22 moves time periods from numbers to datetime.timedelta. The
# PTB_TIMEDELTA flag turns on that form now, so this image never reads the
# deprecated int form (telegram/_utils/datetime.get_timedelta_value).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PTB_TIMEDELTA=1
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-install-project \
 && rm -rf /root/.cache/uv
COPY src ./src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev \
 && rm -rf /root/.cache/uv

# HOME must be writable by the (non-root) runtime user: Streamlink's plugin
# cache defaults to $HOME/.cache. /tmp is a tmpfs under compose and world-
# writable in the image, so this value works even when the entrypoint cannot
# create a home of its own. The entrypoint moves HOME to /tmp/stream-archive and
# gives that directory to the app uid, so another uid cannot pre-create the
# cache path of the in-process plugin from a shared location. The cache is
# per-container, which is fine because it is a cache: the ttvlol plugin
# re-fetches on restart.
# This ENV stays after `uv sync`: at build time HOME=/root, so the build's
# caches do not pollute /tmp with root-owned dirs.
ENV HOME=/tmp

# Entrypoint adopts the data-dir owner's uid/gid (entrypoint.sh) so compose
# deployments work on hosts whose user's uid/gid is not 1000. The container
# starts as root because only root can adopt another uid, and the entrypoint
# drops to that identity before it execs the app. The app never runs as root: a
# data dir owned by root resolves uid 0, and the entrypoint then exits with the
# chown that fixes it instead of running with the image identity. setpriv comes
# from util-linux, which is essential in the slim base image.
# The dropped identity also drives the tailscale CLI. tailscaled denies a
# state-changing command (funnel, serve, up) to a uid that is neither root nor
# the configured operator, so register that uid on the host when the bot must
# manage the funnel: `tailscale set --operator=<uid>`.
COPY entrypoint.sh /usr/local/bin/stream-archive-entrypoint
RUN chmod +x /usr/local/bin/stream-archive-entrypoint
ENTRYPOINT ["stream-archive-entrypoint"]

# Plain `docker run` starts the scheduler. Overriding CMD runs other entry
# points, for example `docker compose run --rm stream-archive
# stream-archive-setup-youtube`. Both need a data dir the app can own: without
# one the entrypoint exits, so mount /data or set USER_UID.
CMD ["stream-archive"]

# Liveness for the hung-process case. The scheduler serves /healthz on the
# loopback interface (scheduler.py _start_health_server, constant
# _HEALTH_PORT). Keep 9100 in step with that constant: ci.yml compares this line
# with the constant, so a change to one without the other fails CI instead of
# drifting silently. Compose inherits this healthcheck automatically. Do not
# duplicate it in docker-compose.yml. The inner 2s timeout leaves room for the
# interpreter start and the imports inside the 5s healthcheck timeout, so a
# loaded host still reports a healthy process. The check also runs for an
# overridden CMD, so a one-shot setup run reports "unhealthy" after it exits.
# Pass --no-healthcheck to skip it.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9100/healthz', timeout=2)"]
