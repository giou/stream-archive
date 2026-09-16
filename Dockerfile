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
# install.sh goes to a file first. A plain `curl ... | sh` reports the status
# of sh, so a failed or empty download would build an image without the CLI.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates curl \
 && curl -fsSL -o /tmp/tailscale-install.sh https://tailscale.com/install.sh \
 && sh /tmp/tailscale-install.sh \
 && rm -f /tmp/tailscale-install.sh \
 && rm -rf /var/lib/apt/lists/*

COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# twitch.py plugin (2bc4/streamlink-ttvlol). The build fetches the release in
# TTVLOL_PLUGIN_VERSION. The default "latest" points at the newest release, but
# it does not change the RUN command text, so a rebuilt image keeps the cached
# layer and the old plugin copy in it. The publish workflow passes the resolved
# tag, which changes the command text and with it the layer cache key. Use
# --no-cache, or --build-arg TTVLOL_PLUGIN_VERSION=<tag>, to fetch the current
# release by hand.
# The file is fetched without a checksum on purpose (the release asset is
# mutable). The syntax check rejects a truncated file or an HTML error page.
ARG TTVLOL_PLUGIN_VERSION=latest
RUN mkdir -p /app/plugins \
 && if [ "${TTVLOL_PLUGIN_VERSION}" = "latest" ]; then \
      URL="https://github.com/2bc4/streamlink-ttvlol/releases/latest/download/twitch.py"; \
    else \
      URL="https://github.com/2bc4/streamlink-ttvlol/releases/download/${TTVLOL_PLUGIN_VERSION}/twitch.py"; \
    fi \
 && curl -fsSL "$URL" -o /app/plugins/twitch.py \
 && python -c "import ast; ast.parse(open('/app/plugins/twitch.py').read())"

# Two-stage dependency install so source edits do not invalidate the dep layer.
# Stage 1 resolves and installs third-party deps only. Build caches it until
# uv.lock or project metadata changes. Stage 2 adds the project itself from
# src/.
COPY pyproject.toml uv.lock README.md ./

# Venv lives outside /app so the read-only rootfs never blocks it.
# PTB 22 moves time periods from numbers to datetime.timedelta. The
# PTB_TIMEDELTA flag turns on that form now, so this image never reads the
# deprecated int form (telegram/_utils/datetime.get_timedelta_value).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PTB_TIMEDELTA=1
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# HOME must be writable by the (non-root) runtime user. Streamlink's plugin
# cache defaults to $HOME/.cache. /tmp is a tmpfs under compose and world-
# writable in the image, so it always works. The cache is per-container,
# which is fine because it is a cache: the ttvlol plugin re-fetches on restart.
# This ENV stays after `uv sync`: at build time HOME=/root, so the build's
# caches do not pollute /tmp with root-owned dirs.
ENV HOME=/tmp

# Entrypoint adopts the data-dir owner's uid/gid (entrypoint.sh) so compose
# deployments work on hosts whose user's uid/gid is not 1000. The container
# starts as root and immediately drops to that identity. A data dir owned by
# root resolves uid 0; the entrypoint then warns on stderr and keeps root,
# because a chown of existing data is a host decision. setpriv comes from
# util-linux, which is essential in the slim base image.
COPY entrypoint.sh /usr/local/bin/stream-archive-entrypoint
RUN chmod +x /usr/local/bin/stream-archive-entrypoint
ENTRYPOINT ["stream-archive-entrypoint"]

# Plain `docker run` starts the scheduler. Overriding CMD runs other entry
# points, for example `docker compose run --rm stream-archive
# stream-archive-setup-youtube`.
CMD ["stream-archive"]

# Liveness for the hung-process case. The scheduler serves /healthz on the
# loopback interface (scheduler.py _start_health_server, constant
# _HEALTH_PORT). Keep 9100 in step with that constant. Compose inherits this
# healthcheck automatically. Do not duplicate it in docker-compose.yml. The
# check also runs for an overridden CMD, so a one-shot setup run reports
# "unhealthy" after it exits. Pass --no-healthcheck to skip it.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9100/healthz', timeout=4)"]
