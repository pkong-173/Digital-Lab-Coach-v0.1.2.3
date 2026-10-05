"""
Telemetry chain: machine identity -> local spool -> ship ->
course proxy (limits, ingest, dedup) -> client relay routing.
"""

import json
import os
import sqlite3
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

import dlc.llm.client as lc
from dlc.telemetry import machine as mach
from dlc.telemetry import ship, sink


@pytest.fixture()
def tele_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_TELEMETRY_DB", str(tmp_path / "tele.db"))
    monkeypatch.setenv("DLC_MACHINE_CACHE", str(tmp_path / "machine.json"))
    monkeypatch.setenv("DLC_CONSENT_PATH", str(tmp_path / "consent.json"))
    monkeypatch.setenv("DLC_STUDY_CACHE", str(tmp_path / "study.json"))
    monkeypatch.setenv("DLC_PROXY_DB", str(tmp_path / "proxy.db"))
    monkeypatch.delenv("DLC_PROXY_URL", raising=False)
    monkeypatch.setenv("DLC_COURSE_TOKEN", "tok")
    monkeypatch.setenv("DLC_PROXY_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("DLC_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DLC_GLOBAL_DAILY_CALLS", raising=False)
    monkeypatch.delenv("DLC_GLOBAL_DAILY_USD", raising=False)
    return tmp_path


def test_machine_id_survives_cache_deletion(tele_env, monkeypatch):
    monkeypatch.setattr(mach, "_raw_machine_identifier",
                        lambda: "GUID-AAAA-BBBB")
    a = mach.machine_identity()
    assert a["source"] == "os" and len(a["install_id"]) == 16
    assert a["issued"]
    (tele_env / "machine.json").unlink()
    b = mach.machine_identity()
    assert b["install_id"] == a["install_id"]
    monkeypatch.setattr(mach, "_raw_machine_identifier",
                        lambda: "GUID-OTHER")
    c = mach.machine_identity()
    assert c["install_id"] != a["install_id"]


def test_machine_id_stable_fallback_without_os_guid(tele_env, monkeypatch):
    monkeypatch.setattr(mach, "_raw_machine_identifier", lambda: None)
    a = mach.machine_identity()
    assert a["source"] == "stable_fallback"
    (tele_env / "machine.json").unlink()
    assert mach.machine_identity()["install_id"] == a["install_id"]


# proxy app

def _proxy_client(monkeypatch, upstream=None):
    from proxy import dlc_proxy
    if upstream is None:
        def upstream(prompt, **kw):
            return {"ok": True, "text": "{}", "error": None,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "stop_reason": "end_turn", "model": kw.get("model")}
    import dlc.llm.client as real
    monkeypatch.setattr(real, "call_llm", upstream)
    return TestClient(dlc_proxy.app, headers={"X-DLC-Token": "tok"})


def test_proxy_ingest_dedup_and_first_seen(tele_env, monkeypatch):
    pc = _proxy_client(monkeypatch)
    batch = {"install_id": "m1", "issued": "2026-08-01",
             "id_source": "os", "app_version": "0.1.0",
             "events": [{"client_row_id": 1, "kind": "upload",
                         "props": {"count": 2}},
                        {"client_row_id": 2, "kind": "tests_run_complete",
                         "props": {}}]}
    assert pc.post("/v1/events", json=batch).json()["ok"] is True
    pc.post("/v1/events", json=batch)
    h = pc.get("/v1/health").json()
    assert h["machines"] == 1 and h["events"] == 2

    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    s = pc.get("/admin/summary", params={"token": "adm"}).json()
    assert s["machines"][0]["install_id"] == "m1"
    assert s["machines"][0]["first_seen"]
    assert s["machines"][0]["issued_client"] == "2026-08-01"


def test_proxy_budget_survives_redownload_and_separates_machines(
        tele_env, monkeypatch):
    from proxy import dlc_proxy
    monkeypatch.setattr(dlc_proxy, "CALL_BUDGETS", {"modeA": 2})
    pc = _proxy_client(monkeypatch)
    body = {"install_id": "mach-A", "feature": "modeA",
            "model": "claude-opus-5", "prompt": "hi"}
    assert pc.post("/v1/llm", json=body).json()["ok"] is True
    assert pc.post("/v1/llm", json=body).json()["ok"] is True
    third = pc.post("/v1/llm", json=body).json()
    assert third["ok"] is False and third["limit_hit"] is True
    pc2 = _proxy_client(monkeypatch)
    again = pc2.post("/v1/llm", json=body).json()
    assert again["ok"] is False and again["limit_hit"] is True
    other = dict(body, install_id="mach-B")
    assert pc2.post("/v1/llm", json=other).json()["ok"] is True


def test_proxy_course_token_gate(tele_env, monkeypatch):
    monkeypatch.setenv("DLC_COURSE_TOKEN", "sekrit")
    pc = _proxy_client(monkeypatch)
    body = {"install_id": "m", "feature": "modeA",
            "model": "claude-opus-5", "prompt": "hi"}
    assert pc.post("/v1/llm", json=body).status_code == 401
    ok = pc.post("/v1/llm", json=body,
                 headers={"X-DLC-Token": "sekrit"})
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_proxy_refuses_every_request_without_a_course_token(tele_env,
                                                            monkeypatch):
    monkeypatch.delenv("DLC_COURSE_TOKEN", raising=False)
    pc = _proxy_client(monkeypatch)
    body = {"install_id": "m", "feature": "modeA",
            "model": "claude-opus-5", "prompt": "hi"}
    assert pc.post("/v1/llm", json=body).status_code == 503
    assert pc.post("/v1/llm", json=body,
                   headers={"X-DLC-Token": "anything"}).status_code == 503
    batch = {"install_id": "m", "events": []}
    assert pc.post("/v1/events", json=batch).status_code == 503
    h = pc.get("/v1/health")
    assert h.status_code == 200
    assert h.json()["course_token_set"] is False
    assert h.json()["admin_token_set"] is False

    monkeypatch.setenv("DLC_COURSE_TOKEN", "tok")
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    assert pc.post("/v1/events", json=batch).status_code == 200
    h = pc.get("/v1/health").json()
    assert h["course_token_set"] is True and h["admin_token_set"] is True


def test_relay_names_a_missing_server_key_and_spends_nothing(tele_env,
                                                             monkeypatch):
    import dlc.llm.client as real
    monkeypatch.setattr(real, "get_api_key", lambda p="anthropic": "")
    pc = _proxy_client(monkeypatch)
    body = {"install_id": "m", "feature": "modeA",
            "model": "claude-opus-5", "prompt": "hi"}
    r = pc.post("/v1/llm", json=body)
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is False and out["server_misconfigured"] is True
    assert "instructor" in out["error"] and "no API key" in out["error"]
    assert pc.get("/v1/health").json()["today_calls"] == 0


def _reinstall_batches():
    def batch(rows, t0):
        return {"install_id": "same-laptop", "events": [
            {"client_row_id": i, "client_ts": t0 + i, "kind": k,
             "props": {"n": i}} for i, k in rows]}
    week1 = batch([(1, "upload"), (2, "tests_run_complete"),
                   (3, "l3_hint_level")], 1000.0)
    after = batch([(1, "upload"), (2, "l2_llm_complete"),
                   (3, "l3_fix_accepted")], 2000.0)
    return week1, after


def test_events_survive_a_student_reinstall(tele_env, monkeypatch):
    pc = _proxy_client(monkeypatch)
    week1, after = _reinstall_batches()
    assert pc.post("/v1/events", json=week1).json()["stored"] == 3
    assert pc.post("/v1/events", json=after).json()["stored"] == 3
    assert pc.get("/v1/health").json()["events"] == 6
    assert pc.post("/v1/events", json=after).json()["stored"] == 0
    assert pc.get("/v1/health").json()["events"] == 6


def test_events_key_migrates_an_old_ledger(tele_env, monkeypatch):
    old = sqlite3.connect(os.environ["DLC_PROXY_DB"])
    with old:
        old.executescript("""
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL, client_row_id INTEGER NOT NULL,
    session_id TEXT, kind TEXT NOT NULL, client_ts REAL, stored_at REAL,
    received_at REAL NOT NULL, props TEXT NOT NULL,
    UNIQUE(install_id, client_row_id));
INSERT INTO events (install_id, client_row_id, kind, client_ts,
                    received_at, props)
    VALUES ('same-laptop', 1, 'upload', 1001.0, 5.0, '{}'),
           ('same-laptop', 2, 'tests_run_complete', 1002.0, 5.0, '{}'),
           ('same-laptop', 3, 'l3_hint_level', 1003.0, 5.0, '{}');
""")
    old.close()
    pc = _proxy_client(monkeypatch)
    assert pc.get("/v1/health").json()["events"] == 3
    _, after = _reinstall_batches()
    assert pc.post("/v1/events", json=after).json()["stored"] == 3
    assert pc.get("/v1/health").json()["events"] == 6
    conn = sqlite3.connect(os.environ["DLC_PROXY_DB"])
    (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE name='events'"
                          ).fetchone()
    assert "stored_at)" in sql.replace(" ", "")
    (mx,) = conn.execute("SELECT MAX(id) FROM events").fetchone()
    conn.close()
    assert mx == 6


def test_fresh_spool_never_reuses_shipped_row_ids(tele_env, monkeypatch):
    sink.log_events("s", [{"kind": "upload"}, {"kind": "upload"}])
    conn = sink._connect()
    first = [r[0] for r in conn.execute("SELECT id FROM events ORDER BY id")]
    conn.close()
    assert first[0] > 10**12 and first[1] == first[0] + 1
    os.remove(os.environ["DLC_TELEMETRY_DB"])          # UNINSTALL
    time.sleep(0.002)
    sink.log_events("s", [{"kind": "upload"}])
    conn = sink._connect()
    (again,) = conn.execute("SELECT MIN(id) FROM events").fetchone()
    conn.close()
    assert again > first[-1]


def test_ship_moves_spool_to_proxy_and_survives_offline(tele_env,
                                                        monkeypatch):
    monkeypatch.setattr(mach, "_raw_machine_identifier", lambda: "G-1")
    from dlc.telemetry import consent
    consent.record("agreed", name="Test Student")
    sink.log_events("sess1", [{"kind": "upload", "count": 1},
                              {"kind": "tests_run_complete", "ok": True}])
    assert ship.ship_pending()["reason"] == "no_proxy"

    pc = _proxy_client(monkeypatch)
    monkeypatch.setenv("DLC_PROXY_URL", "http://course.example")

    class _Resp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body
        def json(self):
            return self._body

    def fake_post(url, json=None, headers=None, timeout=None):
        assert url == "http://course.example/v1/events"
        r = pc.post("/v1/events", json=json, headers=headers)
        return _Resp(r.status_code, r.json())

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    out = ship.ship_pending()
    assert out["reason"] == "ok" and out["shipped"] == 2
    assert pc.get("/v1/health").json()["events"] == 2

    assert ship.ship_pending()["reason"] == "up_to_date"
    conn = sqlite3.connect(str(tele_env / "tele.db"))
    with conn:
        conn.execute("UPDATE ship_state SET last_shipped = 0")
    conn.close()
    assert ship.ship_pending()["shipped"] == 2
    assert pc.get("/v1/health").json()["events"] == 2

    def dead_post(*a, **kw):
        raise OSError("no route to host")
    monkeypatch.setattr(httpx, "post", dead_post)
    sink.log_events("sess1", [{"kind": "upload"}])
    assert ship.ship_pending()["shipped"] == 0


# client relay routing

def test_call_llm_routes_through_proxy_when_configured(tele_env,
                                                       monkeypatch):
    monkeypatch.delenv("DLC_PROXY_SELF", raising=False)
    monkeypatch.setattr(mach, "_raw_machine_identifier", lambda: "G-2")
    monkeypatch.setenv("DLC_PROXY_URL", "http://course.example")
    seen = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"ok": True, "text": "{}", "error": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "model": "claude-opus-5",
                    "limit": {"feature": "modeA", "used": 1, "budget": 8}}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(json)
        seen["url"] = url
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    r = lc.call_llm("hello", model="claude-opus-5", feature="modeA")
    assert r["ok"] is True and r["limit"]["feature"] == "modeA"
    assert seen["url"] == "http://course.example/v1/llm"
    assert seen["feature"] == "modeA"
    assert len(seen["install_id"]) == 16
    assert seen["model"] == "claude-opus-5"


def test_call_llm_goes_direct_inside_the_proxy_process(tele_env,
                                                       monkeypatch):
    import proxy.dlc_proxy
    assert os.environ.get("DLC_PROXY_SELF") == "1"
    monkeypatch.setenv("DLC_PROXY_URL", "http://course.example")

    import httpx

    def explode(*a, **kw):
        raise AssertionError("proxy process tried to relay to a proxy")
    monkeypatch.setattr(httpx, "post", explode)

    class _SDK:
        def __init__(self, *a, **kw):
            pass

        @property
        def messages(self):
            return self

        def create(self, **kw):
            class _B:
                type = "text"
                text = "{}"

            class _U:
                input_tokens = 1
                output_tokens = 1

            class _R:
                content = [_B()]
                usage = _U()
                stop_reason = "end_turn"
            return _R()

    monkeypatch.setattr(lc, "Anthropic", _SDK, raising=False)
    monkeypatch.setattr(lc, "get_api_key", lambda p=None: "sk-test")
    r = lc.call_llm("hi", model="claude-opus-5", feature="modeA")
    assert r["ok"] is True
    assert "limit" not in r


def test_budget_counts_inflight_reservations(tele_env, monkeypatch):
    from proxy import dlc_proxy
    monkeypatch.setattr(dlc_proxy, "CALL_BUDGETS", {"modeA": 2})
    pc = _proxy_client(monkeypatch)
    body = {"install_id": "m-rsv", "feature": "modeA",
            "model": "claude-opus-5", "prompt": "hi"}
    assert pc.post("/v1/llm", json=body).json()["ok"] is True
    conn = sqlite3.connect(os.environ["DLC_PROXY_DB"])
    with conn:
        conn.execute(
            "INSERT INTO llm_calls (install_id, day, ts, feature, model, "
            "ok, in_tokens, out_tokens, ms, error) "
            "VALUES (?,?,?,?,?,0,NULL,NULL,NULL,'pending')",
            ("m-rsv", dlc_proxy._today().isoformat(), 0.0, "modeA",
             "claude-opus-5"))
    conn.close()
    out = pc.post("/v1/llm", json=body).json()
    assert out["ok"] is False and out["limit_hit"] is True


def test_debugger_tags_modeA_feature(tele_env):
    seen = {}
    def call(prompt, **kw):
        seen.update(kw)
        return {"ok": True, "text": json.dumps({
            "contract": "l3.debug.v1.1", "confidence": 0.5,
            "hint": {"suspect_region": "x", "suspect_signals": [],
                     "why": "y"},
            "fix": {"ops": [{"op": "change_attribute",
                             "component_index": 16, "name": "Value",
                             "value": 0}],
                    "explanation_for_student": "z",
                    "animation_script": [{"act": "retest"}]}}),
            "error": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "model": "fake"}
    from dlc.l3.debugger import debug_circuit
    res = debug_circuit("data/sample_circuits/30_bug_benchmark/bug3_wrong_cin/"
                        "Wrong_cin.dig", call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert seen.get("feature") == "modeA"
    # the deterministic suspects ride along so a re-upload can be compared
    assert res["mode"] == "analysis"
    assert isinstance(res["suspect_indices"], list) and res["suspect_indices"]

def test_global_capacity_breaker_across_machines(tele_env, monkeypatch):
    monkeypatch.setenv("DLC_GLOBAL_DAILY_CALLS", "2")
    pc = _proxy_client(monkeypatch)
    a = {"install_id": "mach-A", "feature": "modeA",
         "model": "claude-opus-5", "prompt": "hi"}
    assert pc.post("/v1/llm", json=a).json()["ok"] is True
    assert pc.post("/v1/llm", json=a).json()["ok"] is True
    b = dict(a, install_id="mach-B")
    out = pc.post("/v1/llm", json=b).json()
    assert out["ok"] is False
    assert out.get("capacity_hit") is True
    assert "daily capacity" in out["error"]
    h = pc.get("/v1/health").json()
    assert h["today_calls"] == 2 and h["global_daily_calls"] == 2


def test_limit_message_dropped_redownload_sentence(tele_env, monkeypatch):
    from proxy import dlc_proxy
    monkeypatch.setattr(dlc_proxy, "CALL_BUDGETS", {"modeA": 1})
    pc = _proxy_client(monkeypatch)
    body = {"install_id": "m-msg", "feature": "modeA",
            "model": "claude-opus-5", "prompt": "hi"}
    assert pc.post("/v1/llm", json=body).json()["ok"] is True
    out = pc.post("/v1/llm", json=body).json()
    assert out["limit_hit"] is True
    assert "resets tomorrow" in out["error"]
    assert "Re-downloading" not in out["error"]


def test_admin_header_auth_daily_and_dashboard(tele_env, monkeypatch):
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    pc = _proxy_client(monkeypatch)
    body = {"install_id": "m-dash", "feature": "modeA",
            "model": "claude-opus-5", "prompt": "hi"}
    assert pc.post("/v1/llm", json=body).json()["ok"] is True

    assert pc.get("/admin/summary").status_code == 401
    s = pc.get("/admin/summary", headers={"X-DLC-Admin-Token": "adm"})
    assert s.status_code == 200

    d = pc.get("/admin/daily", headers={"X-DLC-Admin-Token": "adm"}).json()
    assert d["llm"][0]["install_id"] == "m-dash"
    assert d["llm"][0]["calls"] == 1
    assert d["spend_by_day"] and "est_usd" in d["spend_by_day"][0]

    page = pc.get("/admin/view")
    assert page.status_code == 200
    assert "dashboard" in page.text.lower()
    assert "X-DLC-Admin-Token" in page.text


def test_proxy_401_maps_to_settings_guidance(tele_env, monkeypatch):
    monkeypatch.delenv("DLC_PROXY_SELF", raising=False)
    monkeypatch.setattr(mach, "_raw_machine_identifier", lambda: "G-3")
    monkeypatch.setenv("DLC_PROXY_URL", "http://course.example")

    class _R401:
        status_code = 401
        def json(self):
            return {"detail": "bad course token"}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _R401())
    r = lc.call_llm("hello", model="claude-opus-5", feature="modeA")
    assert r["ok"] is False
    assert "re-paste" in r["error"]
    assert "Settings" in r["error"]


def test_saving_course_server_verifies_token(tele_env, monkeypatch):
    from fastapi.testclient import TestClient
    from dlc.web.server import app as webapp
    monkeypatch.setattr(mach, "_raw_machine_identifier", lambda: "G-4")
    wc = TestClient(webapp)

    class _R:
        def __init__(self, code):
            self.status_code = code
        def json(self):
            return {}

    import httpx
    replies = {"code": 200}
    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: _R(replies["code"]))

    body = {"url": "http://course.example", "token": "tok"}
    assert wc.post("/api/config/proxy",
                   json=body).json()["verify"] == "accepted"
    replies["code"] = 401
    assert wc.post("/api/config/proxy",
                   json=body).json()["verify"] == "bad_token"
    replies["code"] = 503
    assert wc.post("/api/config/proxy",
                   json=body).json()["verify"] == "unreachable"

    def boom(*a, **kw):
        raise OSError("down")
    monkeypatch.setattr(httpx, "post", boom)
    assert wc.post("/api/config/proxy",
                   json=body).json()["verify"] == "unreachable"

    g = wc.get("/api/config/proxy", params={"verify": 1}).json()
    assert g["configured"] is True and g["verify"] == "unreachable"

    out = wc.post("/api/config/proxy", json={"url": ""}).json()
    assert out == {"ok": True, "configured": False}


def test_admin_events_feed_filters_and_pagination(tele_env, monkeypatch):
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    pc = _proxy_client(monkeypatch)
    for mach_id, rows in (("m-one", 4), ("m-two", 2)):
        pc.post("/v1/events", json={
            "install_id": mach_id,
            "events": [{"client_row_id": i,
                        "kind": "upload" if i % 2 else "tests_run_started",
                        "props": {"count": i, "filename": f"f{i}.dig"}}
                       for i in range(1, rows + 1)]})

    assert pc.get("/admin/events").status_code == 401
    hdr = {"X-DLC-Admin-Token": "adm"}

    d = pc.get("/admin/events", headers=hdr).json()
    assert d["total"] == 6
    assert set(d["machines"]) == {"m-one", "m-two"}
    assert set(d["kinds"]) == {"upload", "tests_run_started"}
    assert d["days"] and d["rows"][0]["id"] > d["rows"][-1]["id"]
    assert isinstance(d["rows"][0]["props"], dict)
    assert d["rows"][0]["time"]

    d = pc.get("/admin/events", headers=hdr,
               params={"kind": "upload"}).json()
    assert d["total"] == 3
    assert all(r["kind"] == "upload" for r in d["rows"])

    d = pc.get("/admin/events", headers=hdr,
               params={"install_id": "m-two"}).json()
    assert d["total"] == 2

    d = pc.get("/admin/events", headers=hdr,
               params={"per_page": 2, "page": 3}).json()
    assert d["total"] == 6 and len(d["rows"]) == 2

    d = pc.get("/admin/events", headers=hdr,
               params={"day": "1999-01-01"}).json()
    assert d["total"] == 0 and d["rows"] == []


def test_health_reports_effective_key_not_just_env(tele_env, monkeypatch):
    import dlc.llm.client as real
    monkeypatch.setattr(real, "get_api_key",
                        lambda p="anthropic": "sk-from-config")
    pc = _proxy_client(monkeypatch)
    h = pc.get("/v1/health").json()
    assert h["key_configured"] is True
    assert h["key_format_ok"] is True


def test_llm_texts_feed_stores_and_filters_responses(tele_env,
                                                     monkeypatch):
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")

    def upstream(prompt, **kw):
        return {"ok": True,
                "text": json.dumps({
                    "contract": "l3.debug.v1.1", "confidence": 0.8,
                    "hint": {"why": "carry-in stuck"},
                    "fix": {"ops": [{"op": "change_attribute"}],
                            "explanation_for_student": "flip the const"}}),
                "error": None,
                "usage": {"input_tokens": 9, "output_tokens": 4},
                "stop_reason": "end_turn", "model": kw.get("model")}
    pc = _proxy_client(monkeypatch, upstream=upstream)
    for feat in ("modeA", "explain", "explain"):
        pc.post("/v1/llm", json={"install_id": "m-txt", "feature": feat,
                                 "model": "claude-opus-5", "prompt": "p"})

    assert pc.get("/admin/llm_texts").status_code == 401
    hdr = {"X-DLC-Admin-Token": "adm"}
    d = pc.get("/admin/llm_texts", headers=hdr).json()
    assert d["total"] == 3
    assert set(d["features"]) == {"modeA", "explain"}
    assert "explanation_for_student" in d["rows"][0]["response"]
    assert d["rows"][0]["time"]

    d = pc.get("/admin/llm_texts", headers=hdr,
               params={"feature": "explain"}).json()
    assert d["total"] == 2
    d = pc.get("/admin/llm_texts", headers=hdr,
               params={"per_page": 2, "page": 2}).json()
    assert d["total"] == 3 and len(d["rows"]) == 1


def test_llm_response_column_migrates_old_db(tele_env, monkeypatch):
    import os as _os
    db = _os.environ["DLC_PROXY_DB"]
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " install_id TEXT NOT NULL, day TEXT NOT NULL, ts REAL NOT"
            " NULL, feature TEXT NOT NULL, model TEXT, ok INTEGER,"
            " in_tokens INTEGER, out_tokens INTEGER, ms INTEGER,"
            " error TEXT)")
    conn.close()
    pc = _proxy_client(monkeypatch)
    body = {"install_id": "m-old", "feature": "modeA",
            "model": "claude-opus-5", "prompt": "hi"}
    assert pc.post("/v1/llm", json=body).json()["ok"] is True
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    d = pc.get("/admin/llm_texts",
               headers={"X-DLC-Admin-Token": "adm"}).json()
    assert d["total"] == 1 and d["rows"][0]["response"] == "{}"


def test_admin_stats_windows_and_l3_outcomes(tele_env, monkeypatch):
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    pc = _proxy_client(monkeypatch)
    pc.post("/v1/events", json={
        "install_id": "m-st", "events": [
            {"client_row_id": 1, "kind": "upload", "props": {"count": 1}},
            {"client_row_id": 2, "kind": "l3_modeA_result_server",
             "props": {"filename": "a.dig", "cards": 2,
                       "confirmed": True, "llm_calls": 1}},
            {"client_row_id": 3, "kind": "l3_modeA_result_server",
             "props": {"filename": "b.dig", "cards": 0,
                       "confirmed": False, "llm_calls": 1}},
            {"client_row_id": 6, "kind": "l3_modeA_result_server",
             "props": {"filename": "c.dig", "mode": "rom_mismatch",
                       "cards": 0, "confirmed": 0, "llm_calls": 0}},
            {"client_row_id": 7, "kind": "l1_result",
             "props": {"filename": "a.dig", "errors": 2, "warnings": 1,
                       "kinds": ["width_mismatch"], "unsupported": False,
                       "failed": False, "testcase_rows": 8}},
            {"client_row_id": 8, "kind": "l1_result",
             "props": {"filename": "b.dig", "errors": 0, "warnings": 0,
                       "kinds": [], "unsupported": True, "failed": False,
                       "testcase_rows": 4}},
            {"client_row_id": 9, "kind": "tests_run_complete",
             "props": {"filename": "a.dig", "mode": "per_row", "ok": True,
                       "all_passed": False, "failing_rows": 3,
                       "total_rows": 8}},
            {"client_row_id": 10, "kind": "reupload_diff",
             "props": {"filename": "a.dig", "comps_changed": 1,
                       "wires_changed": 0, "had_suspects": True,
                       "touched_suspect": True}},
            {"client_row_id": 11, "kind": "l3_locked",
             "props": {"filename": "b.dig", "reason": "l1_errors",
                       "errors": 2}},
            {"client_row_id": 4, "kind": "l3_modeB_result_server",
             "props": {"filename": "a.dig", "proposals": 3, "rows": 5,
                       "disputed": 2, "rejected": 1,
                       "rejected_kinds": {"duplicate": 1},
                       "covered": False}},
            {"client_row_id": 12, "kind": "l3_modeB_inject_outcome",
             "props": {"file": "a.dig", "outcome": "rows_fail", "added": 5,
                       "failed": 2, "disputed": 2, "disputed_failed": 1,
                       "clean_failed": 1}},
            {"client_row_id": 13, "kind": "l3_modeB_adopted_official",
             "props": {"file": "a.dig", "rows": 9}},
            {"client_row_id": 5, "kind": "l3_accept_fix_server",
             "props": {"filename": "a.dig"}}]})
    pc.post("/v1/events", json={
        "install_id": "m-st2", "events": [
            {"client_row_id": 1, "kind": "upload", "props": {}}]})
    for feat in ("modeA", "explain"):
        pc.post("/v1/llm", json={"install_id": "m-st", "feature": feat,
                                 "model": "claude-opus-5", "prompt": "p"})

    assert pc.get("/admin/stats").status_code == 401
    hdr = {"X-DLC-Admin-Token": "adm"}
    d = pc.get("/admin/stats", headers=hdr).json()
    assert d["range_days"] == 7 and d["since"] <= d["active_by_day"][0]["day"]
    t = d["totals"]
    assert t["active_machines"] == 2 and t["events"] == 14
    assert d["l1"] == {"files": 2, "with_errors": 1, "unsupported": 1,
                       "failed": 0, "avg_test_rows": 6.0}
    assert d["tests"] == {"runs": 1, "all_passed": 0,
                          "avg_failing_rows": 3.0}
    assert t["llm_calls"] == 2 and t["ok_calls"] == 2
    assert t["est_usd"] >= 0
    l3 = d["l3"]
    assert l3["modeA_runs"] == 2 and l3["modeA_confirmed"] == 1
    assert l3["modeA_refused"] == {"rom_mismatch": 1, "l1_locked": 1}
    assert l3["hint_targeting"] == {"reuploads": 1, "touched_suspect": 1,
                                    "touched_card": 0}
    assert l3["modeA_cards"] == 2
    assert l3["modeB_runs"] == 1 and l3["fixes_accepted"] == 1
    assert l3["modeB"] == {"rows": 5, "disputed": 2, "rejected": 1,
                           "covered": 0, "accepts": 1, "rows_accepted": 5,
                           "rows_failed": 2, "clean_failed": 1,
                           "adopted": 1, "adopted_rows": 9}
    feats = {f["feature"]: f for f in d["by_feature"]}
    assert feats["modeA"]["calls"] == 1 and feats["explain"]["calls"] == 1
    assert d["active_by_day"][0]["machines"] == 2
    assert any(k["kind"] == "upload" and k["n"] == 2
               for k in d["top_kinds"])
    d30 = pc.get("/admin/stats", headers=hdr,
                 params={"range_days": 30}).json()
    assert d30["range_days"] == 30 and d30["totals"]["events"] == 14
    assert pc.get("/admin/stats", headers=hdr,
                  params={"range_days": 0}).status_code == 422


def test_health_names_the_database_file(tele_env, monkeypatch):
    pc = _proxy_client(monkeypatch)
    h = pc.get("/v1/health").json()
    import os as _os
    assert h["db_path"] == str(
        __import__("pathlib").Path(_os.environ["DLC_PROXY_DB"]).resolve())
