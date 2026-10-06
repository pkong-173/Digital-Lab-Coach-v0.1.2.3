"""
Layer-3 web endpoints
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from dlc.l3 import limits
from dlc.l3.coverage import scan_tree_coverage
from dlc.l3.oracle import (
    InjectedRow,
    rerun_with_program,
    rerun_with_rows,
    rerun_with_second,
)
from dlc.parser.dig_parser import parse_dig_file
from dlc.testing.spec import extract_test_specs

router = APIRouter()


_SWITCH_LEVEL_ELEMENTS = frozenset({"NFET", "PFET", "PullUp", "PullDown"})


def _switch_level_elements_in(circuit, seen=None) -> set[str]:
    """Element names from _SWITCH_LEVEL_ELEMENTS found in the circuit or
    any resolved subcircuit (cycle-safe via the visited-id set)."""
    if seen is None:
        seen = set()
    if id(circuit) in seen:
        return set()
    seen.add(id(circuit))
    found = {
        c.element_name for c in circuit.components
        if c.element_name in _SWITCH_LEVEL_ELEMENTS
    }
    for sub in circuit.subcircuits:
        if sub.child_circuit is not None:
            found |= _switch_level_elements_in(sub.child_circuit, seen)
    return found


def _transistor_guard(path: str) -> dict | None:
    try:
        circuit = parse_dig_file(path)
    except Exception:
        return None
    found = sorted(_switch_level_elements_in(circuit))
    if not found:
        return None
    return {
        "ok": False,
        "unsupported": True,
        "warning": "DLC does not support transistor labs yet.",
    }


class CoverageRequest(BaseModel):
    session_id: str
    filename: str


def _official_base(path: str, filename: str) -> tuple[str | None, list[str]]:
    from dlc.testing.inject import file_test_status, prepare_injected_run
    try:
        if file_test_status(parse_dig_file(path), filename) != "missing":
            return None, []
    except Exception:
        return None, []
    return prepare_injected_run(path, filename)


@router.post("/api/l3/coverage")
def l3_coverage(req: CoverageRequest) -> dict:
    """
    Mode B's deterministic pass
    """
    from dlc.testing.inject import cleanup_injected
    from dlc.web import server

    target = server._resolve_target(req.session_id, req.filename)
    scan_path, on_temp = target["path"], False
    _s = server._SESSIONS.get(req.session_id)
    _lt = (_s or {}).get("l3_temp") or None
    if (_lt and _lt.get("for") == req.filename and _lt.get("path")
            and os.path.exists(_lt["path"])):
        scan_path, on_temp = _lt["path"], True
    guard = _transistor_guard(scan_path)
    if guard is not None:
        _log_modeB_result(req.session_id, req.filename,
                          {"mode": "unsupported"})
        return guard
    if not limits.allowed("modeB"):
        _log_modeB_result(req.session_id, req.filename, {"mode": "limited"})
        return {
            "ok": False,
            "limited": True,
            "warning": "Daily Coverage Coach limit reached — try again tomorrow.",
            "limits": limits.state(),
        }
    inj_temp, inj_notes = (None, []) if on_temp else _official_base(
        scan_path, req.filename)
    try:
        report = scan_tree_coverage(
            inj_temp or scan_path,
            display=os.path.basename(scan_path) if inj_temp else None)
    except Exception as exc:
        return {
            "ok": False,
            "warning": f"Coverage scan failed: {type(exc).__name__}: {exc}",
        }
    finally:
        cleanup_injected(inj_temp)
    if inj_notes:
        report.notes.extend(inj_notes)
    consumed = report.total_flags == 0 and not report.select_gate
    lim = limits.consume("modeB") if consumed else limits.state()
    if consumed:
        session = server._SESSIONS.get(req.session_id)
        if session is not None:
            session.setdefault("l3_refundable", set()).add(req.filename)
    return {
        "ok": True,
        "warning": None,
        "consumed_use": consumed,
        "limits": lim,
        "on_coach_temp": on_temp,
        "injected": inj_notes,
        **report.to_dict(),
    }


class ProposeRequest(BaseModel):
    session_id: str
    filename: str
    model: str | None = None


@router.post("/api/l3/propose")
def l3_propose(req: ProposeRequest) -> dict:
    from dlc.l3 import proposer
    from dlc.testing.inject import cleanup_injected
    from dlc.web import server

    target = server._resolve_target(req.session_id, req.filename)
    prop_path, on_temp = target["path"], False
    _s = server._SESSIONS.get(req.session_id)
    _lt = (_s or {}).get("l3_temp") or None
    if (_lt and _lt.get("for") == req.filename and _lt.get("path")
            and os.path.exists(_lt["path"])):
        prop_path, on_temp = _lt["path"], True
    guard = _transistor_guard(prop_path)
    if guard is not None:
        _log_modeB_result(req.session_id, req.filename,
                          {"mode": "unsupported"})
        return {**guard, "proposals": [], "rejected": [], "notes": []}
    inj_temp, inj_notes = (None, []) if on_temp else _official_base(
        prop_path, req.filename)
    try:
        result = proposer.propose_rows(
            inj_temp or prop_path, model=req.model,
            display=os.path.basename(prop_path) if inj_temp else None)
    except Exception as exc:
        result = {"ok": False, "proposals": [], "rejected": [],
                  "model": req.model, "notes": [],
                  "error": f"Proposer failed: {type(exc).__name__}: {exc}"}
    finally:
        cleanup_injected(inj_temp)
    if inj_notes:
        result["injected"] = inj_notes
    session = server._SESSIONS.get(req.session_id)
    refundable = (session is not None
                  and req.filename in session.get("l3_refundable", set()))
    if refundable:
        session["l3_refundable"].discard(req.filename)
        if not result.get("proposals"):
            result["limits"] = limits.refund("modeB")
            result["refunded"] = True
            if result.get("all_categories_covered"):
                note = "Today's Coverage Coach use was refunded."
            else:
                note = ("No usable new tests this time - that can be a "
                        "coach limitation, not proof your tests are "
                        "complete. Today's Coverage Coach use was refunded.")
            result.setdefault("notes", []).append(note)
    _log_modeB_result(req.session_id, req.filename, {
        "mode": "analysis" if result.get("ok", True) else "error",
        "proposals": len(result.get("proposals") or []),
        "refunded": bool(result.get("refunded")),
        "model": result.get("model"),
        **_modeB_shape(result),
    })
    return result


def _modeB_shape(result: dict) -> dict:
    groups = result.get("proposals") or []
    rejected = result.get("rejected") or []
    kinds: dict[str, int] = {}
    for r in rejected:
        k = str(r.get("kind") or "format")
        kinds[k] = kinds.get(k, 0) + len(r.get("rows") or [])
    return {
        "rows": sum(len(g.get("rows") or []) for g in groups),
        "disputed": sum(len(g.get("disputed_rows") or []) for g in groups),
        "rejected": sum(len(r.get("rows") or []) for r in rejected),
        "rejected_kinds": kinds,
        "covered": bool(result.get("all_categories_covered")),
    }


def _log_modeB_result(session_id: str, filename: str, props: dict) -> None:
    try:
        from dlc.telemetry.sink import log_events
        log_events(session_id, [{"kind": "l3_modeB_result_server",
                                 "filename": filename, **props}])
    except Exception:
        pass


class InjectRequest(BaseModel):
    session_id: str
    filename: str
    rows: list[str] = []
    spec_name: str | None = None
    origin: str = "coach"
    as_second: bool = False
    rom_words: list[str] = []
    insert_at: int | None = None
    insert_before_row: int | None = None
    pc_shift: int = 0
    pc_col: str | None = None
    headers: list[str] | None = None


@router.post("/api/l3/inject")
def l3_inject(req: InjectRequest) -> dict:
    """Mode B's accept-flow"""
    from dlc.l3.coverage import SYNTHETIC_SPEC_NAME
    from dlc.testing.inject import cleanup_injected
    from dlc.web import server

    target = server._resolve_target(req.session_id, req.filename)
    base_path, on_temp = target["path"], False
    _s = server._SESSIONS.get(req.session_id)
    _lt = (_s or {}).get("l3_temp") or None
    prev_coach_rows: list[int] = []
    if (_lt and _lt.get("for") == req.filename and _lt.get("path")
            and os.path.exists(_lt["path"])):
        base_path, on_temp = _lt["path"], True
        prev_coach_rows = list(_lt.get("coach_rows") or [])
    inj_temp, inj_notes = (None, []) if on_temp else _official_base(
        base_path, req.filename)
    if inj_temp:
        base_path = inj_temp

    try:
        spec_name = req.spec_name
        if spec_name is None:
            try:
                specs = extract_test_specs(parse_dig_file(base_path))
            except Exception as exc:
                return {"ok": False, "outcome": "error",
                        "warning": f"Could not parse circuit: {exc}"}
            if specs:
                spec_name = specs[0].name
            elif req.headers:
                spec_name = SYNTHETIC_SPEC_NAME
            else:
                return {"ok": False, "outcome": "error",
                        "warning": "This file has no testcase to inject into."}

        rows = [InjectedRow(raw=r, origin=req.origin or "coach")
                for r in req.rows if isinstance(r, str)]
        if req.as_second:
            outcome = rerun_with_second(
                base_path, spec_name, rows, req.rom_words, keep_temp=True,
            )
        elif req.rom_words:
            splice = ({"insert_at": req.insert_at,
                       "insert_before_row": req.insert_before_row,
                       "pc_shift": req.pc_shift, "pc_col": req.pc_col}
                      if req.insert_at is not None else {})
            outcome = rerun_with_program(
                base_path, spec_name, rows, req.rom_words, keep_temp=True,
                **splice,
            )
        else:
            outcome = rerun_with_rows(
                base_path, spec_name, rows, keep_temp=True,
                headers=req.headers or None,
            )
    finally:
        cleanup_injected(inj_temp)
    body = outcome.to_dict()
    if inj_notes:
        body["injected"] = inj_notes
    if not outcome.ok:
        return {**body, "outcome": "error", "temp_filename": None}

    temp_filename = f"{Path(req.filename).stem}__coach.dig"
    session = server._SESSIONS.get(req.session_id)
    if session is not None and outcome.temp_path:
        for f in list(session["files"]):
            if f["name"] == temp_filename:
                session["files"].remove(f)
                if f["path"] != outcome.temp_path:
                    try:
                        os.remove(f["path"])
                    except OSError:
                        pass
        session["files"].append(
            {"name": temp_filename, "path": outcome.temp_path},
        )
        session["l3_temp"] = {
            "for": req.filename,
            "name": temp_filename,
            "path": outcome.temp_path,
            "spec_name": outcome.spec_name or spec_name,
            "coach_rows": sorted(set(prev_coach_rows) | {
                r["index"] for r in (outcome.rows or [])
                if r.get("added") and isinstance(r.get("index"), int)}),
        }

    return {
        **body,
        "outcome": "all_set" if outcome.all_passed else "rows_fail",
        "temp_filename": temp_filename,
    }


class UninjectRequest(BaseModel):
    session_id: str
    filename: str


@router.post("/api/l3/uninject")
def l3_uninject(req: UninjectRequest) -> dict:
    from dlc.web import server   

    server._resolve_target(req.session_id, req.filename)
    temp_filename = f"{Path(req.filename).stem}__coach.dig"
    session = server._SESSIONS.get(req.session_id)
    removed = False
    if session is not None:
        for f in list(session["files"]):
            if f["name"] == temp_filename:
                session["files"].remove(f)
                removed = True
                try:
                    os.remove(f["path"])
                except OSError:
                    pass
        lt = session.get("l3_temp")
        if lt and lt.get("name") == temp_filename:
            session["l3_temp"] = None
    return {"ok": True, "removed": removed, "temp_filename": temp_filename}


class AdoptRequest(BaseModel):
    session_id: str
    filename: str


@router.post("/api/l3/adopt_official")
def l3_adopt_official(req: AdoptRequest) -> dict:
    from dlc.l3 import official_store
    from dlc.web import server

    server._resolve_target(req.session_id, req.filename)
    session = server._SESSIONS.get(req.session_id)
    temp_filename = f"{Path(req.filename).stem}__coach.dig"
    entry = next((f for f in (session or {}).get("files", [])
                  if f["name"] == temp_filename), None)
    if entry is None:
        return {"ok": False, "warning": ("No verified coach temp for this "
                                         "file — run Mode B and Accept "
                                         "first.")}
    try:
        specs = extract_test_specs(parse_dig_file(entry["path"]))
    except Exception as exc:
        return {"ok": False,
                "warning": f"Could not read the temp circuit: {exc}"}
    if not specs:
        return {"ok": False, "warning": "The temp circuit has no testcase."}
    lt = (session or {}).get("l3_temp") or {}
    spec = (next((s for s in specs if s.name == lt.get("spec_name")), None)
            or next((s for s in specs if s.rows), specs[0]))
    saved = official_store.save_test(req.filename, spec.raw_data_string,
                                     allow_default_override=True)
    return {"ok": True, "filename": req.filename, "sha1": saved["sha1"],
            "rows": spec.row_count()}


class DebugRequest(BaseModel):
    session_id: str
    filename: str
    spec_index: int = 0
    model: str | None = None


def _rom_gate_result(gate: dict, model: str | None, on_temp: bool) -> dict:
    from dlc.l3 import debugger
    return {
        "ok": True, "contract": debugger.CONTRACT,
        "model": model or debugger._debug_model(),
        "mode": "rom_mismatch", "message": gate["message"],
        "rom_check": gate, "cards": [], "notes": [], "dropped_ideas": [],
        "usage": {"input_tokens": 0, "output_tokens": 0}, "llm_calls": 0,
        "rom_verified": False, "limits": limits.state(),
        "consumed_use": False, "on_coach_temp": on_temp,
    }


def _log_modeA_result(session_id: str, filename: str, result: dict) -> None:
    try:
        from dlc.telemetry.sink import log_events
        u = result.get("usage") or {}
        log_events(session_id, [{
            "kind": "l3_modeA_result_server",
            "filename": filename, "mode": result.get("mode"),
            "cards": len(result.get("cards") or []),
            "confirmed": sum(1 for c in (result.get("cards") or [])
                             if (c.get("verified") or {}).get("confirmed")),
            "llm_calls": result.get("llm_calls"),
            "in_tokens": u.get("input_tokens"),
            "out_tokens": u.get("output_tokens"),
            "model": result.get("model"),
            "rom_verified": bool(result.get("rom_verified")),
            "consumed_use": bool(result.get("consumed_use")),
        }])
    except Exception:
        pass


@router.post("/api/llm/debug")
def llm_debug(req: DebugRequest) -> dict:
    from dlc.l3 import debugger
    from dlc.l3.official_store import get_runtime_payload
    from dlc.testing.inject import (
        check_rom_contents, prepare_injected_run, cleanup_injected,
    )
    from dlc.web import server

    target = server._resolve_target(req.session_id, req.filename)
    guard = _transistor_guard(target["path"])
    if guard is not None:
        result = {**guard, "mode": "unsupported", "cards": []}
        _log_modeA_result(req.session_id, req.filename, result)
        return result

    path, spec_name, on_temp = target["path"], None, False
    coach_rows = None
    session = server._SESSIONS.get(req.session_id)
    lt = (session or {}).get("l3_temp") or None
    if (lt and lt.get("for") == req.filename and lt.get("path")
            and os.path.exists(lt["path"])):
        path, spec_name, on_temp = lt["path"], lt.get("spec_name"), True
        coach_rows = lt.get("coach_rows") or None

    gate = check_rom_contents(path, req.filename)
    if gate is not None:
        result = _rom_gate_result(gate, req.model, on_temp)
        _log_modeA_result(req.session_id, req.filename, result)
        return result

    if not limits.allowed("modeA"):
        result = {
            "ok": False,
            "limited": True,
            "warning": "Daily debug-analysis limit reached — try again "
                       "tomorrow.",
            "limits": limits.state(),
        }
        _log_modeA_result(req.session_id, req.filename,
                          {**result, "mode": "limited", "cards": []})
        return result

    inj_temp, inj_notes = (None, [])
    if not on_temp:
        inj_temp, inj_notes = prepare_injected_run(path, req.filename)
        if inj_temp:
            path = inj_temp
            spec_name = None

    try:
        result = debugger.debug_circuit(
            path, spec_name=spec_name, spec_index=req.spec_index,
            model=req.model, coach_rows=coach_rows,
            lazy_exempt=debugger._lazy_exempt_name(req.filename),
            source_filename=req.filename,
        )
    except Exception as exc:
        return {"ok": False, "mode": "error",
                "warning": f"Debug run failed: {type(exc).__name__}: {exc}"}
    finally:
        if inj_temp:
            cleanup_injected(inj_temp)
    if inj_notes:
        result["injected"] = inj_notes
    result["rom_verified"] = bool(get_runtime_payload(req.filename, "rom"))

    consumed = (result.get("mode") == "analysis"
                and bool(result.get("cards")))
    result["limits"] = limits.consume("modeA") if consumed else limits.state()
    result["consumed_use"] = consumed
    result["on_coach_temp"] = on_temp
    _remember_marks(req.filename, target["path"], result)
    _log_modeA_result(req.session_id, req.filename, result)
    return result


def _remember_marks(filename: str, path: str, result: dict) -> None:
    if result.get("mode") != "analysis":
        return
    try:
        from dlc.parser.dig_parser import parse_dig_file
        from dlc.parser.netlist import build_netlist
        from dlc.web import server
        c = parse_dig_file(path)
        nl = build_netlist(c)
        s_keys, s_pins = server._component_marks(
            c, nl, result.get("suspect_indices") or [])
        card_idx = [op.get("component_index")
                    for card in (result.get("cards") or [])
                    for op in ((card.get("fix") or {}).get("ops") or [])
                    if isinstance(op.get("component_index"), int)]
        c_keys, c_pins = server._component_marks(c, nl, card_idx)
        server._LAST_SUSPECTS[filename] = {
            "suspects": s_keys, "suspect_pins": s_pins,
            "cards": c_keys, "card_pins": c_pins}
    except Exception:
        pass


class AcceptFixRequest(BaseModel):
    session_id: str
    filename: str
    ops: list[dict] = []
    spec_name: str | None = None


@router.post("/api/l3/accept_fix")
def l3_accept_fix(req: AcceptFixRequest) -> dict:
    """
    ACCEPT FIX: apply a CONFIRMED card's ops to a TEMP
    copy ONLY 
    """
    from dlc.l3.patch import apply_patch
    from dlc.testing.runner import per_row_run_auto
    from dlc.web import server

    target = server._resolve_target(req.session_id, req.filename)
    if not req.ops:
        return {"ok": False, "warning": "No fix ops to accept."}
    path = target["path"]
    session = server._SESSIONS.get(req.session_id)
    lt = (session or {}).get("l3_temp") or None
    prev_coach_rows: list[int] = []
    spec_name = req.spec_name
    on_prev_temp = False
    if (lt and lt.get("for") == req.filename and lt.get("path")
            and os.path.exists(lt["path"])):
        path = lt["path"]
        prev_coach_rows = list(lt.get("coach_rows") or [])
        spec_name = spec_name or lt.get("spec_name")
        on_prev_temp = True

    temp, rep = apply_patch(path, req.ops)
    if temp is None:
        return {"ok": False, "warning": rep.warning}

    if not on_prev_temp:
        from dlc.testing.inject import inject_official_tests_in_place
        if inject_official_tests_in_place(temp, req.filename):
            spec_name = None

    temp_filename = f"{Path(req.filename).stem}__coach.dig"
    if session is not None:
        for f in list(session["files"]):
            if f["name"] == temp_filename:
                session["files"].remove(f)
                if f["path"] != temp:
                    try:
                        os.remove(f["path"])
                    except OSError:
                        pass
        session["files"].append({"name": temp_filename, "path": temp})
        session["l3_temp"] = {
            "for": req.filename,
            "name": temp_filename,
            "path": temp,
            "spec_name": spec_name,
            "coach_rows": prev_coach_rows,
        }

    inj2 = None
    try:
        from dlc.testing.inject import (
            prepare_injected_run, cleanup_injected,
        )
        inj2, inj2_notes = prepare_injected_run(temp, req.filename)
        run_path = inj2 or temp
        circ = parse_dig_file(run_path)
        specs = extract_test_specs(circ)
        sp = next((s for s in specs if s.name == spec_name),
                  specs[0] if specs else None)
        if sp is None:
            return {"ok": True, "temp_filename": temp_filename,
                    "spec": None, "all_passed": None}
        raw_by_idx = {r.line_index: r.raw for r in sp.rows
                      if not r.is_malformed}
        from dlc.testing.runner import find_digital_jar
        jar = find_digital_jar()
        if jar:
            rs = per_row_run_auto(sp, run_path, jar_path=jar)
            rows = [{"index": r.row_index,
                     "raw": raw_by_idx.get(r.row_index, ""),
                     "status": "passed" if r.status == "passed"
                               else "failed"} for r in rs]
        else:
            from dlc.l3.debugger import _offline_failing
            failing, _det = _offline_failing(run_path, sp.name)
            rows = [{"index": r.line_index, "raw": r.raw,
                     "status": "failed" if r.line_index in failing
                               else "passed"}
                    for r in sp.rows if not r.is_malformed]
        allp = all(r["status"] == "passed" for r in rows)
        out = {"ok": True, "temp_filename": temp_filename,
               "spec": {"name": sp.name, "headers": list(sp.headers),
                        "rows": rows, "all_passed": allp},
               "all_passed": allp}
        if inj2_notes:
            out["injected"] = inj2_notes
        try:
            from dlc.telemetry.sink import log_events
            log_events(req.session_id, [{
                "kind": "l3_accept_fix_server",
                "filename": req.filename,
                "n_ops": len(req.ops or []),
                "all_passed": out.get("all_passed"),
                "injected": bool(out.get("injected")),
            }])
        except Exception:
            pass
        return out
    except Exception as exc:
        return {"ok": True, "temp_filename": temp_filename, "spec": None,
                "all_passed": None,
                "warning": (f"fix accepted onto the temp, but the rerun "
                            f"failed: {type(exc).__name__}: {exc}")}
    finally:
        if inj2:
            cleanup_injected(inj2)


class FixRetestRequest(BaseModel):
    session_id: str
    filename: str
    ops: list[dict] = []
    spec_name: str | None = None


@router.post("/api/l3/fix_retest")
def l3_fix_retest(req: FixRetestRequest) -> dict:
    from dlc.l3.patch import rerun_with_patch
    from dlc.web import server

    target = server._resolve_target(req.session_id, req.filename)
    path = target["path"]
    spec_name = req.spec_name
    session = server._SESSIONS.get(req.session_id)
    lt = (session or {}).get("l3_temp") or None
    if (lt and lt.get("for") == req.filename and lt.get("path")
            and os.path.exists(lt["path"])):
        path, spec_name = lt["path"], spec_name or lt.get("spec_name")
    if not req.ops:
        return {"ok": False, "warning": "No fix ops to retest."}
    try:
        outcome = rerun_with_patch(path, req.ops, spec_name=spec_name)
    except Exception as exc:
        return {"ok": False,
                "warning": f"Retest failed: {type(exc).__name__}: {exc}"}
    if outcome.ok:
        spec = None
        if outcome.specs:
            spec = next((s for s in outcome.specs if s["name"] == spec_name),
                        outcome.specs[0])
        return {"ok": True, "spec": spec, "all_passed": outcome.all_passed}
    if "Digital.jar" not in (outcome.warning or ""):
        return {"ok": False, "warning": outcome.warning}

    from dlc.l3.debugger import _offline_failing
    from dlc.l3.patch import apply_patch
    temp, rep = apply_patch(path, req.ops)
    if temp is None:
        return {"ok": False, "warning": rep.warning}
    try:
        circ = parse_dig_file(temp)
        specs = extract_test_specs(circ)
        sp = next((s for s in specs if s.name == spec_name),
                  specs[0] if specs else None)
        if sp is None:
            return {"ok": False, "warning": "The temp has no testcase."}
        failing, _det = _offline_failing(temp, sp.name)
        rows = [{"index": r.line_index, "raw": r.raw,
                 "status": "failed" if r.line_index in failing else "passed"}
                for r in sp.rows if not r.is_malformed]
        return {"ok": True,
                "spec": {"name": sp.name, "headers": list(sp.headers),
                         "rows": rows, "all_passed": not failing},
                "all_passed": not failing}
    except Exception as exc:
        return {"ok": False,
                "warning": f"Retest failed: {type(exc).__name__}: {exc}"}
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


@router.get("/api/l3/configured")
def l3_configured() -> dict:
    from dlc.l3 import manifest as mf
    from dlc.l3 import official_store

    files: set[str] = set()
    for m in mf.load_manifests():
        files |= set(m.get("applies_to") or [])
        files |= set((m.get("official_tests") or {}).keys())
    files |= {t["filename"] for t in official_store.list_tests()}
    return {"ok": True, "files": sorted(files)}