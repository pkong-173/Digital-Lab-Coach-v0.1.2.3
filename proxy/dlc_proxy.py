"""
DLC course proxy: key custody + machine-keyed limits + telemetry.

One small server the instructor user runs; students' tools point at it via
`proxy_url` in their config.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import sqlite3
import time
import zipfile
from datetime import date, datetime, timezone
from html import escape as _h
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app):
    _startup_sanity()
    yield


app = FastAPI(title="DLC course proxy", lifespan=_lifespan)

os.environ["DLC_PROXY_SELF"] = "1"

CALL_BUDGETS = {"modeA": 4, "modeB": 4, "grade": 2, "explain": 2}
_DEFAULT_BUDGET = 12


def _global_daily_calls() -> int:
    try:
        return int(os.environ.get("DLC_GLOBAL_DAILY_CALLS", "") or 600)
    except ValueError:
        return 600


def _global_daily_usd() -> float:
    try:
        return float(os.environ.get("DLC_GLOBAL_DAILY_USD", "") or 20.0)
    except ValueError:
        return 20.0

_PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0), "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0), "gpt-5": (1.25, 10.0),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    install_id  TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    issued_client TEXT,
    id_source   TEXT,
    app_version TEXT,
    last_seen   REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL,
    client_row_id INTEGER NOT NULL,
    session_id TEXT,
    kind TEXT NOT NULL,
    client_ts REAL,
    stored_at REAL,
    received_at REAL NOT NULL,
    props TEXT NOT NULL,
    UNIQUE(install_id, client_row_id, stored_at)
);
CREATE INDEX IF NOT EXISTS idx_ev_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_ev_machine ON events(install_id);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL,
    day TEXT NOT NULL,
    ts REAL NOT NULL,
    feature TEXT NOT NULL,
    model TEXT,
    ok INTEGER,
    in_tokens INTEGER,
    out_tokens INTEGER,
    ms INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_budget ON llm_calls(install_id, feature, day);
"""


def _db() -> sqlite3.Connection:
    p = Path(os.environ.get("DLC_PROXY_DB", "dlc_proxy.db"))
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    try:
        conn.execute("ALTER TABLE llm_calls ADD COLUMN response TEXT")
    except sqlite3.OperationalError:
        pass
    _migrate_events_key(conn)
    conn.executescript(_CONSENT_SCHEMA)
    return conn


# Research consent records live in their own table: the typed name and the
# drawn signature sit here and nowhere else; the usage data in `events` only
# ever carries the hashed install id (the study code).
_CONSENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL,
    study_id TEXT,
    version TEXT,
    decision TEXT NOT NULL,
    name TEXT,
    signature TEXT,
    app_version TEXT,
    decided_at REAL,
    received_at REAL NOT NULL,
    UNIQUE(install_id, version, decision, decided_at)
);
CREATE INDEX IF NOT EXISTS idx_consent_machine ON consents(install_id);
"""


def _study_id() -> str | None:
    return os.environ.get("DLC_STUDY_ID", "").strip() or None


def _survey_rate() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("DLC_SURVEY_RATE", "") or 0.35)))
    except ValueError:
        return 0.35


def _migrate_events_key(conn: sqlite3.Connection) -> None:
    (sql,) = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    if not re.search(r"UNIQUE\s*\(\s*install_id\s*,\s*client_row_id\s*\)", sql):
        return
    conn.executescript("""
BEGIN;
CREATE TABLE events_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL,
    client_row_id INTEGER NOT NULL,
    session_id TEXT,
    kind TEXT NOT NULL,
    client_ts REAL,
    stored_at REAL,
    received_at REAL NOT NULL,
    props TEXT NOT NULL,
    UNIQUE(install_id, client_row_id, stored_at)
);
INSERT INTO events_new (id, install_id, client_row_id, session_id, kind,
                        client_ts, stored_at, received_at, props)
    SELECT id, install_id, client_row_id, session_id, kind,
           client_ts, COALESCE(stored_at, client_ts, 0.0), received_at, props
    FROM events;
DROP TABLE events;
ALTER TABLE events_new RENAME TO events;
CREATE INDEX IF NOT EXISTS idx_ev_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_ev_machine ON events(install_id);
COMMIT;
""")


def _check_course_token(tok: str | None) -> None:
    want = os.environ.get("DLC_COURSE_TOKEN")
    if not want:
        raise HTTPException(
            status_code=503,
            detail="course server not configured: DLC_COURSE_TOKEN is unset")
    if (tok or "") != want:
        raise HTTPException(status_code=401, detail="bad course token")


def _check_admin(token: str | None, header_token: str | None = None) -> None:
    want = os.environ.get("DLC_ADMIN_TOKEN")
    got = header_token or token or ""
    if not want or got != want:
        raise HTTPException(status_code=401, detail="bad admin token")


def _est_usd(conn, day: str | None = None) -> float:
    q = ("SELECT model, COALESCE(SUM(in_tokens),0),"
         " COALESCE(SUM(out_tokens),0) FROM llm_calls")
    args: tuple = ()
    if day:
        q += " WHERE day = ?"
        args = (day,)
    q += " GROUP BY model"
    spend = 0.0
    for model, i, o in conn.execute(q, args):
        pin, pout = _PRICES.get(model, (5.0, 25.0))
        spend += (i * pin + o * pout) / 1e6
    return spend


def _touch_machine(conn, install_id: str, issued=None, source=None,
                   version=None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO machines (install_id, first_seen, issued_client,"
            " id_source, app_version, last_seen) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(install_id) DO UPDATE SET last_seen = ?,"
            " app_version = COALESCE(excluded.app_version, app_version)",
            (install_id, _today().isoformat(), issued, source,
             version, time.time(), time.time()))


class EventsIn(BaseModel):
    install_id: str
    issued: str | None = None
    id_source: str | None = None
    app_version: str | None = None
    events: list[dict] = []


@app.post("/v1/events")
def ingest(req: EventsIn,
           x_dlc_token: str | None = Header(default=None)) -> dict:
    _check_course_token(x_dlc_token)
    if not req.install_id:
        raise HTTPException(status_code=400, detail="install_id required")
    conn = _db()
    try:
        _touch_machine(conn, req.install_id, req.issued, req.id_source,
                       req.app_version)
        stored = 0
        now = time.time()
        for ev in req.events[:1000]:
            if not isinstance(ev, dict) or not ev.get("kind"):
                continue
            try:
                with conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO events (install_id,"
                        " client_row_id, session_id, kind, client_ts,"
                        " stored_at, received_at, props)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (req.install_id,
                         int(ev.get("client_row_id") or 0),
                         ev.get("session_id"),
                         str(ev.get("kind"))[:64],
                         ev.get("client_ts"),
                         ev.get("stored_at") or ev.get("client_ts") or 0.0,
                         now,
                         json.dumps(ev.get("props") or {})[:20000]))
                stored += cur.rowcount
            except (sqlite3.Error, TypeError, ValueError):
                continue
        return {"ok": True, "stored": stored}
    finally:
        conn.close()


class LlmIn(BaseModel):
    install_id: str
    feature: str = "other"
    model: str
    prompt: str
    system: str | None = None
    max_tokens: int = 3000
    effort: str | None = None


class ConsentIn(BaseModel):
    install_id: str
    study_id: str | None = None
    version: str | None = None
    decision: str
    name: str | None = None
    signature: str | None = None
    app_version: str | None = None
    decided_at: float | None = None


@app.post("/v1/consent")
def consent_ingest(req: ConsentIn,
                   x_dlc_token: str | None = Header(default=None)) -> dict:
    _check_course_token(x_dlc_token)
    if req.decision not in ("agreed", "declined"):
        raise HTTPException(status_code=400, detail="decision must be agreed or declined")
    if not req.install_id:
        raise HTTPException(status_code=400, detail="install_id required")
    sig = req.signature
    if sig is not None and (not isinstance(sig, str)
                            or not sig.startswith("data:image/png;base64,")
                            or len(sig) > 400_000):
        sig = None
    conn = _db()
    try:
        _touch_machine(conn, req.install_id, None, None, req.app_version)
        removed = 0
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO consents (install_id, study_id, version,"
                " decision, name, signature, app_version, decided_at, received_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (req.install_id, req.study_id, req.version, req.decision,
                 (req.name or "").strip()[:120] if req.decision == "agreed" else "",
                 sig if req.decision == "agreed" else None,
                 req.app_version,
                 req.decided_at if req.decided_at is not None else time.time(),
                 time.time()))
            stored = cur.rowcount
            if req.decision == "declined":
                removed = conn.execute(
                    "DELETE FROM events WHERE install_id = ?",
                    (req.install_id,)).rowcount
        return {"ok": True, "stored": int(stored), "events_removed": int(removed)}
    finally:
        conn.close()


@app.post("/v1/llm")
def relay(req: LlmIn,
          x_dlc_token: str | None = Header(default=None)) -> dict:
    _check_course_token(x_dlc_token)
    if not req.install_id:
        raise HTTPException(status_code=400, detail="install_id required")
    if not _effective_key():
        return {"ok": False, "text": None,
                "error": ("The course server has no API key configured — "
                          "tell your instructor. All deterministic checks "
                          "still work."),
                "server_misconfigured": True,
                "usage": None, "model": req.model}
    day = _today().isoformat()
    budget = CALL_BUDGETS.get(req.feature, _DEFAULT_BUDGET)
    conn = _db()
    try:
        _touch_machine(conn, req.install_id)
        conn.execute("BEGIN IMMEDIATE")
        (day_calls,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE day = ?",
            (day,)).fetchone()
        if (day_calls >= _global_daily_calls()
                or _est_usd(conn, day) >= _global_daily_usd()):
            conn.execute("ROLLBACK")
            return {"ok": False, "text": None,
                    "error": ("The course server has reached its daily"
                              " capacity — please try again tomorrow. All"
                              " deterministic checks still work."),
                    "limit_hit": True, "capacity_hit": True,
                    "usage": None, "model": req.model}
        (used,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE install_id = ?"
            " AND feature = ? AND day = ?",
            (req.install_id, req.feature, day)).fetchone()
        if used >= budget:
            conn.execute("ROLLBACK")
            return {"ok": False, "text": None,
                    "error": (f"Daily limit reached for {req.feature} on"
                              f" this machine — it resets tomorrow."),
                    "limit_hit": True, "usage": None, "model": req.model}
        cur = conn.execute(
            "INSERT INTO llm_calls (install_id, day, ts, feature,"
            " model, ok, in_tokens, out_tokens, ms, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (req.install_id, day, time.time(), req.feature, req.model,
             0, None, None, None, "pending"))
        row_id = cur.lastrowid
        conn.execute("COMMIT")
        t0 = time.monotonic()
        from dlc.llm.client import call_llm
        r = call_llm(req.prompt, model=req.model,
                     max_tokens=req.max_tokens, system=req.system,
                     effort=req.effort)
        ms = int((time.monotonic() - t0) * 1000)
        u = r.get("usage") or {}
        with conn:
            conn.execute(
                "UPDATE llm_calls SET ts = ?, ok = ?, in_tokens = ?,"
                " out_tokens = ?, ms = ?, error = ?, response = ?"
                " WHERE id = ?",
                (time.time(), 1 if r.get("ok") else 0,
                 u.get("input_tokens"), u.get("output_tokens"), ms,
                 (r.get("error") or "")[:300] or None,
                 (r.get("text") or "")[:20000] or None, row_id))
        r["limit"] = {"feature": req.feature, "used": used + 1,
                      "budget": budget}
        return r
    finally:
        conn.close()


def _effective_key() -> str:
    from dlc.llm.client import get_api_key
    return get_api_key("anthropic") or ""


def _startup_sanity() -> None:
    key = _effective_key()
    if not key:
        print("WARNING: no API key — set ANTHROPIC_API_KEY in this terminal"
              " window and start the proxy again. Until then every AI"
              " request answers 'the course server has no API key'.")
    elif not key.startswith("sk-"):
        print("WARNING: ANTHROPIC_API_KEY does not look like a real key"
              " (expected it to start with 'sk-'). LLM relays will fail"
              " with 401 until it is fixed.")
    if not os.environ.get("DLC_COURSE_TOKEN"):
        print("WARNING: DLC_COURSE_TOKEN is not set — every student request"
              " is refused (503) until it is. Set it in THIS terminal window"
              " and start the proxy again.")
    if not os.environ.get("DLC_ADMIN_TOKEN"):
        print("WARNING: DLC_ADMIN_TOKEN is not set — the admin dashboard"
              " rejects every token until it is.")


@app.get("/v1/health")
def health() -> dict:
    conn = _db()
    try:
        (m,) = conn.execute("SELECT COUNT(*) FROM machines").fetchone()
        (e,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        day = _today().isoformat()
        (day_calls,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE day = ?",
            (day,)).fetchone()
        key = _effective_key()
        db_path = str(Path(os.environ.get("DLC_PROXY_DB",
                                          "dlc_proxy.db")).resolve())
        return {"ok": True, "machines": m, "events": e,
                "db_path": db_path,
                "key_configured": bool(key),
                "key_format_ok": key.startswith("sk-") if key else False,
                "course_token_set": bool(os.environ.get("DLC_COURSE_TOKEN")),
                "admin_token_set": bool(os.environ.get("DLC_ADMIN_TOKEN")),
                "today_calls": day_calls,
                "today_est_usd": round(_est_usd(conn, day), 2),
                "global_daily_calls": _global_daily_calls(),
                "global_daily_usd": _global_daily_usd(),
                "study_id": _study_id(),
                "survey_rate": _survey_rate() if _study_id() else 0.0}
    finally:
        conn.close()


@app.get("/admin/summary")
def summary(token: str | None = Query(default=None),
            x_dlc_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        machines = [dict(zip(
            ("install_id", "first_seen", "issued_client", "id_source",
             "app_version", "last_seen"), row))
            for row in conn.execute(
                "SELECT install_id, first_seen, issued_client, id_source,"
                " app_version, last_seen FROM machines"
                " ORDER BY first_seen")]
        for m in machines:
            if m["last_seen"]:
                m["last_seen"] = _fmt_time(m["last_seen"])
            (m["events"],) = conn.execute(
                "SELECT COUNT(*) FROM events WHERE install_id = ?",
                (m["install_id"],)).fetchone()
            (m["llm_calls"],) = conn.execute(
                "SELECT COUNT(*) FROM llm_calls WHERE install_id = ?",
                (m["install_id"],)).fetchone()
        kinds = conn.execute(
            "SELECT kind, COUNT(*) FROM events GROUP BY kind"
            " ORDER BY COUNT(*) DESC LIMIT 30").fetchall()
        spend = 0.0
        for model, i, o in conn.execute(
                "SELECT model, COALESCE(SUM(in_tokens),0),"
                " COALESCE(SUM(out_tokens),0) FROM llm_calls"
                " GROUP BY model"):
            pin, pout = _PRICES.get(model, (5.0, 25.0))
            spend += (i * pin + o * pout) / 1e6
        return {"machines": machines,
                "event_kinds": [{"kind": k, "n": n} for k, n in kinds],
                "llm_spend_est_usd": round(spend, 2)}
    finally:
        conn.close()


@app.get("/admin/daily")
def daily(token: str | None = Query(default=None),
          x_dlc_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        llm = [dict(zip(("day", "install_id", "feature", "calls",
                         "ok_calls", "in_tokens", "out_tokens"), row))
               for row in conn.execute(
                   "SELECT day, install_id, feature, COUNT(*),"
                   " COALESCE(SUM(ok),0), COALESCE(SUM(in_tokens),0),"
                   " COALESCE(SUM(out_tokens),0) FROM llm_calls"
                   " GROUP BY day, install_id, feature"
                   " ORDER BY day DESC, install_id")]
        spend = {}
        for d, model, i, o in conn.execute(
                "SELECT day, model, COALESCE(SUM(in_tokens),0),"
                " COALESCE(SUM(out_tokens),0) FROM llm_calls"
                " GROUP BY day, model"):
            pin, pout = _PRICES.get(model, (5.0, 25.0))
            spend[d] = spend.get(d, 0.0) + (i * pin + o * pout) / 1e6
        ev_day = _day_sql("received_at")
        activity = [dict(zip(("day", "install_id", "events"), row))
                    for row in conn.execute(
                        f"SELECT {ev_day}, install_id,"
                        " COUNT(*) FROM events"
                        f" GROUP BY {ev_day}, install_id"
                        " ORDER BY 1 DESC, install_id")]
        return {"llm": llm,
                "spend_by_day": [{"day": d, "est_usd": round(v, 2)}
                                 for d, v in sorted(spend.items(),
                                                    reverse=True)],
                "activity": activity}
    finally:
        conn.close()


@app.get("/admin/events")
def admin_events(token: str | None = Query(default=None),
                 x_dlc_admin_token: str | None = Header(default=None),
                 day: str | None = Query(default=None),
                 install_id: str | None = Query(default=None),
                 kind: str | None = Query(default=None),
                 page: int = Query(default=1, ge=1),
                 per_page: int = Query(default=50, ge=1, le=200)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        where, args = [], []
        if day:
            where.append(_day_sql("received_at") + " = ?")
            args.append(day)
        if install_id:
            where.append("install_id = ?")
            args.append(install_id)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        (total,) = conn.execute(
            "SELECT COUNT(*) FROM events" + w, args).fetchone()
        rows = [dict(zip(("id", "install_id", "kind", "ts",
                          "session_id", "props"), r))
                for r in conn.execute(
                    "SELECT id, install_id, kind,"
                    " COALESCE(client_ts, received_at), session_id, props"
                    " FROM events" + w +
                    " ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*args, per_page, (page - 1) * per_page))]
        for r in rows:
            try:
                r["props"] = json.loads(r["props"])
            except (TypeError, ValueError):
                r["props"] = {}
            r["time"] = _fmt_time(r.pop("ts"))
        days = [d for (d,) in conn.execute(
            f"SELECT DISTINCT {_day_sql('received_at')} FROM events"
            " ORDER BY 1 DESC LIMIT 120")]
        kinds = [k for (k,) in conn.execute(
            "SELECT DISTINCT kind FROM events ORDER BY kind LIMIT 100")]
        machines = [m for (m,) in conn.execute(
            "SELECT DISTINCT install_id FROM events ORDER BY 1 LIMIT 200")]
        return {"total": total, "page": page, "per_page": per_page,
                "rows": rows, "days": days, "kinds": kinds,
                "machines": machines}
    finally:
        conn.close()


@app.get("/admin/llm_texts")
def admin_llm_texts(token: str | None = Query(default=None),
                    x_dlc_admin_token: str | None = Header(default=None),
                    day: str | None = Query(default=None),
                    install_id: str | None = Query(default=None),
                    feature: str | None = Query(default=None),
                    page: int = Query(default=1, ge=1),
                    per_page: int = Query(default=20, ge=1,
                                          le=100)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        where, args = ["response IS NOT NULL"], []
        if day:
            where.append("day = ?")
            args.append(day)
        if install_id:
            where.append("install_id = ?")
            args.append(install_id)
        if feature:
            where.append("feature = ?")
            args.append(feature)
        w = " WHERE " + " AND ".join(where)
        (total,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls" + w, args).fetchone()
        rows = [dict(zip(("id", "install_id", "feature", "model", "ok",
                          "in_tokens", "out_tokens", "ms", "ts",
                          "response"), r))
                for r in conn.execute(
                    "SELECT id, install_id, feature, model, ok,"
                    " in_tokens, out_tokens, ms, ts, response"
                    " FROM llm_calls" + w +
                    " ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*args, per_page, (page - 1) * per_page))]
        for r in rows:
            r["time"] = _fmt_time(r.pop("ts"))
        days = [d for (d,) in conn.execute(
            "SELECT DISTINCT day FROM llm_calls ORDER BY 1 DESC"
            " LIMIT 120")]
        features = [f for (f,) in conn.execute(
            "SELECT DISTINCT feature FROM llm_calls ORDER BY 1")]
        machines = [m for (m,) in conn.execute(
            "SELECT DISTINCT install_id FROM llm_calls ORDER BY 1"
            " LIMIT 200")]
        return {"total": total, "page": page, "per_page": per_page,
                "rows": rows, "days": days, "features": features,
                "machines": machines}
    finally:
        conn.close()


@app.get("/admin/stats")
def admin_stats(token: str | None = Query(default=None),
                x_dlc_admin_token: str | None = Header(default=None),
                range_days: int = Query(default=7, ge=1,
                                        le=366)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    from datetime import timedelta
    since = (_today() - timedelta(days=range_days - 1)).isoformat()
    conn = _db()
    try:
        ev_day = _day_sql("received_at")
        active_by_day = [dict(zip(("day", "machines", "events"), r))
                         for r in conn.execute(
            f"SELECT {ev_day}, COUNT(DISTINCT install_id), COUNT(*)"
            f" FROM events WHERE {ev_day} >= ?"
            f" GROUP BY {ev_day} ORDER BY 1", (since,))]
        (active, ev_total) = conn.execute(
            f"SELECT COUNT(DISTINCT install_id), COUNT(*) FROM events"
            f" WHERE {ev_day} >= ?", (since,)).fetchone()
        (new_machines,) = conn.execute(
            "SELECT COUNT(*) FROM machines WHERE first_seen >= ?",
            (since,)).fetchone()
        by_feature = {}
        spend_by_feature = {}
        for feat, model, calls, ok, i, o in conn.execute(
                "SELECT feature, model, COUNT(*), COALESCE(SUM(ok),0),"
                " COALESCE(SUM(in_tokens),0), COALESCE(SUM(out_tokens),0)"
                " FROM llm_calls WHERE day >= ?"
                " GROUP BY feature, model", (since,)):
            f = by_feature.setdefault(feat, {"feature": feat, "calls": 0,
                                             "ok_calls": 0,
                                             "in_tokens": 0,
                                             "out_tokens": 0})
            f["calls"] += calls
            f["ok_calls"] += ok
            f["in_tokens"] += i
            f["out_tokens"] += o
            pin, pout = _PRICES.get(model, (5.0, 25.0))
            spend_by_feature[feat] = spend_by_feature.get(feat, 0.0) +                 (i * pin + o * pout) / 1e6
        features = []
        for feat, f in sorted(by_feature.items()):
            f["est_usd"] = round(spend_by_feature.get(feat, 0.0), 2)
            features.append(f)
        top_kinds = [dict(zip(("kind", "n"), r)) for r in conn.execute(
            f"SELECT kind, COUNT(*) FROM events WHERE {ev_day} >= ?"
            f" GROUP BY kind ORDER BY COUNT(*) DESC LIMIT 15", (since,))]
        by_mode = {}
        for m, n, c, k in conn.execute(
                f"SELECT COALESCE(json_extract(props, '$.mode'), 'analysis'),"
                f" COUNT(*),"
                f" COALESCE(SUM(json_extract(props, '$.confirmed')), 0),"
                f" COALESCE(SUM(json_extract(props, '$.cards')), 0)"
                f" FROM events WHERE kind = 'l3_modeA_result_server'"
                f" AND {ev_day} >= ? GROUP BY 1", (since,)):
            by_mode[m] = (n, int(c or 0), int(k or 0))
        a_runs, a_confirmed, a_cards = by_mode.get("analysis", (0, 0, 0))
        a_refused = {m: v[0] for m, v in by_mode.items() if m != "analysis"}
        (locked,) = conn.execute(
            f"SELECT COUNT(*) FROM events WHERE kind = 'l3_locked'"
            f" AND {ev_day} >= ?", (since,)).fetchone()
        if locked:
            a_refused["l1_locked"] = locked
        (l1_files, l1_err, l1_unsup, l1_failed, l1_rows) = conn.execute(
            f"SELECT COUNT(*),"
            f" COALESCE(SUM(json_extract(props, '$.errors') > 0), 0),"
            f" COALESCE(SUM(json_extract(props, '$.unsupported') = 1), 0),"
            f" COALESCE(SUM(json_extract(props, '$.failed') = 1), 0),"
            f" AVG(json_extract(props, '$.testcase_rows'))"
            f" FROM events WHERE kind = 'l1_result'"
            f" AND {ev_day} >= ?", (since,)).fetchone()
        (t_runs, t_pass, t_fail_rows) = conn.execute(
            f"SELECT COUNT(*),"
            f" COALESCE(SUM(json_extract(props, '$.all_passed') = 1), 0),"
            f" AVG(json_extract(props, '$.failing_rows'))"
            f" FROM events WHERE kind = 'tests_run_complete'"
            f" AND {ev_day} >= ?", (since,)).fetchone()
        (h_re, h_suspect, h_card) = conn.execute(
            f"SELECT COUNT(*),"
            f" COALESCE(SUM(json_extract(props, '$.touched_suspect') = 1), 0),"
            f" COALESCE(SUM(json_extract(props, '$.touched_card') = 1), 0)"
            f" FROM events WHERE kind = 'reupload_diff'"
            f" AND json_extract(props, '$.had_suspects') = 1"
            f" AND {ev_day} >= ?", (since,)).fetchone()
        l1 = {"files": l1_files, "with_errors": int(l1_err),
              "unsupported": int(l1_unsup), "failed": int(l1_failed),
              "avg_test_rows": (round(l1_rows, 1) if l1_rows is not None
                                else None)}
        tests = {"runs": t_runs, "all_passed": int(t_pass),
                 "avg_failing_rows": (round(t_fail_rows, 1)
                                      if t_fail_rows is not None else None)}
        hint = {"reuploads": h_re, "touched_suspect": int(h_suspect),
                "touched_card": int(h_card)}
        (b_runs, b_rows, b_disp, b_rej, b_cov) = conn.execute(
            f"SELECT COUNT(*),"
            f" COALESCE(SUM(json_extract(props, '$.rows')), 0),"
            f" COALESCE(SUM(json_extract(props, '$.disputed')), 0),"
            f" COALESCE(SUM(json_extract(props, '$.rejected')), 0),"
            f" COALESCE(SUM(json_extract(props, '$.covered') = 1), 0)"
            f" FROM events WHERE kind = 'l3_modeB_result_server'"
            f" AND {ev_day} >= ?", (since,)).fetchone()
        (b_acc, b_added, b_failed, b_clean_failed) = conn.execute(
            f"SELECT COUNT(*),"
            f" COALESCE(SUM(json_extract(props, '$.added')), 0),"
            f" COALESCE(SUM(json_extract(props, '$.failed')), 0),"
            f" COALESCE(SUM(json_extract(props, '$.clean_failed')), 0)"
            f" FROM events WHERE kind = 'l3_modeB_inject_outcome'"
            f" AND {ev_day} >= ?", (since,)).fetchone()
        (b_adopt, b_adopt_rows) = conn.execute(
            f"SELECT COUNT(*),"
            f" COALESCE(SUM(json_extract(props, '$.rows')), 0)"
            f" FROM events WHERE kind = 'l3_modeB_adopted_official'"
            f" AND {ev_day} >= ?", (since,)).fetchone()
        modeB = {"rows": int(b_rows or 0), "disputed": int(b_disp or 0),
                 "rejected": int(b_rej or 0), "covered": int(b_cov or 0),
                 "accepts": b_acc, "rows_accepted": int(b_added or 0),
                 "rows_failed": int(b_failed or 0),
                 "clean_failed": int(b_clean_failed or 0),
                 "adopted": b_adopt, "adopted_rows": int(b_adopt_rows or 0)}
        (accepts,) = conn.execute(
            f"SELECT COUNT(*) FROM events"
            f" WHERE kind = 'l3_accept_fix_server' AND {ev_day} >= ?",
            (since,)).fetchone()
        spend_by_day = {}
        calls_by_day = {}
        for d, model, i, o, c in conn.execute(
                "SELECT day, model, COALESCE(SUM(in_tokens),0),"
                " COALESCE(SUM(out_tokens),0), COUNT(*) FROM llm_calls"
                " WHERE day >= ? GROUP BY day, model", (since,)):
            pin, pout = _PRICES.get(model, (5.0, 25.0))
            spend_by_day[d] = spend_by_day.get(d, 0.0) +                 (i * pin + o * pout) / 1e6
            calls_by_day[d] = calls_by_day.get(d, 0) + c
        (calls_total, ok_total, in_tok, out_tok) = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(ok),0),"
            " COALESCE(SUM(in_tokens),0), COALESCE(SUM(out_tokens),0)"
            " FROM llm_calls WHERE day >= ?", (since,)).fetchone()
        return {"since": since, "range_days": range_days,
                "totals": {"active_machines": active,
                           "new_machines": new_machines,
                           "events": ev_total, "llm_calls": calls_total,
                           "ok_calls": ok_total, "in_tokens": in_tok,
                           "out_tokens": out_tok,
                           "est_usd": round(_est_usd_since(conn, since),
                                            2)},
                "active_by_day": active_by_day,
                "by_feature": features,
                "top_kinds": top_kinds,
                "l1": l1,
                "tests": tests,
                "l3": {"modeA_runs": a_runs,
                       "modeA_confirmed": int(a_confirmed or 0),
                       "modeA_cards": int(a_cards or 0),
                       "modeA_refused": a_refused,
                       "hint_targeting": hint,
                       "modeB_runs": b_runs, "modeB": modeB,
                       "fixes_accepted": accepts},
                "spend_by_day": [
                    {"day": d, "est_usd": round(v, 2),
                     "calls": calls_by_day.get(d, 0)}
                    for d, v in sorted(spend_by_day.items())]}
    finally:
        conn.close()


def _est_usd_since(conn, since: str) -> float:
    spend = 0.0
    for model, i, o in conn.execute(
            "SELECT model, COALESCE(SUM(in_tokens),0),"
            " COALESCE(SUM(out_tokens),0) FROM llm_calls WHERE day >= ?"
            " GROUP BY model", (since,)):
        pin, pout = _PRICES.get(model, (5.0, 25.0))
        spend += (i * pin + o * pout) / 1e6
    return spend


@app.get("/admin/research")
def admin_research(token: str | None = Query(default=None),
                   x_dlc_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        latest: dict[str, dict] = {}
        for iid, sid, ver, dec, has_sig, dat, ver_app in conn.execute(
                "SELECT install_id, study_id, version, decision,"
                " signature IS NOT NULL, decided_at, app_version"
                " FROM consents ORDER BY COALESCE(decided_at, received_at)"):
            latest[iid] = {"install_id": iid, "study_id": sid, "version": ver,
                           "decision": dec, "has_signature": bool(has_sig),
                           "decided_at": dat, "app_version": ver_app}
        rows = sorted(latest.values(),
                      key=lambda r: r.get("decided_at") or 0, reverse=True)
        agreed = sum(1 for r in rows if r["decision"] == "agreed")
        declined = sum(1 for r in rows if r["decision"] == "declined")
        (machines,) = conn.execute("SELECT COUNT(*) FROM machines").fetchone()
        helpful: dict[str, int] = {}
        answered: dict[str, int] = {}
        by_feature: dict[str, int] = {}
        comments: list[dict] = []
        n = 0
        for iid, ts, props in conn.execute(
                "SELECT install_id, COALESCE(client_ts, received_at), props"
                " FROM events WHERE kind = 'feedback_survey'"
                " ORDER BY COALESCE(client_ts, received_at) DESC"):
            try:
                p = json.loads(props)
            except (TypeError, ValueError):
                p = {}
            n += 1
            h, a = p.get("helpful"), p.get("answered")
            if h:
                helpful[h] = helpful.get(h, 0) + 1
            if a:
                answered[a] = answered.get(a, 0) + 1
            f = p.get("feature") or "?"
            by_feature[f] = by_feature.get(f, 0) + 1
            if p.get("comment") and len(comments) < 100:
                comments.append({"install_id": iid, "ts": ts, "feature": f,
                                 "helpful": h, "answered": a,
                                 "comment": str(p["comment"])[:300]})
        (shown,) = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'feedback_survey_shown'"
        ).fetchone()
        (skipped,) = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'feedback_survey_skipped'"
        ).fetchone()
        return {"study_id": _study_id(), "survey_rate": _survey_rate(),
                "consents": rows,
                "counts": {"agreed": agreed, "declined": declined,
                           "machines": machines,
                           "undecided": max(0, machines - agreed - declined)},
                "survey": {"responses": n, "shown": shown, "skipped": skipped,
                           "helpful": helpful, "answered": answered,
                           "by_feature": by_feature, "comments": comments}}
    finally:
        conn.close()


# exports

_TIME_COLS = {"ts", "client_ts", "stored_at", "received_at", "decided_at",
              "last_seen"}

_EXPORT_SQL = {
    "events": "SELECT * FROM events ORDER BY id",
    "machines": "SELECT * FROM machines ORDER BY first_seen",
    "llm_calls": "SELECT * FROM llm_calls ORDER BY id",
    # the drawn signature is not a CSV cell (Excel cuts cells at 32k
    # characters): the column names the PNG inside signatures.zip instead
    "consents": (
        "SELECT id, install_id, study_id, version AS sheet_version, decision,"
        " name, CASE WHEN signature IS NOT NULL AND signature != ''"
        " THEN 'sig_' || id || '.png' ELSE '' END AS signature_png,"
        " app_version, decided_at, received_at FROM consents ORDER BY id"),
    "surveys": (
        "SELECT install_id, COALESCE(client_ts, received_at) AS ts,"
        " json_extract(props, '$.feature') AS feature,"
        " json_extract(props, '$.filename') AS filename,"
        " json_extract(props, '$.helpful') AS helpful,"
        " json_extract(props, '$.answered') AS answered,"
        " json_extract(props, '$.comment') AS comment"
        " FROM events WHERE kind = 'feedback_survey' ORDER BY ts"),
}


def _tz():
    name = (os.environ.get("DLC_TIMEZONE", "") or "America/New_York").strip()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _tz_label() -> str:
    return datetime.now(_tz()).strftime("%Z") or "UTC"


def _today() -> date:
    return datetime.now(_tz()).date()


def _day_sql(col: str) -> str:
    """SQL for the course-zone calendar day of an epoch column."""
    off = datetime.now(_tz()).utcoffset()
    secs = int(off.total_seconds()) if off is not None else 0
    return f"date({col} + {secs}, 'unixepoch')"


def _fmt_time(v) -> str:
    """Epoch seconds -> '2026-10-05 01:31:24 EDT'; anything else unchanged."""
    if v is None or v == "":
        return ""
    try:
        return datetime.fromtimestamp(float(v), tz=_tz()).strftime(
            "%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(v)


def _csv_cell(v) -> str:
    s = "" if v is None else str(v)
    if any(ch in s for ch in ',"\n'):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _csv_download(filename: str, lines: list[str]) -> Response:
    body = "﻿" + "\n".join(lines) + "\n"
    return Response(content=body.encode("utf-8"),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


def _signature_png(data_url) -> bytes | None:
    """The drawn signature as PNG bytes, or None unless the stored value is a
    well-formed PNG data URL (nothing else is ever embedded in a page)."""
    prefix = "data:image/png;base64,"
    if not isinstance(data_url, str) or not data_url.startswith(prefix):
        return None
    try:
        raw = base64.b64decode(data_url[len(prefix):], validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if raw.startswith(b"\x89PNG\r\n\x1a\n") else None


@app.get("/admin/export.csv")
def export_csv(token: str | None = Query(default=None),
               x_dlc_admin_token: str | None = Header(default=None),
               table: str = Query(default="events")) -> Response:
    _check_admin(token, x_dlc_admin_token)
    sql = _EXPORT_SQL.get(table)
    if sql is None:
        raise HTTPException(status_code=400, detail="unknown table")
    conn = _db()
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        lines = [",".join(cols)]
        for row in cur:
            lines.append(",".join(
                _csv_cell(_fmt_time(v) if col in _TIME_COLS else v)
                for col, v in zip(cols, row)))
        return _csv_download(f"{table}.csv", lines)
    finally:
        conn.close()


@app.get("/admin/signatures.zip")
def admin_signatures_zip(token: str | None = Query(default=None),
                         x_dlc_admin_token: str | None = Header(default=None)
                         ) -> Response:
    """Every drawn signature as sig_<id>.png (the names consents.csv uses)
    plus index.csv: id, install_id, name, decided_at, file."""
    _check_admin(token, x_dlc_admin_token)
    buf = io.BytesIO()
    index = ["id,install_id,name,decided_at,file"]
    conn = _db()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for cid, iid, name, sig, dat in conn.execute(
                    "SELECT id, install_id, name, signature, decided_at"
                    " FROM consents WHERE signature IS NOT NULL"
                    " AND signature != '' ORDER BY id"):
                png = _signature_png(sig)
                if png is None:
                    continue
                fn = f"sig_{cid}.png"
                zf.writestr(fn, png)
                index.append(",".join(_csv_cell(x) for x in
                                      (cid, iid, name, _fmt_time(dat), fn)))
            zf.writestr("index.csv", "﻿" + "\n".join(index) + "\n")
    finally:
        conn.close()
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition":
                             'attachment; filename="signatures.zip"'})


_CONSENT_LOG_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Consent log — study __STUDY__</title>
<style>
 body{font:13px/1.45 system-ui,sans-serif;margin:24px;color:#111827}
 h1{font-size:18px;margin:0 0 4px}
 .muted{color:#6b7280}
 table{border-collapse:collapse;width:100%;margin-top:14px}
 th,td{border:1px solid #d1d5db;padding:6px 8px;text-align:left;
       vertical-align:middle;font-size:12.5px}
 th{background:#f3f4f6}
 img{height:60px;max-width:220px;background:#fff;display:block}
 code{font-size:12px}
 button{padding:6px 14px;margin-top:10px;cursor:pointer}
 @media print{button{display:none} body{margin:0}}
</style></head><body>
<h1>Digital Lab Coach — consent log, study __STUDY__</h1>
<div class="muted">__N__ decisions: __AGREED__ agreed, __DECLINED__ declined
 · generated __GENERATED__ · times in __TZ__ · every decision the course server received,
 oldest first; signatures as drawn in the tool</div>
<button onclick="window.print()">Print / save as PDF</button>
<table><thead><tr><th>#</th><th>decided</th><th>decision</th>
<th>typed name</th><th>machine</th><th>sheet</th><th>app</th><th>signature</th>
</tr></thead><tbody>
__ROWS__
</tbody></table></body></html>"""


@app.get("/admin/consents.html", response_class=HTMLResponse)
def admin_consent_log(token: str | None = Query(default=None),
                      x_dlc_admin_token: str | None = Header(default=None)
                      ) -> str:
    """A printable consent log for the study records: every decision with
    the typed name and the drawn signature inline. Print it to PDF."""
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, install_id, version, decision, name, signature,"
            " app_version, decided_at, received_at FROM consents"
            " ORDER BY COALESCE(decided_at, received_at), id").fetchall()
    finally:
        conn.close()
    trs = []
    for cid, iid, ver, dec, name, sig, appv, dat, rec in rows:
        png = _signature_png(sig)
        if png is not None:
            img = ('<img alt="signature" src="data:image/png;base64,'
                   + base64.b64encode(png).decode("ascii") + '">')
        else:
            img = '<span class="muted">typed only</span>' if dec == "agreed" else ""
        cells = (str(cid), _h(_fmt_time(dat if dat is not None else rec)),
                 _h(dec or ""), _h(name or ""),
                 f"<code>{_h((iid or '')[:8])}</code>", _h(ver or ""),
                 _h(appv or ""), img)
        trs.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    page = _CONSENT_LOG_PAGE
    for key, val in (("__STUDY__", _h(_study_id() or "none")),
                     ("__N__", str(len(rows))),
                     ("__AGREED__", str(sum(1 for r in rows if r[3] == "agreed"))),
                     ("__DECLINED__", str(sum(1 for r in rows if r[3] == "declined"))),
                     ("__GENERATED__", _fmt_time(time.time())),
                     ("__TZ__", _h(_tz_label())),
                     ("__ROWS__", "\n".join(trs) or
                      '<tr><td colspan="8" class="muted">no decisions recorded yet</td></tr>')):
        page = page.replace(key, val)
    return page


_ADMIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DLC course dashboard</title>
<link rel="icon" href="data:,">
<style>
 body{font:14px/1.45 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#111827}
 header{background:#111827;color:#f9fafb;padding:12px 20px;display:flex;
        justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
 header h1{font-size:16px;margin:0}
 main{max-width:1200px;margin:0 auto;padding:18px}
 .tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
 .tile{background:#fff;border:1px solid #e5e7eb;border-radius:8px;
       padding:10px 16px;min-width:120px}
 .tile b{display:block;font-size:20px}
 .tile span{color:#6b7280;font-size:12px}
 .warn{color:#b45309}
 h2{font-size:15px;margin:18px 0 8px}
 table{border-collapse:collapse;width:100%;background:#fff;
       border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}
 th,td{padding:6px 10px;border-bottom:1px solid #eef0f3;text-align:left;
       font-size:13px;vertical-align:top}
 th{background:#f3f4f6;color:#374151;white-space:nowrap}
 tr:last-child td{border-bottom:none}
 td.nw,th.nw{white-space:nowrap}
 #gate{max-width:420px;margin:80px auto;background:#fff;padding:24px;
       border:1px solid #e5e7eb;border-radius:10px}
 #gate input{width:100%;padding:8px;margin:10px 0;box-sizing:border-box}
 #gate button{padding:8px 16px}
 .err{color:#b91c1c}
 .muted{color:#6b7280}
 button.ghost{background:none;border:1px solid #6b7280;color:#f9fafb;
              border-radius:6px;padding:4px 10px;cursor:pointer}
 .tabs{display:flex;gap:6px;margin:4px 0 12px}
 .tabs button{padding:7px 16px;border:1px solid #d1d5db;background:#fff;
              border-radius:8px;cursor:pointer;font-weight:600;color:#374151}
 .tabs button.on{background:#111827;color:#fff;border-color:#111827}
 .filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;
          background:#fff;border:1px solid #e5e7eb;border-radius:8px;
          padding:10px 12px;margin:6px 0 10px}
 .filters select{padding:5px 8px;border:1px solid #d1d5db;border-radius:6px}
 .filters label{font-size:12px;color:#6b7280}
 .filters .push{margin-left:auto;display:flex;gap:10px;align-items:center}
 .filters button{padding:5px 12px;border:1px solid #d1d5db;background:#f9fafb;
                 border-radius:6px;cursor:pointer}
 .pager{display:flex;gap:6px;align-items:center;margin:10px 0;flex-wrap:wrap}
 .pager button{min-width:32px;padding:4px 8px;border:1px solid #d1d5db;
               background:#fff;border-radius:6px;cursor:pointer}
 .pager button.cur{background:#111827;color:#fff;border-color:#111827}
 .pager button:disabled{opacity:.4;cursor:default}
 details.props{display:inline}
 details.props summary{cursor:pointer;color:#2563eb;font-size:12px;
                       display:inline;margin-left:6px}
 details.props pre{background:#f3f4f6;border-radius:6px;padding:8px;
                   font-size:11.5px;white-space:pre-wrap;margin:6px 0 0}
 code.mid{background:#f3f4f6;border-radius:4px;padding:1px 5px;font-size:12px}
 .range{display:flex;gap:6px}
 .range button{padding:5px 14px;border:1px solid #d1d5db;background:#fff;
               border-radius:6px;cursor:pointer}
 .range button.on{background:#111827;color:#fff;border-color:#111827}
 .barrow{display:flex;align-items:center;gap:8px;margin:3px 0}
 .barrow .lbl{width:190px;font-size:12px;color:#374151;white-space:nowrap;
              overflow:hidden;text-overflow:ellipsis;text-align:right}
 .barrow .track{flex:1;background:#eef0f3;border-radius:4px;height:14px}
 .barrow .bar{background:#2563eb;height:14px;border-radius:4px;min-width:2px}
 .barrow .val{font-size:12px;color:#374151;white-space:nowrap}
 .cardbox{background:#fff;border:1px solid #e5e7eb;border-radius:8px;
          padding:12px 14px;margin-bottom:14px}
 #dbline{font-size:11.5px;color:#6b7280;margin:-6px 0 10px}
 .aitext{background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;
         padding:8px 10px;margin-top:6px;white-space:pre-wrap;
         font-size:12.5px;max-height:340px;overflow:auto}
 .aihead{font-size:12px;color:#6b7280;margin-bottom:2px}
 .pill{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:10px;
       padding:1px 8px;font-size:11.5px;margin-right:6px}
 .xbtns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
 .xbtns button{padding:6px 14px;border:1px solid #d1d5db;background:#fff;
               border-radius:6px;cursor:pointer;font-weight:600;color:#374151}
 .xbtns button:hover{background:#f3f4f6}
</style></head><body>
<header><h1>Digital Lab Coach — course dashboard</h1>
<button class="ghost" id="logout" style="display:none">forget token</button>
</header>
<main>
 <div id="gate">
   <h2 style="margin-top:0">Admin token</h2>
   <p class="muted">Stored only in this browser. Only the instructor
      holding DLC_ADMIN_TOKEN can load data.</p>
   <input id="tok" type="password" placeholder="admin token">
   <button id="go">Open dashboard</button>
   <p id="gate-err" class="err"></p>
 </div>
 <div id="dash" style="display:none">
   <div class="tiles" id="tiles"></div>
   <div class="tabs">
     <button id="tab-overview" class="on">Overview</button>
     <button id="tab-activity">Activity</button>
     <button id="tab-ai">AI outputs</button>
     <button id="tab-stats">Stats</button>
     <button id="tab-research">Research</button>
   </div>
   <div id="dbline"></div>
   <div class="filters">
     <label>Day <select id="f-day"><option value="">all</option></select></label>
     <label>Machine <select id="f-mach"><option value="">all</option></select></label>
     <label id="l-kind">Event <select id="f-kind"><option value="">all</option></select></label>
     <label id="l-feat" style="display:none">Feature <select id="f-feat"><option value="">all</option></select></label>
     <label>Rows <select id="f-pp">
       <option>10</option><option selected>25</option>
       <option>50</option><option>100</option></select></label>
     <span class="push">
       <label><input type="checkbox" id="auto"> auto-refresh 60s</label>
       <button id="refresh">Refresh</button>
     </span>
   </div>
   <section id="sec-overview">
     <h2>Per-day LLM usage <span class="muted">(Day / Machine filters apply)</span></h2>
     <div id="llm"></div>
     <h2>Machines</h2><div id="machines"></div>
   </section>
   <section id="sec-activity" style="display:none">
     <h2>Activity <span id="act-count" class="muted"></span></h2>
     <div id="activity"></div>
     <div class="pager" id="act-pager"></div>
   </section>
   <section id="sec-ai" style="display:none">
     <h2>AI outputs <span id="ai-count" class="muted"></span>
       <span class="muted" style="font-weight:normal">— every LLM reply the
       course server relayed (L2 summaries &amp; grades, Mode A, Mode B)</span></h2>
     <div id="ai"></div>
     <div class="pager" id="ai-pager"></div>
   </section>
   <section id="sec-stats" style="display:none">
     <div class="filters"><span class="muted">Window</span>
       <span class="range">
         <button id="rng-7" class="on">Last 7 days</button>
         <button id="rng-30">Last 30 days</button>
       </span>
       <span class="push"><span id="st-since" class="muted"></span></span>
     </div>
     <div class="tiles" id="st-tiles"></div>
     <div class="cardbox"><h2 style="margin-top:0">Daily activity</h2>
       <div id="st-daily"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">Layer 3 outcomes</h2>
       <div class="tiles" id="st-l3" style="margin-bottom:0"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">AI usage by feature</h2>
       <div id="st-feat"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">Most frequent events</h2>
       <div id="st-kinds"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">Spend by day</h2>
       <div id="st-spend"></div></div>
   </section>
   <section id="sec-research" style="display:none">
     <div class="tiles" id="rs-tiles"></div>
     <div class="cardbox"><h2 style="margin-top:0">Feedback survey
       <span class="muted" style="font-weight:normal">— consenting students only; each feature asks once the first time it answers, then at random</span></h2>
       <div class="tiles" id="rs-survey" style="margin-bottom:8px"></div>
       <div id="rs-comments"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">Consent decisions
       <span class="muted" style="font-weight:normal">— latest per machine; typed names and signatures are only in the downloads below</span></h2>
       <div id="rs-consents"></div>
       <div class="xbtns">
         <button id="rs-x-consents">&#11015; consents.csv</button>
         <button id="rs-x-sigs">&#11015; signatures.zip</button>
         <button id="rs-x-log">consent log (print / PDF)</button>
         <button id="rs-x-surveys">&#11015; surveys.csv</button>
       </div>
       <p class="muted" style="margin:8px 0 0">consents.csv: every decision with the typed name; its <code>signature_png</code> column names the drawing inside signatures.zip.
         The consent log shows the same rows with the signatures inline, ready to print. surveys.csv: one row per answered survey; blank cells mean that question was not part of it. Times are in the course time zone (DLC_TIMEZONE, default America/New_York).</p></div>
   </section>
 </div>
</main>
<script>
const $=(id)=>document.getElementById(id);
function tok(){try{return localStorage.getItem("dlc_admin_token")||""}catch(e){return ""}}
function setTok(v){try{localStorage.setItem("dlc_admin_token",v)}catch(e){}}
async function api(path){
  const r=await fetch(path,{headers:{"X-DLC-Admin-Token":tok()}});
  if(r.status===401)throw new Error("bad token");
  return r.json();
}
function esc(s){const d=document.createElement("div");
  d.textContent=String(s??"");return d.innerHTML}
function table(rows,cols){
  if(!rows||!rows.length)return '<p class="muted">nothing yet</p>';
  const h=cols.map(c=>`<th>${esc(c)}</th>`).join("");
  const b=rows.map(r=>"<tr>"+cols.map(c=>`<td>${r[c]??""}</td>`)
    .join("")+"</tr>").join("");
  return `<table><tr>${h}</tr>${b}</table>`;
}
const SKIP=new Set(["session_id","install_id","issued","id_source"]);
function kv(props){
  const parts=[];
  for(const [k,v] of Object.entries(props||{})){
    if(SKIP.has(k))continue;
    parts.push(`${k}=${typeof v==="object"?JSON.stringify(v):v}`);
  }
  return parts.join(", ");
}
function describe(kind,p){
  p=p||{};
  const fname=p.filename||p.file;
  const f=fname?`<code class="mid">${esc(fname)}</code>`:"";
  switch(kind){
    case "app_start": return `app started (v${esc(p.version||"?")})`;
    case "upload": return `uploaded ${p.count??"?"} file(s)`;
    case "tests_run_started": return `running tests on ${f} (${esc(p.mode||"")})`;
    case "tests_run_all_started": return "running ALL files' tests";
    case "l1_result":{
      if(p.failed)return `Layer 1 on ${f}: could not analyze (file failed to parse)`;
      const bits=[`${p.errors??0} error(s)`,`${p.warnings??0} warning(s)`];
      if(p.unsupported)bits.push("unsupported component");
      if(p.testcase_rows!=null)bits.push(`${p.testcase_rows} test row(s)`);
      const kinds=(p.kinds||[]).length?" — "+esc(p.kinds.join(", ")):"";
      return `Layer 1 on ${f}: ${bits.join(", ")}${kinds}`;}
    case "reupload_diff":{
      const hit=p.had_suspects
        ?(p.touched_suspect?"; the edit touched a named suspect":"; the edit missed the named suspects")
        :"";
      return `re-upload of ${f}: +${p.comps_added??0} / −${p.comps_removed??0} / ~${p.comps_changed??0} component(s), `+
        `${p.wires_changed??0} wire(s)${hit}`;}
    case "l3_locked":
      return `Mode A locked on ${f}: ${p.reason==="parse_failed"?"file failed to parse":`${p.errors??"?"} Layer-1 error(s) unresolved`}`;
    case "tests_run_complete":{
      if(p.ok===false)return `tests on ${f}: runner error`;
      if(p.all_passed)return `tests on ${f}: all rows passed`;
      const rows=p.failing_rows!=null?` (${p.failing_rows}${p.total_rows?"/"+p.total_rows:""} row(s) failing)`:"";
      return `tests on ${f}: some rows failed${rows}`;}
    case "tests_run_all_complete": return `tests finished ${kv(p)?"— "+esc(kv(p)):""}`;
    case "l2_llm_started": return "L2 summary requested";
    case "l2_llm_complete": return `L2 summary done ${p.model?"("+esc(p.model)+")":""}`;
    case "l3_modeA_started": return `Mode A started on ${f}`;
    case "l3_modeA_result_server":{
      const why={rom_mismatch:"refused: ROM contents differ",
                 lazy:"refused: lazy gate (too many failures)",
                 unsupported:"refused: unsupported circuit",
                 limited:"refused: daily cap",
                 clear:"nothing to debug, every row passes",
                 error:"error"}[p.mode||"analysis"];
      if(why)return `Mode A on ${f}: ${why}`;
      return `Mode A on ${f}: ${p.cards??0} card(s)`+
        `${p.confirmed?", confirmed &#10003;":""}, `+
        `${p.llm_calls??0} LLM call(s)${p.model?", "+esc(p.model):""}`;}
    case "l3_modeB_result_server":{
      const why={limited:"refused: daily cap",unsupported:"refused: unsupported circuit",error:"error"}[p.mode];
      if(why)return `Coverage Coach on ${f}: ${why}`;
      if(p.covered)return `Coverage Coach on ${f}: every category already covered, nothing to add${p.refunded?", use refunded":""}`;
      const shape=p.rows!=null
        ?`, ${p.rows} row(s)${p.disputed?`, ${p.disputed} disputed`:""}${p.rejected?`, ${p.rejected} rejected by the gates`:""}`
        :"";
      return `Coverage Coach on ${f}: ${p.proposals??0} proposal(s)${shape}${p.refunded?", use refunded":""}`;}
    case "l3_modeB_inject_outcome":{
      const n=p.added!=null
        ?`, ${p.failed??0} of ${p.added} row(s) failed${p.clean_failed?` (${p.clean_failed} not flagged as disputed)`:""}`
        :"";
      return `student ACCEPTED coach rows on ${f}: ${esc(p.outcome||"?")}${n}`;}
    case "l3_modeB_adopted_official":
      return `adopted the coach rows into the official tests for ${f}${p.rows!=null?` (${p.rows} rows total)`:""}`;
    case "l3_modeB_discard_failing":
      return `discarded the failing coach rows on ${f}${p.kept!=null?`, kept ${p.kept}`:""}`;
    case "l3_accept_fix_server":
    case "l3_fix_accepted": return `student ACCEPTED a fix ${f?("on "+f):""}`;
    case "l3_fix_animation_played": return "fix walkthrough animation played";
    case "l2_walkthrough_played": return `signal-flow walkthrough played on ${f} (${p.waves??"?"} waves)`;
    case "l3_fix_drillin_opened": return "opened a fix inside a subcircuit";
    case "l3_hint_level": return `hint level ${esc(p.level??"?")}`;
    case "l3_adopt_popup_shown": return "asked whether to adopt the proposed test rows";
    case "l3_adopt_popup_accepted": return "adopted the proposed test rows";
    case "l3_adopt_popup_declined": return "declined the proposed test rows";
    case "l3_acceptfail_popup_shown": return "told that the accepted fix still fails rows";
    case "l3_acceptfail_kept": return "kept the fix despite failing rows";
    case "l3_acceptfail_discarded": return "discarded the fix after failing rows";
    case "l3_circuit_re_uploaded":
      return `re-uploaded after a coach result${(p.files||[]).length?": "+esc(p.files.join(", ")):(f?" "+f:"")}`;
    case "l3_netids_toggled":
    case "netids_toggled": return "toggled net ids on the graph";
    case "l3_netref_flashed": return "flashed a net reference on the graph";
    case "tab_switch": return `switched to the ${esc(p.tab||"?")} tab`;
    case "settings_opened": return "opened settings";
    case "settings_guide_opened": return "opened the settings guide";
    case "settings_language": return `switched the interface language to ${esc(p.lang||"?")}`;
    case "settings_official_test_deleted": return `official test removed ${f}`;
    case "build_refused": return `Mode A refused on ${f}: ${esc(p.reason||kv(p)||"gate")}`;
    case "low_pass_rate":
    case "too_many_failures":
    case "scattered_failures":
    case "missing_clocked_logic":
    case "unbound_columns": return `Mode A gate: ${esc(kind.replace(/_/g," "))} on ${f}`;
    case "l3_modeA_row_viewed": return "viewed a failing-row analysis";
    case "settings_proxy_saved": return "connected to the course server";
    case "settings_proxy_cleared": return "disconnected from the course server";
    case "settings_official_test_saved": return `official test saved ${f}`;
    case "connect_gate_saved": return `first-run: connected to the course server (${esc(p.verify||"?")})`;
    case "connect_gate_skipped": return "first-run: skipped the course-server step";
    case "consent_shown": return `research consent sheet shown${p.version?` (sheet ${esc(p.version)})`:""}`;
    case "consent_agreed":
    case "consent_recorded": return `student AGREED to the research study${p.version?` (sheet ${esc(p.version)})`:""}`;
    case "feedback_survey_shown": return `feedback question shown after ${esc(p.feature||"?")}${f?" on "+f:""}`;
    case "feedback_survey_skipped": return `feedback question skipped (${esc(p.feature||"?")})`;
    case "feedback_survey_timeout": return `feedback question closed unanswered (${esc(p.feature||"?")})`;
    case "feedback_survey":
      return `feedback on ${esc(p.feature||"?")}${f?" for "+f:""}: helpful=${esc(p.helpful||"–")}, answered=${esc(p.answered||"–")}${p.comment?` — “${esc(String(p.comment).slice(0,120))}”`:""}`;
    default: {
      const t=kv(p);
      return esc(kind)+(t?` — ${esc(t.length>150?t.slice(0,150)+"…":t)}`:"");
    }
  }
}
function renderAiText(feature,text){
  try{
    const o=JSON.parse(text);
    if(o&&o.fix&&o.fix.explanation_for_student){
      const ops=(o.fix.ops||[]).length;
      const why=o.hint&&o.hint.why?o.hint.why:"";
      return `<div class="aihead">Mode A hypothesis — confidence `+
        `${esc(o.confidence??"?")}, ${ops} op(s)</div>`+
        `<div class="aitext"><b>why:</b> ${esc(why)}

`+
        `<b>student explanation:</b> ${esc(o.fix.explanation_for_student)}`+
        `

<b>ops:</b> ${esc(JSON.stringify(o.fix.ops))}</div>`;
    }
    if(o&&Array.isArray(o.proposals)){
      const t=o.proposals.map((pr,i)=>`#${i+1} `+
        esc(JSON.stringify(pr)).slice(0,300)).join(`
`);
      return `<div class="aihead">Mode B — ${o.proposals.length} `+
        `proposal(s)</div><div class="aitext">${t}</div>`;
    }
    if(o&&typeof o==="object")
      return `<div class="aitext">${esc(JSON.stringify(o,null,1))}</div>`;
  }catch(e){}
  return `<div class="aitext">${esc(text)}</div>`;
}
const state={tab:"overview",actPage:1,aiPage:1,range:7};
function filters(){
  return {day:$("f-day").value, mach:$("f-mach").value,
          kind:$("f-kind").value, feat:$("f-feat").value,
          pp:parseInt($("f-pp").value,10)};
}
function fill(sel,values){
  const cur=sel.value;
  sel.innerHTML='<option value="">all</option>'+
    values.map(v=>`<option${v===cur?" selected":""}>${esc(v)}</option>`).join("");
}
function pager(el,total,page,pp,go){
  const n=Math.max(1,Math.ceil(total/pp));
  const btn=(p,label,cur,dis)=>`<button data-p="${p}"
    ${dis?"disabled":""} class="${cur?"cur":""}">${label}</button>`;
  const parts=[btn(page-1,"&laquo;",false,page<=1)];
  const win=new Set([1,2,n-1,n,page-1,page,page+1]);
  let last=0;
  for(let p=1;p<=n;p++){
    if(!win.has(p))continue;
    if(p-last>1)parts.push('<span class="muted">…</span>');
    parts.push(btn(p,p,p===page,false));last=p;
  }
  parts.push(btn(page+1,"&raquo;",false,page>=n));
  el.innerHTML=parts.join("");
  el.querySelectorAll("button[data-p]").forEach(b=>b.onclick=()=>{
    go(parseInt(b.dataset.p,10));});
}
async function loadActivity(){
  const f=filters();
  const q=new URLSearchParams();
  if(f.day)q.set("day",f.day);
  if(f.mach)q.set("install_id",f.mach);
  if(f.kind)q.set("kind",f.kind);
  q.set("page",state.actPage);q.set("per_page",f.pp);
  const d=await api("/admin/events?"+q.toString());
  fill($("f-day"),d.days);fill($("f-mach"),d.machines);
  fill($("f-kind"),d.kinds);
  $("act-count").textContent=`${d.total} event(s)`;
  $("activity").innerHTML=d.rows.length?`<table>
    <tr><th class="nw">time</th><th class="nw">machine</th><th>what happened</th></tr>`+
    d.rows.map(r=>`<tr><td class="nw">${esc(r.time)}</td>
      <td class="nw" title="${esc(r.install_id)}"><code class="mid">${esc(r.install_id.slice(0,8))}</code></td>
      <td>${describe(r.kind,r.props)}
        <details class="props"><summary>raw</summary>
        <pre>${esc(JSON.stringify({kind:r.kind,...r.props},null,1))}</pre>
        </details></td></tr>`).join("")+"</table>"
    :'<p class="muted">no events match these filters</p>';
  pager($("act-pager"),d.total,d.page,d.per_page,
        p=>{state.actPage=p;loadActivity();});
}
async function loadAi(){
  const f=filters();
  const q=new URLSearchParams();
  if(f.day)q.set("day",f.day);
  if(f.mach)q.set("install_id",f.mach);
  if(f.feat)q.set("feature",f.feat);
  q.set("page",state.aiPage);q.set("per_page",f.pp);
  const d=await api("/admin/llm_texts?"+q.toString());
  fill($("f-day"),d.days);fill($("f-mach"),d.machines);
  fill($("f-feat"),d.features);
  $("ai-count").textContent=`${d.total} repl${d.total===1?"y":"ies"}`;
  $("ai").innerHTML=d.rows.length?`<table>
    <tr><th class="nw">time</th><th class="nw">machine</th><th>reply</th></tr>`+
    d.rows.map(r=>`<tr><td class="nw">${esc(r.time)}</td>
      <td class="nw" title="${esc(r.install_id)}"><code class="mid">${esc(r.install_id.slice(0,8))}</code></td>
      <td><span class="pill">${esc(r.feature)}</span>`+
      `<span class="muted">${esc(r.model||"")}, ${r.in_tokens??0}&rarr;${r.out_tokens??0} tok, ${Math.round((r.ms||0)/1000)}s</span>`+
      renderAiText(r.feature,r.response||"")+
      `</td></tr>`).join("")+"</table>"
    :'<p class="muted">no AI replies match these filters</p>';
  pager($("ai-pager"),d.total,d.page,d.per_page,
        p=>{state.aiPage=p;loadAi();});
}
function bars(el,rows,lbl,val,txt){
  const mx=Math.max(1,...rows.map(val));
  el.innerHTML=rows.length?rows.map(r=>`<div class="barrow">
    <span class="lbl" title="${esc(lbl(r))}">${esc(lbl(r))}</span>
    <span class="track"><span class="bar" style="width:${
      Math.round(100*val(r)/mx)}%"></span></span>
    <span class="val">${txt(r)}</span></div>`).join("")
    :'<p class="muted">nothing in this window</p>';
}
async function loadStats(){
  const d=await api("/admin/stats?range_days="+state.range);
  $("st-since").textContent=`since ${d.since}`;
  const t=d.totals;
  const okPct=t.llm_calls?Math.round(100*t.ok_calls/t.llm_calls):100;
  const l1=d.l1||{},te=d.tests||{};
  const errPct=l1.files?Math.round(100*(l1.with_errors||0)/l1.files):0;
  const passPct=te.runs?Math.round(100*(te.all_passed||0)/te.runs):0;
  $("st-tiles").innerHTML=[
    [t.active_machines,"active machines"],
    [t.new_machines,"new machines"],
    [t.events,"events"],
    [`${t.llm_calls} (${okPct}% ok)`,"AI calls"],
    [`${t.in_tokens}→${t.out_tokens}`,"tokens in→out"],
    ["$"+t.est_usd,"spend (est)"],
    [l1.files??0,"files analysed (Layer 1)"],
    [`${l1.with_errors??0} (${errPct}%)`,"with structural errors"],
    [`${l1.unsupported??0} / ${l1.failed??0}`,"unsupported / failed to parse"],
    [l1.avg_test_rows??"–","avg test rows per file"],
    [`${te.runs??0} (${passPct}% all-pass)`,"test runs"],
    [te.avg_failing_rows??"–","avg failing rows per run"],
  ].map(([v,l])=>`<div class="tile"><b>${v}</b><span>${l}</span></div>`).join("");
  bars($("st-daily"),d.active_by_day,r=>r.day,r=>r.events,
       r=>`${r.events} events · ${r.machines} machine(s)`);
  const l3=d.l3;
  const confPct=l3.modeA_runs?Math.round(100*l3.modeA_confirmed/l3.modeA_runs):0;
  const ref=l3.modeA_refused||{};
  const refTotal=Object.values(ref).reduce((a,b)=>a+b,0);
  const refDetail=Object.entries(ref).map(([k,v])=>`${esc(k)} ${v}`).join(", ");
  const h=l3.hint_targeting||{};
  const b=l3.modeB||{};
  $("st-l3").innerHTML=[
    [l3.modeA_runs,"Mode A analyses"],
    [`${l3.modeA_confirmed} (${confPct}%)`,"confirmed fixes"],
    [l3.modeA_cards,"fix cards shown"],
    [refTotal,"Mode A refused"+(refDetail?` (${refDetail})`:"")],
    [`${h.touched_suspect??0} / ${h.reuploads??0}`,"re-uploads whose edit hit a named suspect"],
    [l3.fixes_accepted,"fixes accepted"],
    [`${l3.modeB_runs}${b.covered?` (${b.covered} already complete)`:""}`,"Mode B runs"],
    [`${b.rows??0} (${b.disputed??0} disputed)`,"coach rows delivered"],
    [b.rejected??0,"coach rows rejected by the gates"],
    [`${b.rows_failed??0} / ${b.rows_accepted??0}`,"accepted coach rows that failed"+(b.clean_failed?` (${b.clean_failed} not flagged)`:"")],
    [`${b.adopted_rows??0} in ${b.adopted??0}`,"coach rows adopted into official tests"],
  ].map(([v,l])=>`<div class="tile"><b>${v}</b><span>${l}</span></div>`).join("");
  $("st-feat").innerHTML=table(d.by_feature.map(f=>({...f,
    est_usd:"$"+f.est_usd})),
    ["feature","calls","ok_calls","in_tokens","out_tokens","est_usd"]);
  bars($("st-kinds"),d.top_kinds,r=>r.kind,r=>r.n,r=>r.n);
  $("st-spend").innerHTML=table(d.spend_by_day.map(r=>({...r,
    est_usd:"$"+r.est_usd})),["day","calls","est_usd"]);
}
async function loadAggregates(){
  const f=filters();
  const health=await fetch("/v1/health").then(r=>r.json());
  if(health.db_path)$("dbline").textContent="serving database: "+health.db_path;
  const s=await api("/admin/summary");
  const d=await api("/admin/daily");
  const cap=health.today_calls>=health.global_daily_calls||
            health.today_est_usd>=health.global_daily_usd;
  $("tiles").innerHTML=[
   [s.machines.length,"machines"],[health.events,"events"],
   [health.today_calls+" / "+health.global_daily_calls,"LLM calls today"],
   ["$"+health.today_est_usd+" / $"+health.global_daily_usd,"spend today (est)"],
   ["$"+s.llm_spend_est_usd,"spend all-time (est)"],
   [cap?"TRIPPED":"ok","capacity breaker",cap],
   [health.key_format_ok?"ok":"CHECK KEY","API key",!health.key_format_ok],
  ].map(([v,l,warn])=>`<div class="tile"><b class="${warn?"warn":""}">${v}</b><span>${l}</span></div>`).join("");
  const spend={};for(const s2 of d.spend_by_day)spend[s2.day]=s2.est_usd;
  const llmRows=d.llm
    .filter(r=>(!f.day||r.day===f.day)&&(!f.mach||r.install_id===f.mach))
    .map(r=>({...r,install_id:`<code class="mid">${esc(r.install_id.slice(0,8))}</code>`,
      est_day_usd:spend[r.day]!==undefined?("$"+spend[r.day]):""}));
  $("llm").innerHTML=table(llmRows,
   ["day","install_id","feature","calls","ok_calls","in_tokens",
    "out_tokens","est_day_usd"]);
  $("machines").innerHTML=table(s.machines,
   ["install_id","first_seen","last_seen","id_source","app_version",
    "events","llm_calls"]);
}
async function loadResearch(){
  const d=await api("/admin/research");
  const c=d.counts||{};
  const fmt=(t)=>t?new Date(t*1000).toLocaleString():"";
  $("rs-tiles").innerHTML=[
    [d.study_id||"none","study id (DLC_STUDY_ID)",!d.study_id],
    [c.agreed??0,"agreed"],[c.declined??0,"declined"],
    [c.undecided??0,"machines with no decision yet"],
    [Math.round((d.survey_rate||0)*100)+"%","survey chance per coach answer"],
  ].map(([v,l,warn])=>`<div class="tile"><b class="${warn?"warn":""}">${esc(String(v))}</b><span>${l}</span></div>`).join("");
  const s=d.survey||{};
  const tally=(o,keys)=>keys.map(k=>`${k} ${o[k]||0}`).join(" · ");
  $("rs-survey").innerHTML=[
    [s.responses??0,"answers"+(s.shown?` (${s.shown} shown, ${s.skipped||0} skipped)`:"")],
    [tally(s.helpful||{},["yes","somewhat","no"]),"was this feedback helpful?"],
    [tally(s.answered||{},["yes","partially","no"]),"did it answer the question?"],
    [Object.entries(s.by_feature||{}).map(([k,v])=>`${k} ${v}`).join(" · ")||"–","by feature"],
  ].map(([v,l])=>`<div class="tile"><b style="font-size:15px">${esc(String(v))}</b><span>${l}</span></div>`).join("");
  $("rs-comments").innerHTML=(s.comments||[]).length
    ?table((s.comments||[]).map(r=>({when:fmt(r.ts),machine:`<code class="mid">${esc((r.install_id||"").slice(0,8))}</code>`,
        feature:esc(r.feature||""),helpful:esc(r.helpful||""),answered:esc(r.answered||""),comment:esc(r.comment||"")})),
        ["when","machine","feature","helpful","answered","comment"])
    :`<p class="muted">no free-text comments yet</p>`;
  $("rs-consents").innerHTML=(d.consents||[]).length
    ?table((d.consents||[]).map(r=>({machine:`<code class="mid">${esc((r.install_id||"").slice(0,8))}</code>`,
        decision:r.decision==="agreed"?"agreed":"<span class=warn>declined</span>",when:fmt(r.decided_at),
        sheet:esc(r.version||""),signature:r.has_signature?"drawn":"typed only",app:esc(r.app_version||"")})),
        ["machine","decision","when","sheet","signature","app"])
    :`<p class="muted">no decisions recorded yet</p>`;
  $("rs-x-consents").onclick=()=>download("/admin/export.csv?table=consents","consents.csv");
  $("rs-x-surveys").onclick=()=>download("/admin/export.csv?table=surveys","surveys.csv");
  $("rs-x-sigs").onclick=()=>download("/admin/signatures.zip","signatures.zip");
  $("rs-x-log").onclick=()=>window.open("/admin/consents.html?token="+encodeURIComponent(tok()),"_blank");
}
async function download(path,filename){
  const r=await fetch(path,{headers:{"X-DLC-Admin-Token":tok()}});
  if(!r.ok){alert("download failed: HTTP "+r.status);return}
  const u=URL.createObjectURL(await r.blob());
  const a=document.createElement("a");a.href=u;a.download=filename;
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(u),10000);
}
function showTab(t){
  state.tab=t;
  for(const x of ["overview","activity","ai","stats","research"]){
    $("sec-"+x).style.display=x===t?"":"none";
    $("tab-"+x).classList.toggle("on",x===t);
  }
  $("l-kind").style.display=t==="activity"?"":"none";
  $("l-feat").style.display=t==="ai"?"":"none";
  for(const id of ["f-day","f-mach","f-pp"])
    $(id).parentElement.style.display=(t==="stats"||t==="research")?"none":"";
  reloadTab();
}
function reloadTab(){
  if(state.tab==="overview")loadAggregates();
  else if(state.tab==="activity")loadActivity();
  else if(state.tab==="ai")loadAi();
  else if(state.tab==="research")loadResearch();
  else loadStats();
}
function setRange(n){
  state.range=n;
  $("rng-7").classList.toggle("on",n===7);
  $("rng-30").classList.toggle("on",n===30);
  loadStats();
}
async function load(){
  await loadAggregates();
  await loadActivity();
  await loadAi();
  $("gate").style.display="none";$("dash").style.display="block";
  $("logout").style.display="inline-block";
  showTab("overview");
}
["f-day","f-mach","f-kind","f-feat","f-pp"].forEach(id=>{
  $(id).addEventListener("change",()=>{state.actPage=1;state.aiPage=1;
    reloadTab();});
});
$("refresh").onclick=()=>reloadTab();
let autoTimer=null;
$("auto").addEventListener("change",()=>{
  if($("auto").checked)autoTimer=setInterval(reloadTab,60000);
  else{clearInterval(autoTimer);autoTimer=null;}
});
$("tab-overview").onclick=()=>showTab("overview");
$("tab-activity").onclick=()=>showTab("activity");
$("tab-ai").onclick=()=>showTab("ai");
$("tab-stats").onclick=()=>showTab("stats");
$("tab-research").onclick=()=>showTab("research");
$("rng-7").onclick=()=>setRange(7);
$("rng-30").onclick=()=>setRange(30);
$("go").onclick=async()=>{setTok($("tok").value.trim());
  try{await load()}catch(e){$("gate-err").textContent=
    "That token was rejected — check it and try again."}};
$("logout").onclick=()=>{setTok("");location.reload()};
if(tok()){load().catch(()=>{$("gate").style.display="block"})}
</script></body></html>"""


@app.get("/admin/view")
def admin_view():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_ADMIN_PAGE)
