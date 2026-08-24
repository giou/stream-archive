FROM python:3.14-slim

# Runtime deps. Chromium is NOT needed: the recorder plays live streams through
# the ttvlol playlist proxies (config proxy_list -> plugin option proxy-playlist),
# which handle client-integrity server-side. plugins/twitch.py does contain a
# browser (CDP) fallback for token acquisition, but this app never triggers it:
# the recorder sets neither proxy-playlist-exclude nor proxy-playlist-fallback.
#
# Tailscale CLI: the Telegram bot enables `tailscale funnel` for the kick
# webhook tunnel by talking to the HOST's tailscaled through the socket that
# docker-compose mounts at /var/run/tailscale/tailscaled.sock. Installed from
# the official pkgs.tailscale.com repo via install.sh — the Alpine base was
# considered but rejected because Tailscale publishes no official Alpine
# packages (install.sh falls back to the community-maintained apk there).
# cloudflared: the bot runs the cloudflare quick/named tunnel itself.
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg git tzdata ca-certificates curl \
 && curl -fsSL https://tailscale.com/install.sh | sh \
 && case "$TARGETARCH" in amd64|arm64|arm) cf_arch="$TARGETARCH";; *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1;; esac \
 && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/2026.8.2/cloudflared-linux-${cf_arch}" -o /usr/local/bin/cloudflared \
 && chmod +x /usr/local/bin/cloudflared \
 && rm -rf /var/lib/apt/lists/*

# Pinned uv version used to build the image; bump deliberately with releases.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app

# twitch.py plugin (2bc4/streamlink-ttvlol), pinned release + sha256 from the
# release API so image builds are reproducible. Bump both when vendoring a new
# plugin release; the bot's update check reports when upstream is ahead.
ARG TTVLOL_PLUGIN_VERSION=8.3.0-20260701
ARG TTVLOL_PLUGIN_SHA256=4d465380159ec59f7caef6cb6a28368bbbbd3abcf80886138182184c30f2fad0
RUN mkdir -p /app/plugins \
 && curl -fsSL "https://github.com/2bc4/streamlink-ttvlol/releases/download/${TTVLOL_PLUGIN_VERSION}/twitch.py" -o /tmp/twitch.py \
 && echo "${TTVLOL_PLUGIN_SHA256}  /tmp/twitch.py" | sha256sum -c - \
 && mv /tmp/twitch.py /app/plugins/twitch.py

# Two-stage dependency install so source edits don't invalidate the dep layer:
# stage 1 resolves+installs third-party deps only (cached until uv.lock or
# project metadata changes); stage 2 adds the project itself from src/.
COPY pyproject.toml uv.lock README.md ./

# Venv lives OUTSIDE /app so the read-only rootfs never blocks it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# HOME must be writable by the (non-root) runtime user: streamlink's plugin
# cache defaults to $HOME/.cache. /tmp is a tmpfs under compose and world-
# writable in the image, so it always works; the cache is per-container,
# which is fine (it is a cache — the ttvlol plugin re-fetches on restart).
# Kept AFTER uv sync: at build time HOME=/root so the build's caches don't
# pollute /tmp with root-owned dirs.
ENV HOME=/tmp

# Entrypoint adopts the data-dir owner's uid/gid (entrypoint.sh) so compose
# deployments work on hosts whose user's uid/gid isn't 1000: the container
# starts as root and immediately drops to that identity. setpriv is part of
# util-linux, which is essential in the slim base image.
COPY entrypoint.sh /usr/local/bin/stream-archive-entrypoint
RUN chmod +x /usr/local/bin/stream-archive-entrypoint
ENTRYPOINT ["stream-archive-entrypoint"]

# Plain `docker run` starts the scheduler; overriding CMD (e.g. `docker compose
# run --rm stream-archive stream-archive-setup-youtube`) runs other entry points.
CMD ["stream-archive"]

# Liveness for the hung-process case: the scheduler serves /healthz on the
# loopback interface (scheduler.py _start_health_server). Compose inherits
# this automatically; do not duplicate it in docker-compose.yml.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9100/healthz', timeout=4)"]
