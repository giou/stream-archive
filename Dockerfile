FROM python:3.12-slim

# Runtime deps. Chromium is NOT needed: the recorder plays live streams through
# the ttvlol playlist proxies (config proxy_list -> plugin option proxy-playlist),
# which handle client-integrity server-side. plugins/twitch.py does contain a
# browser (CDP) fallback for token acquisition, but this app never triggers it:
# the recorder sets neither proxy-playlist-exclude nor proxy-playlist-fallback.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg git tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Pinned to the same uv version the project was developed with (host: 0.11.5).
COPY --from=ghcr.io/astral-sh/uv:0.11.5 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so source edits don't invalidate the layer.
COPY pyproject.toml uv.lock ./
COPY main.py setup_youtube.py ./
COPY src ./src
COPY plugins ./plugins

# Venv lives OUTSIDE /app so the compose `.:/app` bind mount never shadows it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1
RUN uv sync --frozen --no-dev --no-install-project

# ENTRYPOINT/CMD split: plain `docker run` starts the scheduler; overriding CMD
# (e.g. `docker compose run stream-archive setup_youtube.py`) runs other scripts.
ENTRYPOINT ["python"]
CMD ["/app/main.py"]
