import httpx

from stream_archive.updater import installed_app_version

# Defaults for every outbound client that the app builds. A caller that
# needs other timeouts or headers builds its own client instead.
# The version comes from the installed package, so it never drifts
# from the release tag.
_USER_AGENT = f"stream-archive/{installed_app_version() or 'dev'}"


def build_http_client() -> httpx.AsyncClient:
    """Build an outbound HTTP client with the project defaults.

    The function returns a new client on every call. The caller owns that
    client and must close it with ``aclose()``. The function keeps no
    reference, so a caller can close it at any time.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(10, connect=5),
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
