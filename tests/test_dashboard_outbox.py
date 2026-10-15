"""Dashboard API for the Outbox desk (context, editing), campaign preview
sign-off, and bring-your-own CSV lists."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import franco.config as franco_config
import franco.dashboard as dash
from franco.importer import TEMPLATES
from franco.state import StateManager
from tests.phase1_helpers import account_row, import_accounts, make_config, make_writer

CONFIG = make_config()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(franco_config, "load_config", lambda *a, **k: CONFIG)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "franco.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        with TestClient(dash.app, base_url="http://localhost") as c:
            c.state = StateManager(str(db))
            asyncio.run(c.state.init_db())
            yield c


def run(coro):
    return asyncio.run(coro)


def stage_one(c):
    async def go():
        await import_accounts(c.state, [account_row(1)])
        await make_writer(c.state, CONFIG).run()
        return await c.state.get_outbox(limit=50)
    return run(go())


# ── Outbox desk ──


def test_pending_items_carry_context_and_preview_flag(client):
    stage_one(client)
    data = client.get("/api/outbox").json()
    assert len(data["pending"]) == 3
    item = data["pending"][0]
    assert item["preview"] is True
    ctx = item["context"]
    assert ctx["campaign"] == "M1" and ctx["owner"] == "Hemin"
    assert ctx["observation"].startswith("your seller") and ctx["source_url"] == "https://co1.com/help"
    [preview] = data["previews"]
    assert preview["key"] == "M1" and preview["signed_off"] is False
    assert preview["staged"] == {"Hemin": 1} and preview["preview_size"] == 10

    run(client.state.set_signoff("M1", "Hemin"))
    data = client.get("/api/outbox").json()
    assert data["pending"][0]["preview"] is False
    assert data["previews"][0]["signed_off"] is True


def test_edit_pending_draft(client):
    item = stage_one(client)[0]
    r = client.post(f"/api/outbox/{item['id']}/edit", json={"subject": "New", "body": "Changed body"})
    assert r.status_code == 200 and r.json()["success"]
    saved = run(client.state.get_outbox_item(item["id"]))
    assert saved["subject"] == "New" and saved["body"] == "Changed body"


def test_edit_rejects_empty_and_non_pending(client):
    item = stage_one(client)[0]
    assert client.post(f"/api/outbox/{item['id']}/edit",
                       json={"subject": " ", "body": "x"}).status_code == 400
    run(client.state.approve_outbox(item["id"]))
    assert client.post(f"/api/outbox/{item['id']}/edit",
                       json={"subject": "s", "body": "b"}).status_code == 409
    assert client.post("/api/outbox/nope/edit",
                       json={"subject": "s", "body": "b"}).status_code == 409


# ── preview sign-off ──


def test_signoff_requires_a_name(client):
    assert client.post("/api/preview/M1/signoff", json={}).status_code == 400
    assert run(client.state.get_signoff("M1")) is None
    r = client.post("/api/preview/M1/signoff", json={"by": "Hemin"})
    assert r.status_code == 200
    assert run(client.state.get_signoff("M1"))["by"] == "Hemin"
    assert client.post("/api/preview/M1/reopen").status_code == 200
    assert run(client.state.get_signoff("M1")) is None


def test_signoff_unknown_campaign_and_action(client):
    assert client.post("/api/preview/ZZ/signoff", json={"by": "x"}).status_code == 404
    assert client.post("/api/preview/M1/approve", json={"by": "x"}).status_code == 400


def test_preview_csv_download(client):
    stage_one(client)
    r = client.get("/api/preview.csv?campaign=M1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("campaign,owner,company")
    assert "Co1" in r.text and "https://co1.com/help" in r.text


# ── CSV import ──


def test_import_dry_run_then_real(client):
    csv_text = TEMPLATES["contacts"]
    r = client.post("/api/import", json={"kind": "contacts", "csv": csv_text, "dry_run": True})
    body = r.json()
    assert body["success"] and body["dry_run"] and len(body["imported"]) == 2
    assert run(client.state.get_company_by_domain("another.example")) is None

    r = client.post("/api/import", json={"kind": "contacts", "csv": csv_text, "dry_run": False,
                                         "owner": "Hemin"})
    assert r.json()["success"] and not r.json()["dry_run"]
    company = run(client.state.get_company_by_domain("another.example"))
    assert company is not None and company.owner == "Hemin"


def test_import_defaults_to_dry_run_and_reports_rejections(client):
    r = client.post("/api/import", json={"kind": "accounts",
                                         "csv": "company,domain\nCo,co.com\n"}).json()
    assert r["success"] and r["dry_run"] is True and r["imported"] == []
    assert r["rejected"][0]["row"] == 2 and "missing" in r["rejected"][0]["why"]


def test_import_suppressions(client):
    r = client.post("/api/import", json={"kind": "suppressions", "dry_run": False,
                                         "csv": "a@b.com\nc.org\n"}).json()
    assert r["imported"] == ["a@b.com", "c.org"]
    csv_out = client.get("/api/suppressions.csv")
    assert csv_out.status_code == 200
    assert csv_out.text.splitlines()[0] == "value,kind,reason,source,created_at"
    assert "c.org,domain" in csv_out.text


def test_import_limits(client):
    big = "x" * (dash.MAX_IMPORT_BYTES + 1)
    assert client.post("/api/import", json={"kind": "contacts", "csv": big}).status_code == 413
    assert client.post("/api/import", json={"kind": "people", "csv": "a"}).status_code == 400


def test_import_templates(client):
    for kind, text in TEMPLATES.items():
        r = client.get(f"/api/import/template/{kind}")
        assert r.status_code == 200 and r.text == text
        assert f'filename="{kind}.csv"' in r.headers["content-disposition"]
    assert client.get("/api/import/template/people").status_code == 404
