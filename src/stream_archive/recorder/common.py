import re


def _sanitize_filename(name: str) -> str:
    return re.sub(r"[<>:\"/\\|?*]", "_", name)[:200]
