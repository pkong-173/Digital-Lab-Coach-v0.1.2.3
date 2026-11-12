"""
Machine identity -> course proxy (key custody, limits, the ledger) ->
client relay routing.

The proxy keeps one row per AI request under the hashed machine id. 
Several tests here read the database file itself to hold it to that.
"""

import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

import dlc.llm.client as lc
from dlc.telemetry import machine as mach

M1 = "a1" * 8
M2 = "b2" * 8
ADM = {"X-DLC-Admin-Token": "adm"}
LEDGER_COLUMNS = ["id", "install_id", "day", "ts", "feature", "model", "ok",
                  "in_tokens", "out_tokens", "ms", "error"]
# the three in the middle are never filled
MACHINE_COLUMNS = ["install_id", "first_seen", "issued_client", "id_source",
                   "app_version", "last_seen"]
EMPTY = "issued_client IS NULL AND id_source IS NULL AND app_version IS NULL"


@pytest.fixture()
def tele_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_MACHINE_CACHE", str(tmp_path / "machine.json"))
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


def test_machine_id_is_a_hash_that_does_not_contain_its_source(
        tele_env, monkeypatch):
    monkeypatch.setattr(mach, "_raw_machine_identifier",
                        lambda: "4C4C4544-0042-RAW-GUID")
    code = mach.install_id()
    assert len(code) == 16 and set(code) <= set("0123456789abcdef")
    assert "4c4c" not in code and "RAW" not in code


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


def _body(machine=M1, feature="modeA", **over):
    return {"install_id": machine, "feature": feature,
            "model": "claude-opus-5", "prompt": "hi", **over}


def _rows(sql, args=()):
    conn = sqlite3.connect(os.environ["DLC_PROXY_DB"])
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _columns(table):
    return [r[1] for r in _rows(f"PRAGMA table_info({table})")]


def _count(table):
    """Rows in a ledger table; 0 when the proxy never opened its database."""
    try:
        return _rows(f"SELECT COUNT(*) FROM {table}")[0][0]
    except sqlite3.OperationalError:
        return 0


def _tables():
    return {r[0] for r in _rows(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%'")}


def _file_bytes(tmp_path):
    """Everything the proxy wrote: the database and any side file."""
    return b"".join(p.read_bytes() for p in sorted(tmp_path.iterdir())
                    if p.name.startswith("proxy.db"))


def test_proxy_budget_survives_redownload_and_separates_machines(
        tele_env, monkeypatch):
    from proxy import dlc_proxy
    monkeypatch.setattr(dlc_proxy, "CALL_BUDGETS", {"modeA": 2})
    pc = _proxy_client(monkeypatch)
    assert pc.post("/v1/llm", json=_body()).json()["ok"] is True
    assert pc.post("/v1/llm", json=_body()).json()["ok"] is True
    third = pc.post("/v1/llm", json=_body()).json()
    assert third["ok"] is False and third["limit_hit"] is True
    pc2 = _proxy_client(monkeypatch)
    again = pc2.post("/v1/llm", json=_body()).json()
    assert again["ok"] is False and again["limit_hit"] is True
    assert pc2.post("/v1/llm", json=_body(M2)).json()["ok"] is True


def test_proxy_course_token_gate(tele_env, monkeypatch):
    monkeypatch.setenv("DLC_COURSE_TOKEN", "sekrit")
    pc = _proxy_client(monkeypatch)
    assert pc.post("/v1/llm", json=_body()).status_code == 401
    ok = pc.post("/v1/llm", json=_body(),
                 headers={"X-DLC-Token": "sekrit"})
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_proxy_refuses_every_request_without_a_course_token(tele_env,
                                                            monkeypatch):
    monkeypatch.delenv("DLC_COURSE_TOKEN", raising=False)
    pc = _proxy_client(monkeypatch)
    assert pc.post("/v1/llm", json=_body()).status_code == 503
    assert pc.post("/v1/llm", json=_body(),
                   headers={"X-DLC-Token": "anything"}).status_code == 503
    assert pc.post("/v1/events", json={"install_id": M1}).status_code == 503
    assert pc.post("/v1/anything", json={}).status_code == 503
    h = pc.get("/v1/health")
    assert h.status_code == 200
    assert h.json()["course_token_set"] is False
    assert h.json()["admin_token_set"] is False

    monkeypatch.setenv("DLC_COURSE_TOKEN", "tok")
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    assert pc.post("/v1/events", json={"install_id": M1}).status_code == 200
    h = pc.get("/v1/health").json()
    assert h["course_token_set"] is True and h["admin_token_set"] is True


def test_relay_names_a_missing_server_key_and_spends_nothing(tele_env,
                                                             monkeypatch):
    import dlc.llm.client as real
    monkeypatch.setattr(real, "get_api_key", lambda p="anthropic": "")
    pc = _proxy_client(monkeypatch)
    r = pc.post("/v1/llm", json=_body())
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is False and out["server_misconfigured"] is True
    assert "instructor" in out["error"] and "no API key" in out["error"]
    assert pc.get("/v1/health").json()["today_calls"] == 0


# what the proxy keeps

def test_the_ledger_holds_request_counts_and_nothing_else(tele_env,
                                                          monkeypatch):
    def upstream(prompt, **kw):
        return {"ok": True, "text": "ANSWER-TEXT-7731 use an XOR gate",
                "error": None,
                "usage": {"input_tokens": 321, "output_tokens": 45},
                "model": kw.get("model")}
    pc = _proxy_client(monkeypatch, upstream)
    out = pc.post("/v1/llm", json=_body(
        prompt="PROMPT-TEXT-5512 my_cpu_final.dig fails row 3",
        system="SYSTEM-TEXT-9904")).json()
    # the student still gets the answer
    assert out["ok"] is True and "ANSWER-TEXT-7731" in out["text"]

    assert _tables() == {"machines", "llm_calls"}
    assert _columns("llm_calls") == LEDGER_COLUMNS
    assert _columns("machines") == MACHINE_COLUMNS
    assert _rows(f"SELECT install_id FROM machines WHERE {EMPTY}") == [(M1,)]
    (row,) = _rows("SELECT install_id, feature, model, ok, in_tokens,"
                   " out_tokens, error FROM llm_calls")
    assert row == (M1, "modeA", "claude-opus-5", 1, 321, 45, None)

    raw = _file_bytes(tele_env)
    for text in (b"PROMPT-TEXT-5512", b"my_cpu_final", b"SYSTEM-TEXT-9904",
                 b"ANSWER-TEXT-7731", b"XOR gate"):
        assert text not in raw


def test_the_token_check_notes_the_machine_code_and_nothing_else(
        tele_env, monkeypatch):
    pc = _proxy_client(monkeypatch)
    # an earlier app version sends much more on this path
    batch = {"install_id": M1, "issued": "2026-08-01", "id_source": "os",
             "app_version": "9.8.7-MARK",
             "events": [{"client_row_id": 1, "kind": "upload",
                         "props": {"filename": "EVENT-TEXT-4410.dig"}}]}
    assert pc.post("/v1/events", json=batch).json() == {"ok": True,
                                                        "stored": 0}
    assert pc.post("/v1/events", json={"install_id": M2}).json()["ok"] is True
    assert pc.post("/v1/events", json=batch,
                   headers={"X-DLC-Token": "wrong"}).status_code == 401
    assert pc.post("/v1/events", json={"install_id": ""}).status_code == 400

    assert _tables() == {"machines", "llm_calls"}
    assert _columns("machines") == MACHINE_COLUMNS
    assert _rows(f"SELECT install_id FROM machines WHERE {EMPTY}"
                 " ORDER BY install_id") == [(M1,), (M2,)]
    assert _count("llm_calls") == 0
    raw = _file_bytes(tele_env)
    for text in (b"EVENT-TEXT-4410", b"9.8.7-MARK", b"2026-08-01", b"upload"):
        assert text not in raw


def test_anything_else_posted_under_v1_is_checked_and_dropped(tele_env,
                                                              monkeypatch):
    pc = _proxy_client(monkeypatch)
    body = {"install_id": M1, "name": "TYPED-NAME-8831",
            "picture": "data:image/png;base64,PICTURE-MARK-5520"}
    for path in ("/v1/retired-path", "/v1/another_one"):
        assert pc.post(path, json=body).json() == {"ok": True, "stored": 0}
        assert pc.post(path, json=body,
                       headers={"X-DLC-Token": "wrong"}).status_code == 401
    # the body is never read: it does not even have to be JSON
    assert pc.post("/v1/retired-path", content=b"\x00not json").status_code == 200
    assert _count("machines") == 0 and _count("llm_calls") == 0
    raw = _file_bytes(tele_env)
    assert b"TYPED-NAME-8831" not in raw and b"PICTURE-MARK-5520" not in raw
    # the real routes are not shadowed
    assert pc.post("/v1/llm", json=_body()).json()["ok"] is True
    assert pc.get("/v1/health").json()["today_calls"] == 1


def test_the_server_answers_only_these_paths(tele_env, monkeypatch):
    from proxy import dlc_proxy
    paths = {r.path for r in dlc_proxy.app.routes}
    framework = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    assert paths - framework == {
        "/v1/llm", "/v1/events", "/v1/{name}", "/v1/health",
        "/admin/summary", "/admin/daily", "/admin/stats",
        "/admin/export.csv", "/admin/view"}


def test_health_reports_only_configuration_and_counts(tele_env, monkeypatch):
    pc = _proxy_client(monkeypatch)
    assert set(pc.get("/v1/health").json()) == {
        "ok", "machines", "db_path", "key_configured", "key_format_ok",
        "course_token_set", "admin_token_set", "today_calls",
        "today_est_usd", "global_daily_calls", "global_daily_usd"}


_OLD_DATABASE = """
CREATE TABLE machines (install_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL,
    issued_client TEXT, id_source TEXT, app_version TEXT, last_seen REAL);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL, client_row_id INTEGER NOT NULL, kind TEXT NOT NULL,
    received_at REAL NOT NULL, props TEXT NOT NULL);
CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL, day TEXT NOT NULL, ts REAL NOT NULL,
    feature TEXT NOT NULL, model TEXT, ok INTEGER, in_tokens INTEGER,
    out_tokens INTEGER, ms INTEGER, error TEXT, response TEXT);
CREATE INDEX idx_llm_budget ON llm_calls(install_id, feature, day);
"""


def test_a_database_from_an_earlier_version_keeps_working(tele_env,
                                                          monkeypatch):
    """The file is used as it is: the counts carry on, and nothing else an
    earlier version left in it is served by any route."""
    from proxy import dlc_proxy
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    day = dlc_proxy._today().isoformat()
    conn = sqlite3.connect(os.environ["DLC_PROXY_DB"])
    conn.executescript(_OLD_DATABASE)
    with conn:
        conn.execute("INSERT INTO machines VALUES (?,?,?,?,?,?)",
                     (M1, "2026-10-01", "2026-09-30", "os", "9.8.7-MARK", 50.0))
        conn.execute("INSERT INTO events (install_id, client_row_id, kind,"
                     " received_at, props) VALUES (?,?,?,?,?)",
                     (M1, 1, "upload", 1.0, '{"filename": "OLD-EVENT-1188"}'))
        for i in range(3):
            conn.execute(
                "INSERT INTO llm_calls (install_id, day, ts, feature, model,"
                " ok, in_tokens, out_tokens, ms, error, response)"
                " VALUES (?,?,?,?,?,1,100,20,900,NULL,?)",
                (M1, day, 10.0 + i, "modeA", "claude-opus-5",
                 f"OLD-ANSWER-2277-{i}"))
    conn.close()

    monkeypatch.setattr(dlc_proxy, "CALL_BUDGETS", {"modeA": 4})
    pc = _proxy_client(monkeypatch)
    assert pc.get("/v1/health").json()["today_calls"] == 3
    # the three earlier calls still count against today's budget
    assert pc.post("/v1/llm", json=_body()).json()["ok"] is True
    assert pc.post("/v1/llm", json=_body()).json()["limit_hit"] is True
    assert _count("llm_calls") == 4

    served = pc.get("/v1/health").text + pc.get("/admin/view").text
    for path in ("/admin/summary", "/admin/daily", "/admin/stats",
                 "/admin/export.csv", "/admin/export.csv?table=machines"):
        r = pc.get(path, headers=ADM)
        assert r.status_code == 200, path
        served += r.text
    for text in ("OLD-EVENT-1188", "OLD-ANSWER-2277", "9.8.7-MARK",
                 "2026-09-30"):
        assert text not in served


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
    # the request carries the machine code and what the model needs
    assert set(seen) - {"url"} == {"install_id", "feature", "model",
                                   "prompt", "system", "max_tokens",
                                   "effort"}


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
    assert pc.post("/v1/llm", json=_body()).json()["ok"] is True
    conn = sqlite3.connect(os.environ["DLC_PROXY_DB"])
    with conn:
        conn.execute(
            "INSERT INTO llm_calls (install_id, day, ts, feature, model, "
            "ok, in_tokens, out_tokens, ms, error) "
            "VALUES (?,?,?,?,?,0,NULL,NULL,NULL,'pending')",
            (M1, dlc_proxy._today().isoformat(), 0.0, "modeA",
             "claude-opus-5"))
    conn.close()
    out = pc.post("/v1/llm", json=_body()).json()
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
    assert pc.post("/v1/llm", json=_body(M1)).json()["ok"] is True
    assert pc.post("/v1/llm", json=_body(M1)).json()["ok"] is True
    out = pc.post("/v1/llm", json=_body(M2)).json()
    assert out["ok"] is False
    assert out.get("capacity_hit") is True
    assert "daily capacity" in out["error"]
    h = pc.get("/v1/health").json()
    assert h["today_calls"] == 2 and h["global_daily_calls"] == 2


def test_limit_message_dropped_redownload_sentence(tele_env, monkeypatch):
    from proxy import dlc_proxy
    monkeypatch.setattr(dlc_proxy, "CALL_BUDGETS", {"modeA": 1})
    pc = _proxy_client(monkeypatch)
    assert pc.post("/v1/llm", json=_body()).json()["ok"] is True
    out = pc.post("/v1/llm", json=_body()).json()
    assert out["limit_hit"] is True
    assert "resets tomorrow" in out["error"]
    assert "Re-downloading" not in out["error"]


# the admin side

def test_admin_header_auth_daily_and_dashboard(tele_env, monkeypatch):
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    pc = _proxy_client(monkeypatch)
    assert pc.post("/v1/llm", json=_body()).json()["ok"] is True

    assert pc.get("/admin/summary").status_code == 401
    s = pc.get("/admin/summary", headers=ADM)
    assert s.status_code == 200
    assert s.json()["machines"] == [{
        "install_id": M1, "first_seen": s.json()["machines"][0]["first_seen"],
        "last_seen": s.json()["machines"][0]["last_seen"], "llm_calls": 1}]

    d = pc.get("/admin/daily", headers=ADM).json()
    assert d["llm"][0]["install_id"] == M1
    assert d["llm"][0]["calls"] == 1
    assert d["spend_by_day"] and "est_usd" in d["spend_by_day"][0]

    page = pc.get("/admin/view")
    assert page.status_code == 200
    assert "dashboard" in page.text.lower()
    assert "X-DLC-Admin-Token" in page.text


def test_a_token_in_the_web_address_is_refused(tele_env, monkeypatch):
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    pc = _proxy_client(monkeypatch)
    for path in ("/admin/summary", "/admin/daily", "/admin/stats",
                 "/admin/export.csv"):
        assert pc.get(path, params={"token": "adm"}).status_code == 401
        assert pc.get(path).status_code == 401
        assert pc.get(path, headers=ADM).status_code == 200
    page = pc.get("/admin/view").text
    assert "token=" not in page
    assert 'type="password"' in page


def test_admin_stats_and_exports_come_from_the_ledger(tele_env, monkeypatch):
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    pc = _proxy_client(monkeypatch)
    for machine, feature in ((M1, "modeA"), (M1, "modeA"), (M1, "explain"),
                             (M2, "modeB")):
        assert pc.post("/v1/llm",
                       json=_body(machine, feature)).json()["ok"] is True

    st = pc.get("/admin/stats", params={"range_days": 7}, headers=ADM).json()
    assert set(st) == {"since", "range_days", "totals", "active_by_day",
                       "by_feature", "spend_by_day"}
    assert st["totals"]["active_machines"] == 2
    assert st["totals"]["new_machines"] == 2
    assert st["totals"]["llm_calls"] == 4 and st["totals"]["ok_calls"] == 4
    assert st["totals"]["in_tokens"] == 40
    assert st["active_by_day"][-1]["machines"] == 2
    assert st["active_by_day"][-1]["calls"] == 4
    assert {f["feature"]: f["calls"] for f in st["by_feature"]} == {
        "modeA": 2, "explain": 1, "modeB": 1}

    usage = pc.get("/admin/export.csv", headers=ADM)
    assert usage.status_code == 200
    lines = usage.text.lstrip("\ufeff").strip().splitlines()
    assert lines[0] == ("day,install_id,feature,calls,ok_calls,in_tokens,"
                        "out_tokens")
    # one line per day, machine and feature: the rows the dashboard shows
    assert sorted(ln.split(",")[1:4] for ln in lines[1:]) == [
        [M1, "explain", "1"], [M1, "modeA", "2"], [M2, "modeB", "1"]]
    machines = pc.get("/admin/export.csv", params={"table": "machines"},
                      headers=ADM).text.lstrip("\ufeff").strip().splitlines()
    assert machines[0] == "install_id,first_seen,last_seen"
    assert {ln.split(",")[0] for ln in machines[1:]} == {M1, M2}
    for table in ("llm_calls", "events", "sqlite_master"):
        assert pc.get("/admin/export.csv", params={"table": table},
                      headers=ADM).status_code == 400


def test_export_cells_cannot_act_as_formulas_or_break_rows(tele_env,
                                                           monkeypatch):
    """The relay stores the machine code and the feature as sent, so a
    hand-made request can put any text there."""
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    pc = _proxy_client(monkeypatch)
    odd_machine = '=HYPERLINK("http://x")'
    assert pc.post("/v1/llm", json=_body(odd_machine,
                                         "line one\rline two")).json()["ok"]
    assert pc.post("/v1/llm", json=_body("@SUM(1)", "+plus")).json()["ok"]
    text = pc.get("/admin/export.csv", headers=ADM).text.lstrip("\ufeff")
    import csv
    import io
    rows = list(csv.reader(io.StringIO(text, newline="")))
    assert len(rows) == 3
    cells = {(r[1], r[2]) for r in rows[1:]}
    assert cells == {("'" + odd_machine, "line one\rline two"),
                     ("'@SUM(1)", "'+plus")}


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
    sent = []

    def fake_post(url, **kw):
        sent.append((url, kw))
        return _R(replies["code"])
    monkeypatch.setattr(httpx, "post", fake_post)

    body = {"url": "http://course.example", "token": "tok"}
    assert wc.post("/api/config/proxy",
                   json=body).json() == {"ok": True, "configured": True,
                                         "verify": "accepted"}
    # the check sends the token and the machine code, nothing else
    url, kw = sent[-1]
    assert url == "http://course.example/v1/events"
    assert kw.get("headers") == {"X-DLC-Token": "tok"}
    assert set(kw["json"]) == {"install_id"}
    assert len(kw["json"]["install_id"]) == 16
    assert not {"data", "content", "params"} & set(kw)

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


def test_the_app_and_the_proxy_agree_on_the_token_check(tele_env,
                                                        monkeypatch):
    from dlc.web import server as web
    pc = _proxy_client(monkeypatch)
    import httpx

    def through(url, json=None, headers=None, timeout=None):
        assert url.startswith("http://course.example/")
        return pc.post(url[len("http://course.example"):], json=json,
                       headers=headers)
    monkeypatch.setattr(httpx, "post", through)
    assert web._verify_course_server("http://course.example", "tok") \
        == "accepted"
    assert web._verify_course_server("http://course.example", "nope") \
        == "bad_token"


def test_health_reports_effective_key_not_just_env(tele_env, monkeypatch):
    import dlc.llm.client as real
    monkeypatch.setattr(real, "get_api_key",
                        lambda p="anthropic": "sk-from-config")
    pc = _proxy_client(monkeypatch)
    h = pc.get("/v1/health").json()
    assert h["key_configured"] is True
    assert h["key_format_ok"] is True


def test_health_names_the_database_file(tele_env, monkeypatch):
    pc = _proxy_client(monkeypatch)
    h = pc.get("/v1/health").json()
    import os as _os
    assert h["db_path"] == str(
        __import__("pathlib").Path(_os.environ["DLC_PROXY_DB"]).resolve())
