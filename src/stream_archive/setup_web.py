"""Set the browser panel password.

Writes only a PBKDF2 hash to ``config.json``. The password itself never
reaches disk or logs. Run from the data dir (Docker: ``docker compose
exec stream-archive stream-archive-setup-web``).
"""

from __future__ import annotations

import getpass
import logging
import secrets
import sys
from pathlib import Path

from stream_archive.config import AppConfig, apply_config_change, get_config
from stream_archive.webui import MIN_PASSWORD_LEN, hash_password

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> None:
    """Console entry point for ``stream-archive-setup-web``."""
    try:
        config = get_config()
    except (ValueError, FileNotFoundError) as e:
        print(f"Cannot read config.json: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    first = getpass.getpass("New panel password (12+ characters): ")
    if len(first) < MIN_PASSWORD_LEN:
        print(f"Password must hold at least {MIN_PASSWORD_LEN} characters.", file=sys.stderr)
        raise SystemExit(1)
    second = getpass.getpass("Repeat the password: ")
    if first != second:
        print("Passwords differ.", file=sys.stderr)
        raise SystemExit(1)
    hashed = hash_password(first)

    def mutate(candidate: AppConfig) -> None:
        candidate.web.password_hash = hashed
        candidate.web.enabled = True
        if not candidate.web.session_secret.strip():
            candidate.web.session_secret = secrets.token_urlsafe(32)

    try:
        apply_config_change(config, mutate)
    except ValueError as e:
        print(f"Cannot write config.json: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    workdir = Path(config.config_path).parent
    print(f"Panel password set in {workdir / 'config.json'}.")
    print("Open the panel at <endpoint.public_url>/ (enable the endpoint first).")
