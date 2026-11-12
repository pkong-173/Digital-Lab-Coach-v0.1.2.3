"""
DLC course proxy: key custody + machine-keyed limits.

One small server the instructor user runs; students' tools point at it via
`proxy_url` in their config.

It keeps one row per AI request: the machine code (a hash), the time, the
feature, the model, whether the call worked, the token counts, the duration
and, for a failed call, the provider's error message. The daily limits and
the spend cap are computed from those rows. Prompts and answers are never
stored.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app):
    _startup_sanity()
    _db().close()
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


def _db_path() -> Path:
    return Path(os.environ.get("DLC_PROXY_DB", "dlc_proxy.db"))


def _db() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA secure_delete = ON")
    conn.executescript(_SCHEMA)
    return conn


def _check_course_token(tok: str | None) -> None:
    want = os.environ.get("DLC_COURSE_TOKEN")
    if not want:
        raise HTTPException(
            status_code=503,
            detail="course server not configured: DLC_COURSE_TOKEN is unset")
    if (tok or "") != want:
        raise HTTPException(status_code=401, detail="bad course token")


def _check_admin(header_token: str | None) -> None:
    """The admin token is read from the X-DLC-Admin-Token header only: a
    token in the web address would land in server logs and browser history."""
    want = os.environ.get("DLC_ADMIN_TOKEN")
    if not want or (header_token or "") != want:
        raise HTTPException(status_code=401, detail="bad admin token")


def _est_usd(conn, day: str | None = None, since: str | None = None) -> float:
    q = ("SELECT model, COALESCE(SUM(in_tokens),0),"
         " COALESCE(SUM(out_tokens),0) FROM llm_calls")
    args: tuple = ()
    if day:
        q += " WHERE day = ?"
        args = (day,)
    elif since:
        q += " WHERE day >= ?"
        args = (since,)
    q += " GROUP BY model"
    spend = 0.0
    for model, i, o in conn.execute(q, args):
        pin, pout = _PRICES.get(model, (5.0, 25.0))
        spend += (i * pin + o * pout) / 1e6
    return spend


def _touch_machine(conn, install_id: str) -> None:
    now = time.time()
    with conn:
        conn.execute(
            "INSERT INTO machines (install_id, first_seen, last_seen)"
            " VALUES (?,?,?)"
            " ON CONFLICT(install_id) DO UPDATE SET"
            " last_seen = excluded.last_seen",
            (install_id, _today().isoformat(), now))


class MachineIn(BaseModel):
    install_id: str


class LlmIn(BaseModel):
    install_id: str
    feature: str = "other"
    model: str
    prompt: str
    system: str | None = None
    max_tokens: int = 3000
    effort: str | None = None


@app.post("/v1/events")
def check_in(req: MachineIn,
             x_dlc_token: str | None = Header(default=None)) -> dict:
    """The app checks its course token here. Only the machine code is read,
    to note the day it was first seen; anything else in the request is
    ignored."""
    _check_course_token(x_dlc_token)
    if not req.install_id:
        raise HTTPException(status_code=400, detail="install_id required")
    conn = _db()
    try:
        _touch_machine(conn, req.install_id)
        return {"ok": True, "stored": 0}
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
                " out_tokens = ?, ms = ?, error = ? WHERE id = ?",
                (time.time(), 1 if r.get("ok") else 0,
                 u.get("input_tokens"), u.get("output_tokens"), ms,
                 (r.get("error") or "")[:300] or None, row_id))
        r["limit"] = {"feature": req.feature, "used": used + 1,
                      "budget": budget}
        return r
    finally:
        conn.close()


@app.post("/v1/{name}")
def retired(x_dlc_token: str | None = Header(default=None)) -> dict:
    """Any other POST under /v1. Earlier app versions send more than this
    server takes: the token is checked, the body is neither read nor stored."""
    _check_course_token(x_dlc_token)
    return {"ok": True, "stored": 0}


def _effective_key() -> str:
    from dlc.llm.client import get_api_key
    return get_api_key("anthropic") or ""


def _startup_sanity() -> None:
    key = _effective_key()
    if not key:
        print("WARNING: no API key - set ANTHROPIC_API_KEY in this terminal"
              " window and start the proxy again. Until then every AI"
              " request answers 'the course server has no API key'.")
    elif not key.startswith("sk-"):
        print("WARNING: ANTHROPIC_API_KEY does not look like a real key"
              " (expected it to start with 'sk-'). LLM relays will fail"
              " with 401 until it is fixed.")
    if not os.environ.get("DLC_COURSE_TOKEN"):
        print("WARNING: DLC_COURSE_TOKEN is not set - every student request"
              " is refused (503) until it is. Set it in THIS terminal window"
              " and start the proxy again.")
    if not os.environ.get("DLC_ADMIN_TOKEN"):
        print("WARNING: DLC_ADMIN_TOKEN is not set - the admin dashboard"
              " rejects every token until it is.")


@app.get("/v1/health")
def health() -> dict:
    conn = _db()
    try:
        (m,) = conn.execute("SELECT COUNT(*) FROM machines").fetchone()
        day = _today().isoformat()
        (day_calls,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE day = ?",
            (day,)).fetchone()
        key = _effective_key()
        return {"ok": True, "machines": m,
                "db_path": str(_db_path().resolve()),
                "key_configured": bool(key),
                "key_format_ok": key.startswith("sk-") if key else False,
                "course_token_set": bool(os.environ.get("DLC_COURSE_TOKEN")),
                "admin_token_set": bool(os.environ.get("DLC_ADMIN_TOKEN")),
                "today_calls": day_calls,
                "today_est_usd": round(_est_usd(conn, day), 2),
                "global_daily_calls": _global_daily_calls(),
                "global_daily_usd": _global_daily_usd()}
    finally:
        conn.close()


@app.get("/admin/summary")
def summary(x_dlc_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(x_dlc_admin_token)
    conn = _db()
    try:
        machines = [dict(zip(
            ("install_id", "first_seen", "last_seen", "llm_calls"), row))
            for row in conn.execute(
                "SELECT m.install_id, m.first_seen, m.last_seen,"
                " (SELECT COUNT(*) FROM llm_calls c"
                "  WHERE c.install_id = m.install_id)"
                " FROM machines m ORDER BY m.first_seen, m.install_id")]
        for m in machines:
            m["last_seen"] = _fmt_time(m["last_seen"])
        return {"machines": machines,
                "llm_spend_est_usd": round(_est_usd(conn), 2)}
    finally:
        conn.close()


@app.get("/admin/daily")
def daily(x_dlc_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(x_dlc_admin_token)
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
        return {"llm": llm,
                "spend_by_day": [{"day": d, "est_usd": round(v, 2)}
                                 for d, v in sorted(spend.items(),
                                                    reverse=True)]}
    finally:
        conn.close()


@app.get("/admin/stats")
def admin_stats(x_dlc_admin_token: str | None = Header(default=None),
                range_days: int = Query(default=7, ge=1,
                                        le=366)) -> dict:
    _check_admin(x_dlc_admin_token)
    from datetime import timedelta
    since = (_today() - timedelta(days=range_days - 1)).isoformat()
    conn = _db()
    try:
        active_by_day = [dict(zip(("day", "machines", "calls"), r))
                         for r in conn.execute(
            "SELECT day, COUNT(DISTINCT install_id), COUNT(*)"
            " FROM llm_calls WHERE day >= ? GROUP BY day ORDER BY day",
            (since,))]
        (active, calls_total, ok_total, in_tok, out_tok) = conn.execute(
            "SELECT COUNT(DISTINCT install_id), COUNT(*),"
            " COALESCE(SUM(ok),0), COALESCE(SUM(in_tokens),0),"
            " COALESCE(SUM(out_tokens),0)"
            " FROM llm_calls WHERE day >= ?", (since,)).fetchone()
        (new_machines,) = conn.execute(
            "SELECT COUNT(*) FROM machines WHERE first_seen >= ?",
            (since,)).fetchone()
        by_feature = {}
        spend_by_feature = {}
        spend_by_day = {}
        calls_by_day = {}
        for d, feat, model, calls, ok, i, o in conn.execute(
                "SELECT day, feature, model, COUNT(*), COALESCE(SUM(ok),0),"
                " COALESCE(SUM(in_tokens),0), COALESCE(SUM(out_tokens),0)"
                " FROM llm_calls WHERE day >= ?"
                " GROUP BY day, feature, model", (since,)):
            f = by_feature.setdefault(feat, {"feature": feat, "calls": 0,
                                             "ok_calls": 0,
                                             "in_tokens": 0,
                                             "out_tokens": 0})
            f["calls"] += calls
            f["ok_calls"] += ok
            f["in_tokens"] += i
            f["out_tokens"] += o
            pin, pout = _PRICES.get(model, (5.0, 25.0))
            usd = (i * pin + o * pout) / 1e6
            spend_by_feature[feat] = spend_by_feature.get(feat, 0.0) + usd
            spend_by_day[d] = spend_by_day.get(d, 0.0) + usd
            calls_by_day[d] = calls_by_day.get(d, 0) + calls
        features = []
        for feat, f in sorted(by_feature.items()):
            f["est_usd"] = round(spend_by_feature.get(feat, 0.0), 2)
            features.append(f)
        return {"since": since, "range_days": range_days,
                "totals": {"active_machines": active,
                           "new_machines": new_machines,
                           "llm_calls": calls_total,
                           "ok_calls": ok_total, "in_tokens": in_tok,
                           "out_tokens": out_tok,
                           "est_usd": round(_est_usd(conn, since=since),
                                            2)},
                "active_by_day": active_by_day,
                "by_feature": features,
                "spend_by_day": [
                    {"day": d, "est_usd": round(v, 2),
                     "calls": calls_by_day.get(d, 0)}
                    for d, v in sorted(spend_by_day.items())]}
    finally:
        conn.close()


# exports

_TIME_COLS = {"last_seen"}

_EXPORT_SQL = {
    "machines": ("SELECT install_id, first_seen, last_seen FROM machines"
                 " ORDER BY first_seen, install_id"),
    "daily": ("SELECT day, install_id, feature, COUNT(*) AS calls,"
              " COALESCE(SUM(ok),0) AS ok_calls,"
              " COALESCE(SUM(in_tokens),0) AS in_tokens,"
              " COALESCE(SUM(out_tokens),0) AS out_tokens FROM llm_calls"
              " GROUP BY day, install_id, feature"
              " ORDER BY day DESC, install_id, feature"),
}


def _tz():
    name = (os.environ.get("DLC_TIMEZONE", "") or "America/New_York").strip()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _today() -> date:
    return datetime.now(_tz()).date()


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
    if isinstance(v, str) and s[:1] in ("=", "+", "-", "@"):
        s = "'" + s
    if any(ch in s for ch in ',"\r\n'):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _csv_download(filename: str, lines: list[str]) -> Response:
    body = "﻿" + "\n".join(lines) + "\n"
    return Response(content=body.encode("utf-8"),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


@app.get("/admin/export.csv")
def export_csv(x_dlc_admin_token: str | None = Header(default=None),
               table: str = Query(default="daily")) -> Response:
    _check_admin(x_dlc_admin_token)
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
 .range{display:flex;gap:6px}
 .range button{padding:5px 14px;border:1px solid #d1d5db;background:#fff;
               border-radius:6px;cursor:pointer}
 .range button.on{background:#111827;color:#fff;border-color:#111827}
 .barrow{display:flex;align-items:center;gap:8px;margin:3px 0}
 .barrow .lbl{width:190px;font-size:12px;color:#374151;white-space:nowrap;
              overflow:hidden;text-overflow:ellipsis;text-align:right}
 .barrow .track{flex:1;background:#eef0f3;border-radius:4px;height:14px}
 .barrow .bar{display:block;background:#2563eb;height:14px;border-radius:4px;min-width:2px}
 .barrow .val{font-size:12px;color:#374151;white-space:nowrap}
 .cardbox{background:#fff;border:1px solid #e5e7eb;border-radius:8px;
          padding:12px 14px;margin-bottom:14px}
 #dbline{font-size:11.5px;color:#6b7280;margin:-6px 0 10px}
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
     <button id="tab-stats">Stats</button>
   </div>
   <div id="dbline"></div>
   <div class="filters">
     <label id="l-day">Day <select id="f-day"><option value="">all</option></select></label>
     <label id="l-mach">Machine <select id="f-mach"><option value="">all</option></select></label>
     <span class="push">
       <label><input type="checkbox" id="auto"> auto-refresh 60s</label>
       <button id="refresh">Refresh</button>
     </span>
   </div>
   <section id="sec-overview">
     <p class="muted">This server keeps one row per AI request: the machine
       code (a hash), the time, the feature, the model, the token counts and,
       for a failed call, the provider's error message. It keeps no prompts
       and no answers.</p>
     <h2>Per-day AI usage <span class="muted">(Day / Machine filters apply)</span></h2>
     <div id="llm"></div>
     <h2>Machines</h2><div id="machines"></div>
     <div class="xbtns">
       <button id="x-calls">&#11015; usage_by_day.csv</button>
       <button id="x-machines">&#11015; machines.csv</button>
     </div>
     <p class="muted">Times are in the course time zone (DLC_TIMEZONE, default America/New_York).</p>
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
     <div class="cardbox"><h2 style="margin-top:0">AI requests by day</h2>
       <div id="st-daily"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">AI usage by feature</h2>
       <div id="st-feat"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">Spend by day</h2>
       <div id="st-spend"></div></div>
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
  const b=rows.map(r=>"<tr>"+cols.map(c=>`<td>${esc(r[c]??"")}</td>`)
    .join("")+"</tr>").join("");
  return `<table><tr>${h}</tr>${b}</table>`;
}
function tiles(el,items){
  el.innerHTML=items.map(([v,l,warn])=>`<div class="tile">
    <b class="${warn?"warn":""}">${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
}
const state={tab:"overview",range:7};
function fill(sel,values){
  const cur=sel.value;
  sel.innerHTML='<option value="">all</option>'+
    values.map(v=>`<option${v===cur?" selected":""}>${esc(v)}</option>`).join("");
}
function bars(el,rows,lbl,val,txt){
  const mx=Math.max(1,...rows.map(val));
  el.innerHTML=rows.length?rows.map(r=>`<div class="barrow">
    <span class="lbl" title="${esc(lbl(r))}">${esc(lbl(r))}</span>
    <span class="track"><span class="bar" style="width:${
      Math.round(100*val(r)/mx)}%"></span></span>
    <span class="val">${esc(txt(r))}</span></div>`).join("")
    :'<p class="muted">nothing in this window</p>';
}
async function loadOverview(){
  const health=await fetch("/v1/health").then(r=>r.json());
  const s=await api("/admin/summary");
  const d=await api("/admin/daily");
  if(health.db_path)$("dbline").textContent="serving database: "+health.db_path;
  const cap=health.today_calls>=health.global_daily_calls||
            health.today_est_usd>=health.global_daily_usd;
  tiles($("tiles"),[
   [s.machines.length,"machines"],
   [health.today_calls+" / "+health.global_daily_calls,"AI calls today"],
   ["$"+health.today_est_usd+" / $"+health.global_daily_usd,"spend today (est)"],
   ["$"+s.llm_spend_est_usd,"spend all-time (est)"],
   [cap?"TRIPPED":"ok","capacity breaker",cap],
   [health.key_format_ok?"ok":"CHECK KEY","API key",!health.key_format_ok],
  ]);
  fill($("f-day"),[...new Set(d.llm.map(r=>r.day))]);
  fill($("f-mach"),[...new Set(d.llm.map(r=>r.install_id))].sort());
  const day=$("f-day").value, mach=$("f-mach").value;
  const spend={};for(const x of d.spend_by_day)spend[x.day]=x.est_usd;
  const rows=d.llm
    .filter(r=>(!day||r.day===day)&&(!mach||r.install_id===mach))
    .map(r=>({...r,machine:r.install_id.slice(0,8),
      est_day_usd:spend[r.day]!==undefined?("$"+spend[r.day]):""}));
  $("llm").innerHTML=table(rows,
   ["day","machine","feature","calls","ok_calls","in_tokens",
    "out_tokens","est_day_usd"]);
  $("machines").innerHTML=table(s.machines,
   ["install_id","first_seen","last_seen","llm_calls"]);
}
async function loadStats(){
  const d=await api("/admin/stats?range_days="+state.range);
  $("st-since").textContent=`since ${d.since}`;
  const t=d.totals;
  const okPct=t.llm_calls?Math.round(100*t.ok_calls/t.llm_calls):100;
  tiles($("st-tiles"),[
    [t.active_machines,"machines that used AI"],
    [t.new_machines,"new machines"],
    [`${t.llm_calls} (${okPct}% ok)`,"AI calls"],
    [`${t.in_tokens}→${t.out_tokens}`,"tokens in→out"],
    ["$"+t.est_usd,"spend (est)"],
  ]);
  bars($("st-daily"),d.active_by_day,r=>r.day,r=>r.calls,
       r=>`${r.calls} call(s) · ${r.machines} machine(s)`);
  $("st-feat").innerHTML=table(d.by_feature.map(f=>({...f,
    est_usd:"$"+f.est_usd})),
    ["feature","calls","ok_calls","in_tokens","out_tokens","est_usd"]);
  $("st-spend").innerHTML=table(d.spend_by_day.map(r=>({...r,
    est_usd:"$"+r.est_usd})),["day","calls","est_usd"]);
}
async function download(path,filename){
  const r=await fetch(path,{headers:{"X-DLC-Admin-Token":tok()}});
  if(!r.ok){alert("download failed: HTTP "+r.status);return}
  const u=URL.createObjectURL(await r.blob());
  const a=document.createElement("a");a.href=u;a.download=filename;
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(u),10000);
}
function setTab(t){
  state.tab=t;
  for(const x of ["overview","stats"]){
    $("sec-"+x).style.display=x===t?"":"none";
    $("tab-"+x).classList.toggle("on",x===t);
  }
  for(const id of ["l-day","l-mach"])
    $(id).style.display=t==="stats"?"none":"";
}
function reloadTab(){
  return state.tab==="overview"?loadOverview():loadStats();
}
function showTab(t){setTab(t);reloadTab();}
function setRange(n){
  state.range=n;
  $("rng-7").classList.toggle("on",n===7);
  $("rng-30").classList.toggle("on",n===30);
  loadStats();
}
async function load(){
  await loadOverview();
  $("gate").style.display="none";$("dash").style.display="block";
  $("logout").style.display="inline-block";
  setTab("overview");
}
["f-day","f-mach"].forEach(id=>$(id).addEventListener("change",reloadTab));
$("refresh").onclick=()=>reloadTab();
let autoTimer=null;
$("auto").addEventListener("change",()=>{
  if($("auto").checked)autoTimer=setInterval(reloadTab,60000);
  else{clearInterval(autoTimer);autoTimer=null;}
});
$("tab-overview").onclick=()=>showTab("overview");
$("tab-stats").onclick=()=>showTab("stats");
$("rng-7").onclick=()=>setRange(7);
$("rng-30").onclick=()=>setRange(30);
$("x-calls").onclick=()=>download("/admin/export.csv?table=daily","usage_by_day.csv");
$("x-machines").onclick=()=>download("/admin/export.csv?table=machines","machines.csv");
$("go").onclick=async()=>{setTok($("tok").value.trim());
  try{await load()}catch(e){$("gate-err").textContent=
    "That token was rejected — check it and try again."}};
$("logout").onclick=()=>{setTok("");location.reload()};
if(tok()){load().catch(()=>{$("gate").style.display="block"})}
</script></body></html>"""


@app.get("/admin/view")
def admin_view():
    return HTMLResponse(_ADMIN_PAGE)
