import threading

import httpx
import pytest
from conftest import make_config

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


class _FakeFlow:
    """InstalledAppFlow double: no browser, no network, one recorded exchange."""

    class _Credentials:
        @staticmethod
        def to_json() -> str:
            return '{"token": "stub"}'

    def __init__(self, state="expected-state"):
        self.state = state
        self.redirect_uri = ""
        self.exchanged: list[str] = []
        self.credentials = self._Credentials()

    def authorization_url(self, **_kwargs):
        return f"https://accounts.google.com/o/oauth2/auth?state={self.state}", self.state

    def fetch_token(self, code=None, **_kwargs):
        self.exchanged.append(code)


def _run_main(monkeypatch, tmp_path, pastes, expect_exit=True, callback_code=None):
    """Drive main() with a stubbed flow and a scripted paste sequence.

    With ``callback_code``, the loopback callback is treated as having stored
    that code before the operator answered, which is the documented flow.
    """
    import stream_archive.setup_youtube as module

    config = make_config(youtube={"client_secrets_file": str(tmp_path / "client_secret.json")})
    config._workdir = tmp_path
    config._config_path = tmp_path / "config.json"
    (tmp_path / "client_secret.json").write_text("{}")
    monkeypatch.setattr(module, "get_config", lambda: config)
    flow = _FakeFlow()
    monkeypatch.setattr(module.InstalledAppFlow, "from_client_secrets_file", lambda *a, **k: flow)
    monkeypatch.setattr(module.webbrowser, "open", lambda *a, **k: None)
    answers = iter(pastes)
    created: dict = {}
    real_server = module.HTTPServer

    class Recording(real_server):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created["server"] = self

    monkeypatch.setattr(module, "HTTPServer", Recording)

    def answer(*_a, **_k):
        if callback_code is not None:
            created["server"].auth_code = callback_code
            created["server"].auth_event.set()
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    try:
        if expect_exit:
            with pytest.raises(SystemExit):
                module.main()
        else:
            module.main()  # a success path: the tool returns normally
    finally:
        # main() only closes the callback server on its success path, and the
        # harness reuses the real HTTPServer, so release it here on every path.
        server = created.get("server")
        if server is not None:
            server.shutdown()
            server.server_close()
    return flow


def test_main_refuses_a_state_less_paste(monkeypatch, tmp_path):
    """A pasted code with no state cannot be bound to this authorization.

    The state is the only thing tying a code to the request the tool printed,
    and the pasted path used to exchange a state-less paste, outranking the
    code that had passed the callback's own state check.
    """
    flow = _run_main(
        monkeypatch,
        tmp_path,
        ["https://attacker.example/oauth2?code=4%2F0ATTACKER", "not a url", "still not a url"],
    )
    assert flow.exchanged == []


def test_main_exchanges_a_paste_with_the_matching_state(monkeypatch, tmp_path):
    """The documented paste path still works when the state matches."""
    flow = _run_main(
        monkeypatch,
        tmp_path,
        ["https://127.0.0.1/?code=4%2F0GOOD&state=expected-state"],
        expect_exit=False,
    )
    assert flow.exchanged == ["4/0GOOD"]
    assert (tmp_path / "youtube_token.json").exists()


def test_main_accepts_the_browser_callback_after_an_empty_enter(monkeypatch, tmp_path):
    """The documented automatic flow must survive the state guard.

    The prompt says to press Enter once the browser shows the success page.
    An empty line is not a paste: the code comes from the callback, which
    checked the state itself. Requiring a state for it broke the main path.
    """
    flow = _run_main(monkeypatch, tmp_path, [""], expect_exit=False, callback_code="4/0FROMCALLBACK")
    assert flow.exchanged == ["4/0FROMCALLBACK"]
    assert (tmp_path / "youtube_token.json").exists()
