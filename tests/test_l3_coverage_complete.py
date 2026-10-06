"""
Mode B stops when there is nothing left to propose: a combinational
circuit whose testcase already holds every possible input vector, a
proposed row whose inputs an existing row already tests, and manifest
categories that do not fit the testcase's columns. Plus the injected
temp file shared by concurrent requests.
"""

import json
import os
import re
import threading

import pytest
from fastapi.testclient import TestClient

from dlc.l3 import proposer
from dlc.l3.coverage import scan_tree_coverage
from dlc.testing import inject
from dlc.web import server
from dlc.web.server import app

client = TestClient(app)

_AND = "data/sample_circuits/tier1_minimal/single_and.dig"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_LIMITS_PATH", str(tmp_path / "limits.json"))
    monkeypatch.setenv("DLC_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.setenv("DLC_OFFICIAL_TESTS_PATH", str(tmp_path / "official.json"))
    monkeypatch.delenv("DLC_ENFORCE_LIMITS", raising=False)


def _and_with_rows(tmp_path, rows, name="single_and.dig"):
    xml = open(_AND, encoding="utf-8").read()
    data = "A B Y\n" + "\n".join(rows)
    xml = re.sub(r"<dataString>.*?</dataString>",
                 f"<dataString>{data}</dataString>", xml, flags=re.S)
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return p


def _dead(prompt, **_kw):
    raise AssertionError("the model must not be called")


def _fake_rows(rows, path):
    spec_name = proposer.build_targets(scan_tree_coverage(str(path)))[0]["spec_name"]
    text = json.dumps({"proposals": [
        {"file": "single_and.dig", "spec_name": spec_name, "rows": rows,
         "why": "x"}]})
    n = {"v": 0}

    def call(prompt, **kw):
        n["v"] += 1
        return {"ok": True, "text": text if n["v"] == 1 else '{"rows": []}',
                "error": None, "usage": None, "model": kw.get("model")}
    call.count = n
    return call


# --- 100% coverage: nothing to propose ------------------------------------

def test_full_input_space_stops_before_the_model():
    r = scan_tree_coverage(_AND)
    c = r.circuits[0]
    assert c.input_space == 4 and c.distinct_vectors == 4
    out = proposer.propose_rows(_AND, call=_dead)
    assert out["ok"] is True and out["proposals"] == []
    assert out["all_categories_covered"] == ["single_and.dig"]
    assert out["model"] is None
    assert out["notes"] == ["single_and.dig: all 4 possible input vectors are "
                            "already tested (100% coverage) — the coach has "
                            "nothing to add."]


def test_partial_coverage_still_asks_the_model(tmp_path):
    p = _and_with_rows(tmp_path, ["0 0 0", "1 1 1"])
    fake = _fake_rows(["1 0 0"], p)
    out = proposer.propose_rows(str(p), call=fake)
    assert out["ok"] is True and fake.count["v"] >= 1
    assert [g["rows"] for g in out["proposals"]] == [["1 0 0"]]


def test_full_coverage_propose_route_refunds_the_scan(monkeypatch):
    monkeypatch.setattr(proposer, "call_llm", _dead)
    with open(_AND, "rb") as fh:
        sid = client.post("/api/circuit", files=[
            ("files", ("single_and.dig", fh, "application/xml"))]).json()["session_id"]
    try:
        scan = client.post("/api/l3/coverage", json={
            "session_id": sid, "filename": "single_and.dig"}).json()
        assert scan["consumed_use"] is True
        body = client.post("/api/l3/propose", json={
            "session_id": sid, "filename": "single_and.dig"}).json()
        assert body["ok"] is True and body["proposals"] == []
        assert body["all_categories_covered"] == ["single_and.dig"]
        assert body["refunded"] is True
        assert body["limits"]["used"]["modeB"] == 0
        assert "100% coverage" in " ".join(body["notes"])
    finally:
        server._SESSIONS.pop(sid, None)


# --- same inputs as an existing row ----------------------------------------

def test_a_row_repeating_an_existing_input_vector_is_rejected(tmp_path):
    p = _and_with_rows(tmp_path, ["0 0 0", "1 1 1"])
    fake = _fake_rows(["0 0 1", "1 0 0", "1 0 1", "0 1 0"], p)
    out = proposer.propose_rows(str(p), call=fake)
    assert [g["rows"] for g in out["proposals"]] == [["1 0 0", "0 1 0"]]
    rejected = {r["rows"][0]: r for r in out["rejected"]}
    assert set(rejected) == {"0 0 1", "1 0 1"}
    assert all("duplicate input vector" in r["reason"] for r in rejected.values())
    assert all(r["kind"] == "duplicate" for r in rejected.values())


def test_input_dedupe_only_for_combinational_targets():
    t = {"file": "f.dig", "spec_name": "T", "headers": ["A", "B", "Y"],
         "inputs": [{"label": "A", "bits": 1}, {"label": "B", "bits": 1}],
         "outputs": [{"label": "Y", "bits": 1}], "existing_rows": ["0 0 0"],
         "has_clock": False}
    assert proposer._input_columns(t) == [0, 1]
    assert proposer._input_columns({**t, "has_clock": True}) is None
    assert proposer._input_columns({**t, "headers": ["A", "Y"]}) is None
    valid, rejected = proposer.validate_and_dedupe(
        [{"file": "f.dig", "spec_name": "T", "rows": ["0 0 1"], "why": ""}], [t])
    assert valid == [] and "duplicate input vector" in rejected[0]["reason"]
    valid, rejected = proposer.validate_and_dedupe(
        [{"file": "f.dig", "spec_name": "T", "rows": ["0 0 1"], "why": ""}],
        [{**t, "has_clock": True}])
    assert [g["rows"] for g in valid] == [["0 0 1"]] and rejected == []


def test_dedupe_sees_rows_beyond_the_prompt_window():
    rows = [f"{i % 2} {i // 2 % 2} 0" for i in range(4)] * 20   # 80 rows
    t = {"file": "f.dig", "spec_name": "T", "headers": ["A", "B", "Y"],
         "inputs": [{"label": "A", "bits": 1}, {"label": "B", "bits": 1}],
         "outputs": [{"label": "Y", "bits": 1}],
         "existing_rows": rows[:40], "_existing_all": rows, "has_clock": False}
    valid, rejected = proposer.validate_and_dedupe(
        [{"file": "f.dig", "spec_name": "T", "rows": ["1 1 1"], "why": ""}], [t])
    assert valid == [] and rejected


def test_prompt_hides_bookkeeping_keys():
    r = scan_tree_coverage(_AND)
    targets = proposer.build_targets(r)
    assert "_existing_all" in targets[0]
    prompt = proposer.build_prompt(r, targets)
    assert "_existing_all" not in prompt
    assert '"existing_rows"' in prompt


# --- manifest categories must fit the testcase's columns -------------------

def test_categories_needing_a_missing_column_are_not_attached(tmp_path, monkeypatch):
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    (mdir / "and.json").write_text(json.dumps({
        "lab": "and", "applies_to": ["single_and.dig"],
        "categories": {"single_and.dig": [
            {"name": "both_high_loaded", "when": {"A": 1, "B": 1, "load": 1}}]},
        "official_tests": {}, "reference_dir": None}))
    monkeypatch.setenv("DLC_MANIFEST_DIR", str(mdir))
    p = _and_with_rows(tmp_path, ["0 0 0", "1 1 1"])
    seen = {}

    def fake(prompt, **kw):
        seen["prompt"] = prompt
        return {"ok": True, "text": "{}", "error": None, "usage": None, "model": "m"}
    proposer.propose_rows(str(p), call=fake)
    assert '"both_high_loaded"' not in seen["prompt"]
    assert proposer._categories_fit(
        [{"name": "x", "when": {"A": 1}}], ["A", "B", "Y"]) is True
    assert proposer._categories_fit(
        [{"name": "x", "when": {"A": 1, "load": 1}}], ["A", "B", "Y"]) is False


# --- the injected temp survives concurrent requests -------------------------

def test_injected_temp_is_shared_until_its_last_user_is_done(tmp_path):
    from dlc.l3 import official_store
    p = _and_with_rows(tmp_path, ["0 0 0"], name="off.dig")
    official_store.save_test("off.dig", "A B Y\n0 0 0\n1 1 1",
                             allow_default_override=True)
    t1, n1 = inject.prepare_injected_run(str(p), "off.dig")
    t2, n2 = inject.prepare_injected_run(str(p), "off.dig")
    assert t1 and t1 == t2 and os.path.exists(t1)
    inject.cleanup_injected(t1)
    assert os.path.exists(t1), "the second request is still using the copy"
    inject.cleanup_injected(t2)
    assert not os.path.exists(t1)
    inject.cleanup_injected(t2)
    assert not list(tmp_path.glob(".dlc_injected__*"))


def test_concurrent_row_simulations_on_an_injected_file_all_succeed(tmp_path):
    from dlc.l3 import official_store
    p = _and_with_rows(tmp_path, ["0 0 0", "1 1 1", "0 1 0", "1 0 0"], name="off.dig")
    official_store.save_test("off.dig", "A B Y\n0 0 0\n1 1 1\n0 1 0\n1 0 0\n0 0 0",
                             allow_default_override=True)
    with open(p, "rb") as fh:
        sid = client.post("/api/circuit", files=[
            ("files", ("off.dig", fh, "application/xml"))]).json()["session_id"]
    try:
        out: list[dict] = []

        def one(row):
            out.append(client.post("/api/simulate", json={
                "session_id": sid, "filename": "off.dig",
                "spec_index": 0, "row_index": row}).json())
        for _ in range(5):
            server._ROW_REPLAYS.clear()
            threads = [threading.Thread(target=one, args=(r,)) for r in (0, 1, 2, 3)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        assert all(o["ok"] for o in out), [o.get("warning") for o in out if not o["ok"]]
        assert all(o["node_svgs"] == {} or isinstance(o["node_svgs"], dict) for o in out)
        folder = os.path.dirname(server._SESSIONS[sid]["files"][0]["path"])
        assert not [f for f in os.listdir(folder) if f.startswith(".dlc_injected__")]
    finally:
        server._SESSIONS.pop(sid, None)
