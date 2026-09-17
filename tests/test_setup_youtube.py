import threading

import httpx
import pytest

from stream_archive.setup_youtube import _CallbackHandler, extract_code, extract_code_and_state


def _start_server():
    from http.server import HTTPServer

    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.auth_code = None
    # main() waits on this event, so the handler sets it as soon as it accepts
    # a code.
    server.auth_event = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop_server(server):
    server.shutdown()
    server.server_close()


def test_extract_code_passthrough_bare_code():
    assert extract_code("4/0AX4XfGc...") == "4/0AX4XfGc..."


def test_extract_code_from_full_redirect_url():
    # Google percent-encodes the slash in the code, as a real redirect does.
    url = "http://localhost:53421/?code=4%2F0AX4XfGc&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fyoutube"
    assert extract_code(url) == "4/0AX4XfGc"


def test_extract_code_rejects_error_url():
    with pytest.raises(ValueError, match="access_denied"):
        extract_code("http://localhost:53421/?error=access_denied")


def test_extract_code_returns_empty_string_for_blank_code():
    # parse_qs drops blank values. main() then falls back to server.auth_code.
    assert extract_code("http://localhost:53421/?code=") == ""


def test_extract_code_returns_url_when_code_is_absent():
    # The text holds no code= or error= substring, so it stays untouched.
    url = "http://localhost:53421/?state=xyz"
    assert extract_code(url) == url


def test_extract_code_and_state_returns_both_values():
    code, state = extract_code_and_state("http://localhost:53421/?code=4%2Fabc&state=xyz")
    assert (code, state) == ("4/abc", "xyz")


def test_extract_code_and_state_returns_no_state_for_a_bare_code():
    assert extract_code_and_state("4/0AX4XfGc") == ("4/0AX4XfGc", None)


def test_callback_captures_code_and_renders_success_page():
    server = _start_server()
    try:
        resp = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/?code=abc123")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert b"Authorization successful" in resp.content
        assert server.auth_code == "abc123"
        assert server.auth_event.is_set()
    finally:
        _stop_server(server)


@pytest.mark.parametrize("query", ["code=abc123&state=other", "code=abc123"])
def test_callback_rejects_bad_state(query):
    """A missing or mismatched state answers 400 and stores no code."""
    server = _start_server()
    server.auth_state = "expected"
    try:
        resp = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/?{query}")
        assert resp.status_code == 400
        assert b"Authorization failed" in resp.content
        assert server.auth_code is None
        assert not server.auth_event.is_set()
    finally:
        _stop_server(server)


def test_callback_accepts_matching_state():
    """A callback that carries the expected state stores the code."""
    server = _start_server()
    server.auth_state = "expected"
    try:
        resp = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/?code=abc123&state=expected")
        assert resp.status_code == 200
        assert server.auth_code == "abc123"
        assert server.auth_event.is_set()
    finally:
        _stop_server(server)


def test_callback_rejects_error_redirect():
    """An error redirect renders the failure page and stores no code."""
    server = _start_server()
    try:
        resp = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/?error=access_denied")
        assert resp.status_code == 400
        assert b"Authorization failed" in resp.content
        assert server.auth_code is None
        assert not server.auth_event.is_set()
    finally:
        _stop_server(server)


def test_callback_rejects_request_without_code():
    server = _start_server()
    try:
        resp = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/")
        assert resp.status_code == 400
        assert b"Authorization failed" in resp.content
        assert server.auth_code is None
        assert not server.auth_event.is_set()
    finally:
        _stop_server(server)
