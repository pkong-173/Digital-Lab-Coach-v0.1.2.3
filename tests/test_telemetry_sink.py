import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

from dlc.telemetry import sink
from dlc.web import server
from dlc.web.server import app

client = TestClient(app)
STATIC = Path(server.__file__).parent / "static"
SAMPLE = "data/sample_circuits/tier1_minimal/single_and.dig"


def test_log_events_keeps_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.chdir(tmp_path)
    n = sink.log_events("sess1", [
        {"ts": 1751800000000, "kind": "l3_modeA_started",
         "filename": "cpu.dig"},
        {"kind": "tab_switch", "tab": "l3"},
    ])
    assert n == 0
    assert list(tmp_path.rglob("*")) == []


def test_the_app_has_no_route_that_takes_usage_events():
    r = client.post("/api/telemetry", json={
        "session_id": "web1", "events": [{"kind": "upload", "count": 2}]})
    assert r.status_code in (404, 405)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not [p for p in paths if "telemetry" in p or "event" in p]


def test_front_end_event_calls_do_nothing():
    defined = 0
    for js in sorted(STATIC.rglob("*.js")):
        text = js.read_text(encoding="utf-8")
        assert "sendBeacon" not in text, js.name
        assert "/api/telemetry" not in text, js.name
        defined += len(re.findall(r"function\s+logEvent\b", text))
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    # the one definition is the empty function
    assert defined == 1
    assert re.search(
        r"function\s+logEvent\s*\(_kind, _details = \{\}\)\s*\{\s*\}", app_js)


def test_using_the_app_makes_no_outbound_request(tmp_path, monkeypatch):
    """Upload, analysis and a row simulation with no course server set."""
    import httpx

    def explode(*a, **kw):
        raise AssertionError("the app made an outbound request")
    for name in ("get", "post", "put", "request", "stream"):
        monkeypatch.setattr(httpx, name, explode)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    with open(SAMPLE, "rb") as fh:
        r = client.post("/api/circuit", files=[
            ("files", ("single_and.dig", fh, "application/xml"))])
    assert r.status_code == 200
    sid = r.json()["session_id"]
    try:
        assert client.get("/api/config/proxy").json()["configured"] is False
        assert client.get("/api/machine").status_code == 200
        sim = client.post("/api/simulate", json={
            "session_id": sid, "filename": "single_and.dig"})
        assert sim.status_code == 200
    finally:
        server._SESSIONS.pop(sid, None)
    written = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert written <= {"machine.json", "dlc_config.json",
                       "official_tests.json"}


def test_gc_sessions_removes_idle_sessions_and_their_tmp_dirs(tmp_path):
    d = tmp_path / "dlc-old"
    d.mkdir()
    (d / "x.dig").write_text("<circuit/>")
    server._SESSIONS["oldsess"] = {
        "tmp_dir": str(d), "files": [],
        "last_used": time.time() - server.SESSION_TTL_SECONDS - 10,
    }
    server._SESSIONS["fresh"] = {
        "tmp_dir": str(tmp_path / "dlc-fresh"), "files": [],
        "last_used": time.time(),
    }
    try:
        n = server._gc_sessions()
        assert n >= 1
        assert "oldsess" not in server._SESSIONS
        assert "fresh" in server._SESSIONS
        assert not d.exists()
    finally:
        server._SESSIONS.pop("fresh", None)
        server._SESSIONS.pop("oldsess", None)


def test_activity_defers_the_session_ttl():
    server._SESSIONS["busy"] = {
        "tmp_dir": None, "files": [{"name": "a.dig", "path": "/nope/a.dig"}],
        "last_used": time.time() - server.SESSION_TTL_SECONDS - 10,
    }
    try:
        server._resolve_target("busy", "a.dig")
        assert server._gc_sessions() == 0
        assert "busy" in server._SESSIONS
    finally:
        server._SESSIONS.pop("busy", None)


def test_gc_jobs_drops_stale_records():
    with server._JOBS_LOCK:
        server._JOBS["oldjob"] = {
            "ok": True, "finished": True,
            "created_at": time.time() - server.JOB_TTL_SECONDS - 10,
        }
        server._JOBS["newjob"] = {
            "ok": True, "finished": False, "created_at": time.time(),
        }
    try:
        assert server._gc_jobs() >= 1
        with server._JOBS_LOCK:
            assert "oldjob" not in server._JOBS
            assert "newjob" in server._JOBS
    finally:
        with server._JOBS_LOCK:
            server._JOBS.pop("newjob", None)
            server._JOBS.pop("oldjob", None)


def test_upload_runs_the_gc_sweep(tmp_path):
    d = tmp_path / "dlc-stale"
    d.mkdir()
    server._SESSIONS["stale"] = {
        "tmp_dir": str(d), "files": [],
        "last_used": time.time() - server.SESSION_TTL_SECONDS - 10,
    }
    with open(SAMPLE, "rb") as fh:
        r = client.post("/api/circuit",
                        files=[("files", ("single_and.dig", fh, "application/xml"))])
    assert r.status_code == 200
    new_sid = r.json()["session_id"]
    try:
        assert "stale" not in server._SESSIONS
        assert not d.exists()
        assert server._SESSIONS[new_sid]["last_used"] > 0
    finally:
        server._SESSIONS.pop(new_sid, None)
        server._SESSIONS.pop("stale", None)
