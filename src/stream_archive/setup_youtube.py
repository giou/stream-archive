import contextlib
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow

from stream_archive.config import get_config

SCOPES = ["https://www.googleapis.com/auth/youtube"]


class _CallbackHandler(BaseHTTPRequestHandler):
    """Serves the OAuth redirect and stores the code on server.auth_code."""

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        if query.get("code"):
            self.server.auth_code = query["code"][0]  # type: ignore[attr-defined]
            body = (
                b"<html><body><h2>Authorization successful!</h2>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )
            self.send_response(200)
        else:
            body = (
                b"<html><body><h2>Authorization failed</h2>"
                b"<p>No code was received. Close this tab and try again.</p></body></html>"
            )
            self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass  # keep the OAuth prompt clean


def extract_code(text: str) -> str:
    """Return the code from a pasted redirect URL, or the text itself if it is already a code."""
    if "code=" not in text and "error=" not in text:
        return text
    query = parse_qs(urlparse(text).query)
    if "error" in query:
        raise ValueError(f"Authorization failed: {query['error'][0]}")
    codes = query.get("code", [])
    return codes[0] if codes else ""


def main() -> None:
    try:
        config = get_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    secrets_file = config.youtube.client_secrets_file
    secrets_path = Path(secrets_file)
    if not secrets_path.is_absolute():
        secrets_path = config._workdir / secrets_path
    if not secrets_path.exists():
        print(f"ERROR: {secrets_file} not found", file=sys.stderr)
        sys.exit(1)

    print("Starting YouTube OAuth setup...")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)

    # When the browser can reach localhost, the callback server captures
    # the code automatically. Otherwise the user pastes the redirect URL.
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)  # port 0 -> free port
    server.auth_code = None  # type: ignore[attr-defined]
    flow.redirect_uri = f"http://localhost:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print("1. Open this URL in your browser (trying to open it automatically):")
    print(f"   {auth_url}")
    with contextlib.suppress(Exception):
        webbrowser.open(auth_url)  # headless/SSH: the printed URL is the fallback
    print()
    print("2. Authorize the app. Google redirects you to a local page.")
    print("   - If it shows 'Authorization successful!', return here and press Enter.")
    print("   - If the page fails to load (SSH/Docker/headless), copy the FULL")
    print("     URL from the address bar and paste it below.")
    print()

    for _ in range(3):
        pasted = input("   Press Enter after authorizing, or paste the redirect URL: ").strip()
        try:
            candidate = server.auth_code or extract_code(pasted)  # type: ignore[attr-defined]
        except ValueError as exc:
            print(f"   {exc}")
            continue
        if not candidate:
            print("   No code found — wait for the success page, or paste the full redirect URL.")
            continue
        try:
            flow.fetch_token(code=candidate)
            break
        except Exception as exc:
            print(f"   Could not exchange the code ({exc}); paste the full URL from the address bar.")
    else:
        print("ERROR: no valid token after 3 attempts. Re-run the script.", file=sys.stderr)
        sys.exit(1)

    server.shutdown()
    server.server_close()

    token_path = config._workdir / "youtube_token.json"
    data = json.loads(flow.credentials.to_json())
    with os.fdopen(os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)

    print()
    print(f"Token saved to {token_path}")
    print("YouTube authentication complete.")
    print()
    print("Tip: if the Google Cloud OAuth consent screen is still 'Testing' (Audience tab ->")
    print("Publishing status), publish the app to 'In production', or the token will stop")
    print("refreshing after 7 days.")


if __name__ == "__main__":
    main()
