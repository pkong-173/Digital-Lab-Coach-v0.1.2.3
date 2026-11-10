import io
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlE"
       "QVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PNG_BODY = PNG.split(",", 1)[1].encode()
ADM = {"X-DLC-Admin-Token": "adm"}
REC = {"X-DLC-Consent-Token": "names"}
RECORD_PATHS = ("/admin/consent_records.csv", "/admin/signatures.zip",
                "/admin/consents.html")
USAGE_PATHS = ("/admin/summary", "/admin/daily", "/admin/events",
               "/admin/llm_texts", "/admin/stats", "/admin/research",
               "/admin/export.csv")


@pytest.fixture()
def penv(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_PROXY_DB", str(tmp_path / "proxy.db"))
    monkeypatch.delenv("DLC_CONSENT_DB", raising=False)
    monkeypatch.setenv("DLC_COURSE_TOKEN", "tok")
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    monkeypatch.setenv("DLC_CONSENT_ADMIN_TOKEN", "names")
    monkeypatch.setenv("DLC_STUDY_ID", "26-2770")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("DLC_PROXY_URL", raising=False)
    monkeypatch.delenv("DLC_GLOBAL_DAILY_CALLS", raising=False)
    monkeypatch.delenv("DLC_GLOBAL_DAILY_USD", raising=False)
    return tmp_path


def _client(monkeypatch):
    """The real proxy app; only the model call is replaced. Its answer
    repeats a tag from the prompt, so each machine's answer is recognisable."""
    from proxy import dlc_proxy
    import dlc.llm.client as real

    def upstream(prompt, **kw):
        tag = prompt.rsplit(" ", 1)[-1]
        return {"ok": True, "text": f"MODEL-ANSWER-{tag}", "error": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "model": kw.get("model")}
    monkeypatch.setattr(real, "call_llm", upstream)
    return TestClient(dlc_proxy.app, headers={"X-DLC-Token": "tok"})


def _decide(pc, iid, decision, name="", sig=None, version="v1", at=1_700_000_000.0):
    return pc.post("/v1/consent", json={
        "install_id": iid, "study_id": "26-2770", "version": version,
        "decision": decision, "name": name, "signature": sig,
        "app_version": "0.1.2.3", "decided_at": at}).json()


def _ask(pc, iid, tag, feature="modeA"):
    return pc.post("/v1/llm", json={
        "install_id": iid, "feature": feature, "model": "claude-sonnet-4-6",
        "prompt": f"SECRET-PROMPT-TEXT {tag}"}).json()


def _rows(path, sql, args=()):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _columns(path, table):
    return [r[1] for r in _rows(path, f"PRAGMA table_info({table})")]

def test_names_and_signatures_live_only_in_the_linkage_file(penv, monkeypatch):
    pc = _client(monkeypatch)
    assert _decide(pc, "m-ada", "agreed", name="Ada Lovelace", sig=PNG)["stored"] == 1
    assert _decide(pc, "m-bob", "declined")["stored"] == 1
    main, link = penv / "proxy.db", penv / "dlc_consent.db"
    assert link.exists()

    cols = _columns(main, "consents")
    assert "name" not in cols and "signature" not in cols
    assert _rows(main, "SELECT install_id, decision, has_signature FROM consents"
                       " ORDER BY id") == [("m-ada", "agreed", 1), ("m-bob", "declined", 0)]
    raw = main.read_bytes()
    assert b"Ada Lovelace" not in raw and PNG_BODY not in raw

    # the linkage file: the record itself
    assert _rows(link, "SELECT install_id, decision, name, signature FROM"
                       " consent_records ORDER BY id") == [
        ("m-ada", "agreed", "Ada Lovelace", PNG), ("m-bob", "declined", "", None)]
    assert "consent_records" not in [r[0] for r in _rows(
        main, "SELECT name FROM sqlite_master WHERE type = 'table'")]

    h = pc.get("/v1/health").json()
    assert h["consent_db_path"] != h["db_path"] and h["consent_token_set"] is True


def test_consent_records_open_with_their_own_password_only(penv, monkeypatch):
    pc = _client(monkeypatch)
    _decide(pc, "m-ada", "agreed", name="Ada Lovelace", sig=PNG)
    for path in RECORD_PATHS:
        assert pc.get(path).status_code == 401
        assert pc.get(path, headers=ADM).status_code == 401
        assert pc.get(path, headers={"X-DLC-Consent-Token": "adm"}).status_code == 401
        assert pc.get(path, headers=REC).status_code == 200
    # and that password does not open the usage data
    for path in USAGE_PATHS:
        assert pc.get(path, headers=REC).status_code == 401
        assert pc.get(path, headers={"X-DLC-Admin-Token": "names"}).status_code == 401
        assert pc.get(path, headers=ADM).status_code == 200

    csv = pc.get("/admin/consent_records.csv", headers=REC).text
    assert "Ada Lovelace" in csv and "sig_1.png" in csv and "base64" not in csv
    z = zipfile.ZipFile(io.BytesIO(pc.get("/admin/signatures.zip", headers=REC).content))
    assert sorted(z.namelist()) == ["index.csv", "sig_1.png"]
    assert "Ada Lovelace" in pc.get("/admin/consents.html", headers=REC).text
    # nothing with a name comes out under the admin token
    for table in ("consents", "events", "machines", "llm_calls", "surveys"):
        r = pc.get("/admin/export.csv", headers=ADM, params={"table": table})
        assert r.status_code == 200 and "Ada Lovelace" not in r.text
    assert "Ada Lovelace" not in pc.get("/admin/research", headers=ADM).text


def test_consent_password_must_be_set_and_differ_from_the_admin_token(penv, monkeypatch):
    pc = _client(monkeypatch)
    _decide(pc, "m-ada", "agreed", name="Ada Lovelace")
    monkeypatch.delenv("DLC_CONSENT_ADMIN_TOKEN")
    assert pc.get("/v1/health").json()["consent_token_set"] is False
    for path in RECORD_PATHS:
        assert pc.get(path, headers={"X-DLC-Consent-Token": ""}).status_code == 401
        assert pc.get(path, headers=REC).status_code == 401
    monkeypatch.setenv("DLC_CONSENT_ADMIN_TOKEN", "adm")
    assert pc.get("/v1/health").json()["consent_token_set"] is False
    for path in RECORD_PATHS:
        assert pc.get(path, headers={"X-DLC-Consent-Token": "adm"}).status_code == 401


def test_linkage_file_cannot_be_the_research_database(penv, monkeypatch):
    monkeypatch.setenv("DLC_CONSENT_DB", str(penv / "proxy.db"))
    pc = _client(monkeypatch)
    with pytest.raises(RuntimeError, match="different file"):
        _decide(pc, "m-ada", "agreed", name="Ada Lovelace")
    main = penv / "proxy.db"                    # refused before anything was written
    assert not main.exists() or b"Ada Lovelace" not in main.read_bytes()


def test_an_older_database_moves_its_names_to_the_linkage_file(penv, monkeypatch, capsys):
    main, link = penv / "proxy.db", penv / "dlc_consent.db"
    conn = sqlite3.connect(str(main))
    with conn:
        conn.executescript("""
CREATE TABLE consents (id INTEGER PRIMARY KEY AUTOINCREMENT, install_id TEXT NOT NULL,
    study_id TEXT, version TEXT, decision TEXT NOT NULL, name TEXT, signature TEXT,
    app_version TEXT, decided_at REAL, received_at REAL NOT NULL,
    UNIQUE(install_id, version, decision, decided_at));
CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, install_id TEXT NOT NULL,
    day TEXT NOT NULL, ts REAL NOT NULL, feature TEXT NOT NULL, model TEXT, ok INTEGER,
    in_tokens INTEGER, out_tokens INTEGER, ms INTEGER, error TEXT, response TEXT);
""")
        conn.executemany(
            "INSERT INTO consents (id, install_id, study_id, version, decision, name,"
            " signature, app_version, decided_at, received_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(3, "m-grace", "26-2770", "v1", "agreed", "Grace Hopper", PNG, "0.1.2.3", 1.7e9, 1.7e9),
             (5, "m-typed", "26-2770", "v1", "agreed", "Typed Only", None, "0.1.2.3", 1.7e9, 1.7e9),
             (8, "m-no", "26-2770", "v1", "declined", "", None, "0.1.2.3", 1.7e9, 1.7e9)])
        conn.executemany(
            "INSERT INTO llm_calls (install_id, day, ts, feature, model, ok, response)"
            " VALUES (?,?,?,?,?,?,?)",
            [("m-grace", "2026-10-01", 1.0, "modeA", "m", 1, "KEEP-ANSWER-GRACE"),
             ("m-no", "2026-10-01", 2.0, "modeA", "m", 1, "DROP-ANSWER-DECLINED"),
             ("m-never-asked", "2026-10-01", 3.0, "explain", "m", 1, "DROP-ANSWER-UNDECIDED")])
    conn.close()
    assert b"Grace Hopper" in main.read_bytes()

    pc = _client(monkeypatch)
    h = pc.get("/v1/health").json()
    assert h["ok"] is True
    out = capsys.readouterr().out
    assert "moved 3 consent record(s) to the linkage file" in out
    assert "removed the stored AI answer text of 2 call(s)" in out

    raw = main.read_bytes()
    assert b"Grace Hopper" not in raw and b"Typed Only" not in raw and PNG_BODY not in raw
    cols = _columns(main, "consents")
    assert "name" not in cols and "signature" not in cols
    assert _rows(main, "SELECT id, install_id, decision, has_signature FROM consents"
                       " ORDER BY id") == [(3, "m-grace", "agreed", 1),
                                           (5, "m-typed", "agreed", 0),
                                           (8, "m-no", "declined", 0)]
    assert _rows(link, "SELECT id, install_id, decision, name, signature FROM"
                       " consent_records ORDER BY id") == [
        (3, "m-grace", "agreed", "Grace Hopper", PNG),
        (5, "m-typed", "agreed", "Typed Only", None),
        (8, "m-no", "declined", "", None)]
    res = pc.get("/admin/research", headers=ADM).json()
    assert res["counts"]["agreed"] == 2 and res["counts"]["declined"] == 1

    assert _rows(main, "SELECT install_id, response FROM llm_calls ORDER BY id") == [
        ("m-grace", "KEEP-ANSWER-GRACE"), ("m-no", None), ("m-never-asked", None)]
    assert b"DROP-ANSWER" not in raw and b"KEEP-ANSWER-GRACE" in raw

    assert pc.get("/v1/health").json()["ok"] is True
    assert len(_rows(link, "SELECT id FROM consent_records")) == 3
    assert "moved" not in capsys.readouterr().out

def test_answer_text_is_kept_only_for_machines_that_agreed(penv, monkeypatch):
    pc = _client(monkeypatch)
    _decide(pc, "m-yes", "agreed", name="Ada Lovelace")
    _decide(pc, "m-no", "declined")
    for iid, tag in (("m-yes", "TAGYES"), ("m-no", "TAGNO"), ("m-undecided", "TAGUND")):
        r = _ask(pc, iid, tag)
        assert r["ok"] is True and r["text"] == f"MODEL-ANSWER-{tag}"
    main = penv / "proxy.db"
    assert _rows(main, "SELECT install_id, ok, in_tokens, response FROM llm_calls"
                       " ORDER BY id") == [("m-yes", 1, 10, "MODEL-ANSWER-TAGYES"),
                                           ("m-no", 1, 10, None),
                                           ("m-undecided", 1, 10, None)]
    raw = main.read_bytes()
    assert b"TAGNO" not in raw and b"TAGUND" not in raw and b"TAGYES" in raw
    assert b"SECRET-PROMPT-TEXT" not in raw

    d = pc.get("/admin/llm_texts", headers=ADM).json()
    assert d["total"] == 1 and d["machines"] == ["m-yes"]
    assert d["rows"][0]["install_id"] == "m-yes"
    csv = pc.get("/admin/export.csv", headers=ADM, params={"table": "llm_calls"}).text
    lines = csv.lstrip("﻿").strip().split("\n")
    assert len(lines) == 2 and ",m-yes," in lines[1]
    assert "m-no" not in csv and "m-undecided" not in csv


def test_decline_removes_earlier_answers_and_the_daily_limit_still_counts(penv, monkeypatch):
    from proxy import dlc_proxy
    monkeypatch.setattr(dlc_proxy, "CALL_BUDGETS", {"modeA": 2})
    pc = _client(monkeypatch)
    main = penv / "proxy.db"
    _decide(pc, "m-flip", "agreed", name="Ada Lovelace", at=1_700_000_000.0)
    assert _ask(pc, "m-flip", "TAGFIRST")["ok"] is True
    assert _rows(main, "SELECT response FROM llm_calls") == [("MODEL-ANSWER-TAGFIRST",)]

    out = _decide(pc, "m-flip", "declined", version="v2", at=1_700_000_500.0)
    assert out["answers_removed"] == 1
    assert _rows(main, "SELECT response FROM llm_calls") == [(None,)]
    assert b"TAGFIRST" not in main.read_bytes()

    second = _ask(pc, "m-flip", "TAGSECOND")
    assert second["ok"] is True and second["limit"] == {"feature": "modeA", "used": 2, "budget": 2}
    third = _ask(pc, "m-flip", "TAGTHIRD")
    assert third["ok"] is False and third["limit_hit"] is True
    assert _rows(main, "SELECT response FROM llm_calls ORDER BY id") == [(None,), (None,)]

    monkeypatch.setattr(dlc_proxy, "CALL_BUDGETS", {"modeA": 5})
    _decide(pc, "m-flip", "agreed", name="Ada Lovelace", version="v3", at=1_700_000_900.0)
    assert _ask(pc, "m-flip", "TAGLATER")["ok"] is True
    assert _rows(main, "SELECT response FROM llm_calls ORDER BY id")[-1] == ("MODEL-ANSWER-TAGLATER",)

def test_a_token_in_the_web_address_is_refused(penv, monkeypatch):
    pc = _client(monkeypatch)
    _decide(pc, "m-ada", "agreed", name="Ada Lovelace")
    for path in USAGE_PATHS:
        assert pc.get(path, params={"token": "adm"}).status_code == 401
        assert pc.get(path, headers=ADM).status_code == 200
    for path in RECORD_PATHS:
        assert pc.get(path, params={"token": "names"}).status_code == 401
        assert pc.get(path, params={"token": "adm"}).status_code == 401
    page = pc.get("/admin/view").text
    assert "token=" not in page
    assert "X-DLC-Admin-Token" in page and "X-DLC-Consent-Token" in page
    assert 'type="password"' in page and "localStorage.setItem(\"dlc_consent" not in page
