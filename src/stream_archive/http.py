import httpx

from stream_archive.updater import _installed_app_version

# Single construction site for the shared outbound client. Callers that
# need different timeouts or headers pass their own client instead.
# The version comes from the installed package, so it never drifts
# from the release tag.
_USER_AGENT = f"stream-archive/{_installed_app_version() or 'dev'}"


def build_shared_client() -> httpx.AsyncClient:
    """Build the shared outbound HTTP client with default limits."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(10, connect=5),
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
