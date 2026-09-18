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
    """Serves the OAuth redirect and stores the code on server.auth_code.

    The handler accepts a code only when its ``state`` matches the value on
    ``server.auth_state``. A caller that does not set the state accepts any
    state, so a bare test server keeps working.
    """

    def _state_accepted(self, query: dict[str, list[str]]) -> bool:
        """True when the callback carries the expected OAuth ``state``."""
        expected: str | None = getattr(self.server, "auth_state", None)
        if expected is None:
            return True
        return (query.get("state") or [""])[0] == expected

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        if query.get("code") and self._state_accepted(query):
            self.server.auth_code = query["code"][0]  # type: ignore[attr-defined]
            event = getattr(self.server, "auth_event", None)
            if event is not None:
                event.set()
            body = (
                b"<html><body><h2>Authorization successful!</h2>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )
            self.send_response(200)
        else:
            body = (
                b"<html><body><h2>Authorization failed</h2>"
                b"<p>No valid code was received. Close this tab and try again.</p></body></html>"
            )
            self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass  # keep the OAuth prompt clean


def extract_code_and_state(text: str) -> tuple[str, str | None]:
    """Return the OAuth code and state of a pasted redirect URL.

    A bare code gives ``(text, None)``, because it carries no state.
    """
    if "code=" not in text and "error=" not in text:
        return text, None
    query = parse_qs(urlparse(text).query)
    if "error" in query:
        msg = f"Authorization failed: {query['error'][0]}"
        raise ValueError(msg)
    codes = query.get("code", [])
    states = query.get("state", [])
    return (codes[0] if codes else ""), (states[0] if states else None)


def extract_code(text: str) -> str:
    """Return the code from a pasted redirect URL, or the text itself if it is already a code."""
    return extract_code_and_state(text)[0]


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

    # When the browser can reach the loopback address, the callback server
    # captures the code automatically. Otherwise the user pastes the
    # redirect URL. The bind address and the redirect URI must match, so
    # both use 127.0.0.1.
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)  # port 0 -> free port
    server.auth_code = None  # type: ignore[attr-defined]
    server.auth_state = None  # type: ignore[attr-defined]
    server.auth_event = threading.Event()  # type: ignore[attr-defined]
    flow.redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"

    auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
    server.auth_state = state  # type: ignore[attr-defined]
    # Serve only now. The handler accepts any state while auth_state is None,
    # so a request in the window before this line would bypass the check.
    threading.Thread(target=server.serve_forever, daemon=True).start()
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
            candidate, pasted_state = extract_code_and_state(pasted)
        except ValueError as exc:
            print(f"   {exc}")
            continue
        # A pasted URL carries the state of its own authorization request.
        # Require it: the state is the only thing binding the code to the
        # authorization request this run printed, and a paste without one
        # could be text the operator did not generate. An empty line is not a
        # paste: it means "the browser finished", and the code then comes from
        # the callback, which checked the state itself.
        if pasted and pasted_state is None:
            print(
                "   That text carries no state, so it cannot be matched to this authorization.\n"
                "   Paste the FULL redirect URL from the browser's address bar."
            )
            continue
        if pasted and pasted_state != server.auth_state:  # type: ignore[attr-defined]
            print("   That redirect URL belongs to an earlier attempt. Paste the URL of the page you just opened.")
            continue
        if not candidate:
            # The browser callback can land just after the user pressed Enter.
            server.auth_event.wait(timeout=1.0)  # type: ignore[attr-defined]
            candidate = server.auth_code  # type: ignore[attr-defined]
        if not candidate:
            print("   No code found — wait for the success page, or paste the full redirect URL.")
            continue
        try:
            flow.fetch_token(code=candidate)
            break
        except Exception as exc:
            detail = str(exc)
            if "invalid_client" in detail or "unauthorized_client" in detail:
                print(
                    f"ERROR: Google rejected the OAuth client ({detail}).\n"
                    "Check client_secret.json and create a desktop OAuth client again.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if "invalid_grant" not in detail:
                # A network or server problem is not a stale code. Keep the
                # code and the callback event for the next attempt.
                print(f"   The token exchange failed ({exc}). Press Enter to try again.")
                continue
            # Google refused the code: it is stale, used, or belongs to an
            # older attempt. Drop it, but only while it is still the code of
            # this attempt. A newer callback can have stored a fresh one.
            if server.auth_code == candidate:  # type: ignore[attr-defined]
                server.auth_code = None  # type: ignore[attr-defined]
                # Clear the event too, so the one-second grace below applies
                # to the retry that follows this failed exchange.
                server.auth_event.clear()  # type: ignore[attr-defined]
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
