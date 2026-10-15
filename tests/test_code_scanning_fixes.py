"""Regression tests for the CodeQL findings fixed on the inherited code."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import franco.dashboard as dash


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(dash, "DB_PATH", Path(tmp) / "franco.db")
        with TestClient(dash.app, base_url="http://localhost") as c:
            yield c


# py/path-injection: only files that exist under web/ are served.

def test_static_serves_real_assets(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert client.get("/static/fonts/geist.woff2").status_code == 200


@pytest.mark.parametrize("path", [
    "../dashboard.py", "..%2Fdashboard.py", "../../pyproject.toml",
    "%2e%2e/%2e%2e/LICENSE", "index.html", "nope.js", "/etc/passwd",
])
def test_static_refuses_anything_else(client, path):
    r = client.get(f"/static/{path}")
    assert r.status_code == 404
    assert "def " not in r.text and "[project]" not in r.text


# py/stack-trace-exposure: errors are logged, not returned.

def test_errors_do_not_leak_exception_text(client, monkeypatch):
    from franco.state import StateManager

    async def boom(self, *a, **k):
        raise RuntimeError("secret internal detail /Users/someone/data.db")

    monkeypatch.setattr(StateManager, "approve_outbox", boom)
    r = client.post("/api/outbox/approve-all")
    assert r.status_code == 500
    assert "secret internal detail" not in r.text
    assert "see the dashboard log" in r.json()["message"]


# py/bad-tag-filter: closing tags with whitespace or junk are still removed.

def test_script_and_style_stripped_with_odd_end_tags():
    from franco.collectors.profile import _strip_code
    html = ("<p>Jane Doe, CEO</p><script>var ceo='return value instan'</script >"
            "<style>.x{}</style\n><script type=x>evil()</script foo>")
    out = _strip_code(html)
    assert "return value" not in out and "evil" not in out and ".x{}" not in out
    assert "Jane Doe, CEO" in out


# py/log-injection: one event, one log line.

def test_discovery_log_values_cannot_add_lines():
    from franco.collectors.discover import _log_safe
    assert "\n" not in _log_safe("plumbers\nINFO forged entry")
    assert "\r" not in _log_safe(ValueError("a\r\nb"))


# py/clear-text-logging-sensitive-data: hidden input has its own path.

def test_setup_secrets_use_hidden_input_only(monkeypatch):
    import franco.setup as setup
    asked = []
    monkeypatch.setattr(setup.getpass, "getpass", lambda p: asked.append(p) or "s3cret")
    monkeypatch.setattr("builtins.input", lambda p: pytest.fail("secret read with input()"))
    assert setup._ask_secret("API key") == "s3cret"
    assert asked and "API key" in asked[0]


# py/stack-trace-exposure via run reports: the dashboard shows fixed wording.

def test_discovery_reports_never_carry_exception_text():
    import httpx
    from franco.collectors.discover import describe_error
    req = httpx.Request("GET", "https://api.example/x?api_key=SECRET")
    cases = [
        httpx.ReadTimeout("slow", request=req),
        httpx.HTTPStatusError("401 for https://api.example/x?api_key=SECRET",
                              request=req, response=httpx.Response(401, request=req)),
        httpx.ConnectError("refused api_key=SECRET", request=req),
        RuntimeError("/Users/someone/franco/data/franco.db is locked"),
    ]
    for e in cases:
        text = describe_error(e)
        assert "SECRET" not in text and "/Users" not in text and "log" in text
