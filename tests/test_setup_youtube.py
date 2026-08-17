import threading

import httpx
import pytest

from stream_archive.setup_youtube import _CallbackHandler, extract_code


def _start_server():
    from http.server import HTTPServer

    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.auth_code = None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop_server(server):
    server.shutdown()
    server.server_close()


def test_extract_code_passthrough_bare_code():
    assert extract_code("4/0AX4XfGc...") == "4/0AX4XfGc..."


def test_extract_code_from_full_redirect_url():
    url = "http://localhost:53421/?code=4/0AX4XfGc&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fyoutube"
    assert extract_code(url) == "4/0AX4XfGc"


def test_extract_code_rejects_error_url():
    with pytest.raises(ValueError, match="access_denied"):
        extract_code("http://localhost:53421/?error=access_denied")


def test_callback_captures_code_and_renders_success_page():
    server = _start_server()
    try:
        resp = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/?code=abc123")
        assert resp.status_code == 200
        assert b"Authorization successful" in resp.content
        assert server.auth_code == "abc123"
    finally:
        _stop_server(server)


def test_callback_rejects_request_without_code():
    server = _start_server()
    try:
        resp = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/")
        assert resp.status_code == 400
        assert server.auth_code is None
    finally:
        _stop_server(server)
