"""Process degradation registry: what liveness cannot see.

``/healthz`` answers whether the process runs. This registry answers
whether it works: a full disk, dead app credentials, or a dead YouTube
token each set a key with a human reason. The monitor and the recorder
set and clear keys as sweeps prove or disprove them. ``/readyz``,
``/api/v1/status``, the panel Status tab, and the bot ``/status``
all read here, so every surface names the same problems.
"""

from __future__ import annotations

#: Degradation key -> human reason. Keys are stable identifiers:
#: "disk_full", "twitch_auth", "kick_auth", "youtube_auth".
_DEGRADED: dict[str, str] = {}


def set_degraded(key: str, reason: str) -> None:
    """Mark one problem as present. A repeat call refreshes the reason."""
    _DEGRADED[key] = reason


def clear_degraded(key: str) -> None:
    """Mark one problem as gone. Unknown keys stay unknown."""
    _DEGRADED.pop(key, None)


def degraded() -> dict[str, str]:
    """Copy of the present problems, keyed by stable identifier."""
    return dict(_DEGRADED)
