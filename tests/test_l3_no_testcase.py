"""
Mode B on a file that has no testcase
"""

import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from dlc.l3 import proposer
from dlc.l3.coverage import (
    SYNTHETIC_SPEC_NAME, scan_tree_coverage, synthetic_headers_for,
)
from dlc.l3.oracle import InjectedRow, rerun_with_rows, write_temp_with_rows
from dlc.parser.dig_parser import parse_dig_file
from dlc.testing.runner import find_digital_jar
from dlc.testing.spec import extract_test_specs
from dlc.web import server
from dlc.web.server import app

client = TestClient(app)

_AND = "data/sample_circuits/tier1_minimal/single_and.dig"
_CALC = "data/sample_circuits/30_bug_benchmark/bug1_meaningless_mux_in3/tier3_calculator.dig"

_needs_jar = pytest.mark.skipif(
    find_digital_jar() is None, reason="Digital.jar not configured",
)

_TESTCASE_RE = re.compile(
    r"<visualElement>\s*<elementName>Testcase</elementName>.*?</visualElement>\s*",
    re.DOTALL)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_LIMITS_PATH", str(tmp_path / "limits.json"))
    monkeypatch.setenv("DLC_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.setenv("DLC_OFFICIAL_TESTS_PATH", str(tmp_path / "official.json"))
    monkeypatch.delenv("DLC_ENFORCE_LIMITS", raising=False)


def _and_without_testcase(tmp_path, name="single_and.dig", data=None):
    """single_and.dig with its Testcase element removed, or with its
    dataString replaced by `data` (e.g. '' for an empty testcase)."""
    xml = open(_AND, encoding="utf-8").read()
    if data is None:
        xml = _TESTCASE_RE.sub("", xml)
        assert "Testcase" not in xml
    else:
        xml = re.sub(r"<dataString>.*?</dataString>",
                     f"<dataString>{data}</dataString>", xml, flags=re.S)
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return p


def _upload(path):
    with open(path, "rb") as fh:
        r = client.post("/api/circuit", files=[
            ("files", (os.path.basename(path), fh, "application/xml"))])
    assert r.status_code == 200
    return r.json()["session_id"]


def _fake_llm(groups, selfcheck_rows=None):
    """First call: proposals; later calls: the self-check reply."""
    first = json.dumps({"proposals": groups})
    second = json.dumps({"rows": selfcheck_rows or []})
    n = {"v": 0}

    def call(prompt, **kw):
        n["v"] += 1
        return {"ok": True, "text": first if n["v"] == 1 else second,
                "error": None, "usage": None, "model": kw.get("model")}
    call.count = n
    return call

def test_synthetic_headers_follow_inputs_clock_outputs_order(tmp_path):
    c = parse_dig_file(str(_and_without_testcase(tmp_path)))
    assert synthetic_headers_for(c) == ["A", "B", "Y"]
    # the sequential sample: inputs, then the clock, then the outputs
    c2 = parse_dig_file("data/sample_circuits/30_bug_benchmark/bug3_wrong_cin/Wrong_cin.dig")
    h = synthetic_headers_for(c2)
    spec = extract_test_specs(c2)[0]
    assert h == list(spec.headers)


def test_root_without_testcase_gets_synthetic_headers_and_gap_notes(tmp_path):
    p = _and_without_testcase(tmp_path)
    r = scan_tree_coverage(str(p))
    c = r.circuits[0]
    assert c.has_testcases is False
    assert c.synthetic_headers == ["A", "B", "Y"]
    assert r.total_flags == 0 and not r.select_gate
    joined = " ".join(c.notes)
    assert "no embedded testcase" in joined
    assert "propose a first set" in joined
    assert "input 'A' (1-bit) is never tested with values 0, 1." in c.notes
    assert "output 'Y' is never checked by any row." in c.notes
    assert r.to_dict()["circuits"][0]["synthetic_headers"] == ["A", "B", "Y"]


def test_empty_testcase_is_treated_like_none(tmp_path):
    p = _and_without_testcase(tmp_path, data="")
    r = scan_tree_coverage(str(p))
    c = r.circuits[0]
    assert c.has_testcases is True and c.row_count == 0
    assert c.synthetic_headers == ["A", "B", "Y"]
    t = proposer.build_targets(r)
    assert len(t) == 1 and t[0]["synthetic"] is True
    assert t[0]["spec_name"] == SYNTHETIC_SPEC_NAME


def test_untested_child_circuits_are_not_synthesized():
    r = scan_tree_coverage(_CALC)
    assert [c.file for c in r.circuits] == ["tier3_calculator.dig", "bool_unit.dig"]
    child = r.circuits[1]
    assert not child.has_testcases
    assert child.synthetic_headers == []
    assert any("no embedded testcase" in n for n in child.notes)
    assert not any("never tested" in n for n in child.notes)
    targets = proposer.build_targets(r)
    assert [t["file"] for t in targets] == ["tier3_calculator.dig"]
    assert not any(t.get("synthetic") for t in targets)


def test_a_real_testcase_is_never_replaced_by_a_synthetic_one():
    r = scan_tree_coverage(_AND)
    assert r.circuits[0].synthetic_headers == []
    t = proposer.build_targets(r)[0]
    assert "synthetic" not in t and len(t["existing_rows"]) == 4

def test_synthetic_target_has_no_example_rows_and_groups_carry_headers(tmp_path):
    p = _and_without_testcase(tmp_path)
    r = scan_tree_coverage(str(p))
    targets = proposer.build_targets(r)
    assert len(targets) == 1
    t = targets[0]
    assert t["synthetic"] is True
    assert t["spec_name"] == SYNTHETIC_SPEC_NAME
    assert t["headers"] == ["A", "B", "Y"]
    assert t["existing_rows"] == [] and t["has_clock"] is False
    prompt = proposer.build_prompt(r, targets)
    assert '"synthetic": true' in prompt
    fake = _fake_llm(
        [{"file": "single_and.dig", "spec_name": SYNTHETIC_SPEC_NAME,
          "rows": ["1 1 1", "1 0 0", "1 0"], "why": "first rows"}],
        selfcheck_rows=[{"index": 0, "outputs": {"Y": "1"}},
                        {"index": 1, "outputs": {"Y": "0"}}])
    res = proposer.propose_rows(str(p), call=fake)
    assert res["ok"] is True
    assert len(res["proposals"]) == 1
    g = res["proposals"][0]
    assert g["rows"] == ["1 1 1", "1 0 0"]
    assert g["synthetic"] is True and g["headers"] == ["A", "B", "Y"]
    assert g.get("disputed_rows", []) == []
    assert [x["rows"] for x in res["rejected"]] == [["1 0"]]   # format gate


def test_no_labelled_pins_means_no_target(tmp_path):
    xml = open(_AND, encoding="utf-8").read()
    xml = _TESTCASE_RE.sub("", xml)
    xml = xml.replace("<string>Y</string>", "<string></string>")
    p = tmp_path / "unlabelled.dig"
    p.write_text(xml, encoding="utf-8")
    r = scan_tree_coverage(str(p))
    assert r.circuits[0].synthetic_headers == []
    res = proposer.propose_rows(str(p), call=_fake_llm([]))
    assert res["ok"] is False
    assert "No testcase anywhere" in res["error"]

def test_write_temp_creates_the_testcase_when_headers_are_given(tmp_path):
    p = _and_without_testcase(tmp_path)
    temp, spec = write_temp_with_rows(
        str(p), SYNTHETIC_SPEC_NAME, [InjectedRow("1 1 1"), InjectedRow("0 1 0")],
        headers=["A", "B", "Y"])
    try:
        assert spec.row_count() == 0            # the testcase before the rows
        specs = extract_test_specs(parse_dig_file(temp))
        assert [(s.name, s.headers, s.row_count()) for s in specs] == \
            [(SYNTHETIC_SPEC_NAME, ["A", "B", "Y"], 2)]
        assert "Testcase" not in p.read_text(encoding="utf-8")   # untouched
    finally:
        os.unlink(temp)
    with pytest.raises(ValueError, match="No testcase named"):
        write_temp_with_rows(str(p), SYNTHETIC_SPEC_NAME, [InjectedRow("1 1 1")])


@_needs_jar
def test_rerun_on_a_created_testcase_marks_every_row_added(tmp_path):
    p = _and_without_testcase(tmp_path)
    out = rerun_with_rows(str(p), SYNTHETIC_SPEC_NAME,
                          [InjectedRow("1 1 1"), InjectedRow("1 1 0")],
                          headers=["A", "B", "Y"])
    assert out.ok is True
    assert [r["added"] for r in out.rows] == [True, True]
    assert [r["status"] for r in out.rows] == ["passed", "failed"]
    assert out.all_passed is False and out.headers == ["A", "B", "Y"]


@_needs_jar
def test_inject_route_builds_the_coach_temp_for_a_file_without_testcase(tmp_path, monkeypatch):
    p = _and_without_testcase(tmp_path, name="notest.dig")
    sid = _upload(str(p))
    monkeypatch.setattr(proposer, "call_llm", _fake_llm(
        [{"file": "notest.dig", "spec_name": SYNTHETIC_SPEC_NAME,
          "rows": ["1 1 1", "0 1 0"], "why": "x"}]))
    try:
        cov = client.post("/api/l3/coverage", json={
            "session_id": sid, "filename": "notest.dig"}).json()
        assert cov["ok"] and cov["circuits"][0]["synthetic_headers"] == ["A", "B", "Y"]
        prop = client.post("/api/l3/propose", json={
            "session_id": sid, "filename": "notest.dig"}).json()
        assert prop["ok"] and prop["proposals"][0]["synthetic"] is True
        g = prop["proposals"][0]
        body = client.post("/api/l3/inject", json={
            "session_id": sid, "filename": "notest.dig",
            "spec_name": g["spec_name"], "rows": g["rows"],
            "headers": g["headers"]}).json()
        assert body["ok"] is True and body["outcome"] == "all_set"
        assert body["temp_filename"] == "notest__coach.dig"
        lt = server._SESSIONS[sid]["l3_temp"]
        assert lt["spec_name"] == SYNTHETIC_SPEC_NAME
        assert lt["coach_rows"] == [0, 1]
        # the next scan runs on the coach temp, which now has a testcase
        cov2 = client.post("/api/l3/coverage", json={
            "session_id": sid, "filename": "notest.dig"}).json()
        assert cov2["on_coach_temp"] is True
        assert cov2["circuits"][0]["has_testcases"] is True
        assert cov2["circuits"][0]["row_count"] == 2
        # Adopt saves the created testcase (header + the verified rows)
        adopt = client.post("/api/l3/adopt_official", json={
            "session_id": sid, "filename": "notest.dig"}).json()
        assert adopt["ok"] is True and adopt["rows"] == 2
        from dlc.l3 import official_store
        assert official_store.get_content("notest.dig") == "A B Y\n1 1 1\n0 1 0"
    finally:
        server._SESSIONS.pop(sid, None)


def test_inject_without_headers_or_testcase_is_a_clean_error(tmp_path):
    p = _and_without_testcase(tmp_path, name="notest.dig")
    sid = _upload(str(p))
    try:
        body = client.post("/api/l3/inject", json={
            "session_id": sid, "filename": "notest.dig", "rows": ["1 1 1"]}).json()
        assert body["ok"] is False and body["outcome"] == "error"
        assert "no testcase" in body["warning"].lower()
    finally:
        server._SESSIONS.pop(sid, None)

@_needs_jar
def test_lab_with_an_official_test_extends_it_instead_of_a_synthetic_one(tmp_path, monkeypatch):
    from dlc.l3 import official_store
    official_store.save_test("off_and.dig", "A B Y\n0 0 0\n1 1 1",
                             allow_default_override=True)
    p = _and_without_testcase(tmp_path, name="off_and.dig")
    sid = _upload(str(p))
    monkeypatch.setattr(proposer, "call_llm", _fake_llm(
        [{"file": "off_and.dig", "spec_name": "official",
          "rows": ["1 0 0"], "why": "x"}]))
    try:
        cov = client.post("/api/l3/coverage", json={
            "session_id": sid, "filename": "off_and.dig"}).json()
        c0 = cov["circuits"][0]
        assert c0["file"] == "off_and.dig"
        assert c0["has_testcases"] is True and c0["row_count"] == 2
        assert c0["synthetic_headers"] == []
        assert c0["official_test"] == "official"
        assert any("official testcase injected" in n for n in cov["notes"])
        assert cov["injected"]
        prop = client.post("/api/l3/propose", json={
            "session_id": sid, "filename": "off_and.dig"}).json()
        assert prop["ok"] and prop["proposals"][0]["spec_name"] == "official"
        assert not prop["proposals"][0].get("synthetic")
        body = client.post("/api/l3/inject", json={
            "session_id": sid, "filename": "off_and.dig",
            "spec_name": "official", "rows": ["1 0 0"]}).json()
        assert body["ok"] and body["outcome"] == "all_set"
        assert [(r["origin"], r["status"]) for r in body["rows"]] == [
            ("original", "passed"), ("original", "passed"), ("coach", "passed")]
        adopt = client.post("/api/l3/adopt_official", json={
            "session_id": sid, "filename": "off_and.dig"}).json()
        assert adopt["ok"] and adopt["rows"] == 3
        # the official rows survive: Adopt merged, it did not replace
        assert official_store.get_content("off_and.dig") == \
            "A B Y\n0 0 0\n1 1 1\n1 0 0"
        assert not any(f.startswith(".dlc_injected__")
                       for f in os.listdir(tmp_path))
    finally:
        server._SESSIONS.pop(sid, None)

def test_limits_route_reports_local_caps_and_remembered_proxy_budget(monkeypatch):
    from dlc.llm import client as llm_client
    monkeypatch.setattr(llm_client, "_PROXY_BUDGET", {})
    body = client.get("/api/llm/limits").json()
    assert body["ok"] is True
    assert body["local"]["caps"] == {"modeA": 1, "modeB": 2}
    assert body["proxy"] == {}
    llm_client._remember_budget("explain", {
        "ok": True, "limit": {"feature": "explain", "used": 2, "budget": 2}})
    llm_client._remember_budget("grade", {
        "ok": True, "limit": {"feature": "grade", "used": 1, "budget": 2}})
    llm_client._remember_budget("modeA", {
        "ok": False, "limit_hit": True, "capacity_hit": True,
        "error": "The course server has reached its daily capacity."})
    px = client.get("/api/llm/limits").json()["proxy"]
    assert px["explain"]["hit"] is True and px["explain"]["used"] == 2
    assert px["grade"]["hit"] is False
    assert px["modeA"] == {"hit": True, "capacity": True,
                           "message": "The course server has reached its daily capacity.",
                           "used": None, "budget": None}
    llm_client._PROXY_BUDGET["grade"]["date"] = "2000-01-01"
    assert "grade" not in client.get("/api/llm/limits").json()["proxy"]


def test_explain_and_grade_pass_the_limit_flag_through(monkeypatch):
    from dlc.llm import explain, grade
    stop = {"ok": False, "text": None, "usage": None, "model": "m",
            "error": "Daily limit reached for explain on this machine — it resets tomorrow.",
            "limit_hit": True}
    monkeypatch.setattr(explain, "call_llm", lambda *a, **k: stop)
    monkeypatch.setattr(grade, "call_llm", lambda *a, **k: {**stop, "error": "grade stop"})
    from dlc.facts.extractor import extract_facts
    circuit = parse_dig_file(_AND)
    facts = extract_facts(circuit).to_dict()
    out = explain.explain_circuit(facts, [], None, None)
    assert out["ok"] is False and out["limit_hit"] is True
    assert out["capacity_hit"] is False
    g = grade.grade_summary(facts, "some summary", None, None)
    assert g["ok"] is False and g["limit_hit"] is True and g["error"] == "grade stop"


def test_mode_a_run_that_only_hit_the_budget_is_reported_as_limited(monkeypatch):
    from dlc.l3 import debugger
    bug3 = "data/sample_circuits/30_bug_benchmark/bug3_wrong_cin/Wrong_cin.dig"
    sid = _upload(bug3)

    def fake(path, **kw):
        return {"ok": True, "mode": "analysis", "cards": [], "notes": [],
                "best_unverified": None, "limit_hit": True,
                "limit_message": "Daily limit reached for modeA on this machine — it resets tomorrow.",
                "usage": {"input_tokens": 0, "output_tokens": 0}, "llm_calls": 1}
    monkeypatch.setattr(debugger, "debug_circuit", fake)
    try:
        body = client.post("/api/llm/debug", json={
            "session_id": sid, "filename": "Wrong_cin.dig"}).json()
        assert body["ok"] is False and body["limited"] is True
        assert body["proxy_limit"] is True
        assert "Daily limit reached" in body["warning"]
        assert body["limits"]["used"]["modeA"] == 0
    finally:
        server._SESSIONS.pop(sid, None)


def test_mode_a_partial_analysis_keeps_its_cards_despite_a_late_budget_stop(monkeypatch):
    from dlc.l3 import debugger
    bug3 = "data/sample_circuits/30_bug_benchmark/bug3_wrong_cin/Wrong_cin.dig"
    sid = _upload(bug3)

    def fake(path, **kw):
        return {"ok": True, "mode": "analysis", "cards": [{"rank": 1}],
                "notes": [], "limit_hit": True, "limit_message": "stop"}
    monkeypatch.setattr(debugger, "debug_circuit", fake)
    try:
        body = client.post("/api/llm/debug", json={
            "session_id": sid, "filename": "Wrong_cin.dig"}).json()
        assert body["ok"] is True and body["cards"] == [{"rank": 1}]
        assert body["limit_hit"] is True and body["consumed_use"] is True
    finally:
        server._SESSIONS.pop(sid, None)
