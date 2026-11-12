"""
What students append to the official rom does not overwrite original hidden correct rom
"""

import json
from xml.sax.saxutils import escape

import pytest
from fastapi.testclient import TestClient

from dlc.l3 import debugger
from dlc.l3 import official_store as ost
from dlc.testing.inject import check_rom_contents
from dlc.web import server
from dlc.web.server import app

client = TestClient(app)

W1, W2 = "0x00628433", "0x40628533"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_LIMITS_PATH", str(tmp_path / "limits.json"))
    monkeypatch.setenv("DLC_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.delenv("DLC_ENFORCE_LIMITS", raising=False)


def _builtin() -> str:
    return json.loads(open("data/official_tests_defaults.json", encoding="utf-8").read())[
        "cpu.dig"]["content"]


def _cpu(rom_words, rows: str) -> str:
    """A cpu.dig with a program ROM holding `rom_words` and a testcase."""
    data = ("" if rom_words is None else
            "<entry><string>Data</string><data>"
            + ",".join(f"{w:x}" for w in rom_words) + "</data></entry>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<circuit><version>2</version>'
        "<attributes/><visualElements>"
        "<visualElement><elementName>In</elementName><elementAttributes>"
        "<entry><string>Label</string><string>clk</string></entry>"
        '</elementAttributes><pos x="0" y="0"/></visualElement>'
        "<visualElement><elementName>ROM</elementName><elementAttributes>"
        "<entry><string>AddrBits</string><int>10</int></entry>"
        "<entry><string>Bits</string><int>32</int></entry>"
        "<entry><string>Label</string><string>Instruction Memory</string></entry>"
        "<entry><string>isProgramMemory</string><boolean>true</boolean></entry>"
        f'{data}</elementAttributes><pos x="200" y="0"/></visualElement>'
        "<visualElement><elementName>Testcase</elementName><elementAttributes>"
        "<entry><string>Label</string><string>cpu</string></entry>"
        "<entry><string>Testdata</string><testData><dataString>"
        f"{escape(rows)}</dataString></testData></entry>"
        '</elementAttributes><pos x="0" y="200"/></visualElement>'
        "</visualElements><wires/></circuit>\n")


def _save(content, words=None):
    body = {"filename": "cpu.dig", "content": content}
    if words is not None:
        body["program_words"] = words
    return client.post("/api/config/official_tests", json=body)


def _listed(name="cpu.dig"):
    return {t["filename"]: t for t in ost.list_tests()}[name]


# ------------------------------------------------------------- the store

def test_the_built_in_cpu_test_runs_one_row_per_registered_word():
    """The append rule rests on this: row k observes instruction k, so the
    first added row runs the first word after the registered program."""
    assert ost.builtin_row_count("cpu.dig") == len(ost.builtin_program("cpu.dig")) == 23


def test_the_listing_offers_the_word_box_for_the_program_test_only():
    cpu = _listed()
    assert (cpu["source"], cpu["editable"]) == ("default", True)
    assert (cpu["has_program"], cpu["base_rows"], cpu["program_extra"]) == (True, 23, [])
    assert "runtime" not in cpu
    other = _listed("register-file.dig")
    assert other["has_program"] is False and "program_extra" not in other


def test_added_rows_are_saved_with_one_instruction_word_each():
    r = _save(_builtin() + "\nC 1 2\nC 3 4", f"{W1} {W2}")
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "override"
    assert r.json()["program_extra"] == [W1, W2]
    mine = _listed()
    assert (mine["source"], mine["editable"]) == ("override", True)
    assert mine["program_extra"] == [W1, W2]
    assert ost.status_for("cpu.dig", _builtin() + "\nC 1 2\nC 3 4") == "official"
    assert ost.status_for("cpu.dig", _builtin()) == "modified"

    # only the student's own words are ever listed
    tests = client.get("/api/config/official_tests").json()["tests"]
    assert "runtime" not in json.dumps(tests)
    mine = next(t for t in tests if t["filename"] == "cpu.dig")
    assert json.dumps({k: v for k, v in mine.items() if k != "content"}).count("0x") == 2


def test_words_may_be_typed_one_per_line_or_on_one_line_in_any_case():
    for typed in (f"{W1}\n{W2}", f"{W1} {W2}", f"{W1},{W2}", f"  {W1.upper().replace('0X', '0x')}\n\n{W2}\n",
                  "0x628433 0x40628533"):
        assert _save(_builtin() + "\nC 1 2\nC 3 4", typed).status_code == 200, typed
        assert _listed()["program_extra"] == [W1, W2]


def test_the_word_count_must_equal_the_number_of_added_rows():
    for words, given in ((W1, 1), (f"{W1} {W2} {W1}", 3), ("", 0), (None, 0)):
        r = _save(_builtin() + "\nC 1 2\nC 3 4", words)
        assert r.status_code == 400
        assert (f"Rows added after the built-in 23: 2. Instruction words given: {given}."
                in r.json()["detail"])
    assert _listed()["source"] == "default"

    # rows written with a loop count as the rows Digital would run
    looped = _builtin() + "\nrepeat(3) C 1 2"
    assert _save(looped, f"{W1} {W2}").status_code == 400
    assert _save(looped, f"{W1} {W2} {W1}").status_code == 200


def test_a_word_is_0x_and_up_to_eight_hex_digits():
    for bad in ("628433", "0x", "0x123456789", "0xZZ", "12", f"{W1};{W2}"):
        r = _save(_builtin() + "\nC 1 2", bad)
        assert r.status_code == 400, bad
        assert "Instruction word 1 is not valid" in r.json()["detail"]
        assert "0x followed by up to 8 hex digits" in r.json()["detail"]
    assert ost.parse_program_words(["0x1", "0XAB"]) == ["0x00000001", "0x000000ab"]
    assert ost.parse_program_words(None) == [] == ost.parse_program_words("  \n ")


def test_the_built_in_rows_and_the_header_stay_as_they_are():
    lines = _builtin().split("\n")
    changed = "\n".join(lines[:6] + ["C 9999 9999"] + lines[7:])     # lines[0] is the header
    r = _save(changed + "\nC 1 2", W1)
    assert r.status_code == 400
    assert "first 23 rows of 'cpu.dig' are the built-in test" in r.json()["detail"]
    assert "row 6 was changed or removed" in r.json()["detail"]

    r = _save("\n".join(lines[:-1]), "")
    assert r.status_code == 400 and "row 23 was changed or removed" in r.json()["detail"]

    r = _save("\n".join(["clock A B"] + lines[1:]) + "\nC 1 2", W1)
    assert r.status_code == 400
    assert "header line of 'cpu.dig' must stay as it is: clk ReadData1 ReadData2" in r.json()["detail"]

    # an own row slipped in front of the built-in rows is a changed row too
    r = _save("\n".join([lines[0], "C 1 2"] + lines[1:]), W1)
    assert r.status_code == 400 and "row 1 was changed or removed" in r.json()["detail"]
    assert _listed()["source"] == "default"


def test_comments_and_spacing_in_the_built_in_rows_are_free():
    lines = _builtin().split("\n")
    spaced = "\n".join([lines[0] + "   # header", ""] + ["  " + ln.replace(" ", "\t") for ln in lines[1:]]
                       + ["# my rows", "C 1 2  # add"])
    assert _save(spaced, W1).status_code == 200
    assert _listed()["program_extra"] == [W1]


def test_saving_the_built_in_rows_unchanged_keeps_or_restores_the_default():
    r = _save(_builtin(), "")
    assert r.status_code == 200 and r.json()["source"] == "default"
    assert _listed()["source"] == "default"

    assert _save(_builtin() + "\nC 1 2", W1).status_code == 200
    assert _listed()["source"] == "override"
    r = _save(_builtin() + "\n# nothing of my own any more\n", "")
    assert r.status_code == 200 and r.json()["source"] == "default"
    assert _listed()["source"] == "default" and _listed()["program_extra"] == []

    assert _save(_builtin() + "\nC 1 2", W1).status_code == 200
    assert client.delete("/api/config/official_tests",
                         params={"filename": "cpu.dig"}).json()["removed"] is True
    assert _listed()["source"] == "default"


def test_a_test_without_a_registered_program_takes_no_words():
    r = client.post("/api/config/official_tests", json={
        "filename": "mylab.dig", "content": "A Y\n0 1", "program_words": W1})
    assert r.status_code == 400 and "takes no instruction words" in r.json()["detail"]
    r = client.post("/api/config/official_tests", json={
        "filename": "mylab.dig", "content": "A Y\n0 1", "program_words": ""})
    assert r.status_code == 200 and r.json()["source"] == "user"
    assert _listed("mylab.dig")["editable"] is True
    assert _listed("mylab.dig")["has_program"] is False


# --------------------------------------------------------- the ROM check

def test_the_rom_holds_the_registered_program_followed_by_the_added_words(tmp_path):
    base = ost.builtin_program("cpu.dig")
    p = tmp_path / "cpu.dig"
    assert _save(_builtin() + "\nC 1 2\nC 3 4", f"{W1} {W2}").status_code == 200
    assert ost.program_words("cpu.dig") == (base, [0x00628433, 0x40628533])
    assert ost.get_runtime_payload("cpu.dig", "rom").endswith(",628433,40628533")

    p.write_text(_cpu(base + [0x00628433, 0x40628533], _builtin()), encoding="utf-8")
    assert check_rom_contents(str(p), "cpu.dig") is None

    p.write_text(_cpu(base, _builtin()), encoding="utf-8")
    v = check_rom_contents(str(p), "cpu.dig")
    assert (v["status"], v["reason"], v["first_bad_address"]) == ("mismatch", "own_words", 23)
    assert "holds the official program" in v["message"]
    assert "0 in the ROM, 2 registered; first difference at address 23" in v["message"]
    assert "Settings → Official tests" in v["message"]

    p.write_text(_cpu(base + [0x00628433, 0x7], _builtin()), encoding="utf-8")
    v = check_rom_contents(str(p), "cpu.dig")
    assert (v["reason"], v["first_bad_address"]) == ("own_words", 24)
    assert "2 in the ROM, 2 registered" in v["message"]

    broken = [base[0] ^ 1] + base[1:] + [0x00628433, 0x40628533]
    p.write_text(_cpu(broken, _builtin()), encoding="utf-8")
    v = check_rom_contents(str(p), "cpu.dig")
    assert (v["reason"], v["first_bad_address"]) == ("official", 0)
    assert "does not hold the official contents" in v["message"]
    for w in base:
        assert f"{w:x}" not in v["message"].replace(f"{broken[0]:x}", "")


def test_unregistered_words_after_the_official_program_get_their_own_message(tmp_path):
    base = ost.builtin_program("cpu.dig")
    p = tmp_path / "cpu.dig"
    p.write_text(_cpu(base + [0x00628433], _builtin()), encoding="utf-8")
    v = check_rom_contents(str(p), "cpu.dig")
    assert (v["status"], v["reason"], v["first_bad_address"]) == ("mismatch", "unregistered", 23)
    assert "then more words that are not registered (count: 1)" in v["message"]
    assert "add their rows and these words in Settings → Official tests" in v["message"]


def test_a_registered_zero_word_at_the_end_needs_no_rom_entry(tmp_path):
    base = ost.builtin_program("cpu.dig")
    assert _save(_builtin() + "\nC 1 2\nC 3 4", f"{W1} 0x0").status_code == 200
    p = tmp_path / "cpu.dig"
    p.write_text(_cpu(base + [0x00628433], _builtin()), encoding="utf-8")
    assert check_rom_contents(str(p), "cpu.dig") is None


def test_a_version_stored_without_words_is_not_checked(tmp_path):
    """Adopted from a circuit whose ROM is some other program: the rows
    describe that program, so there is nothing registered to compare."""
    ost.save_test("cpu.dig", "clk ReadData1 ReadData2\nC 1 2", allow_default_override=True)
    assert ost.source_of("cpu.dig") == "override"
    assert ost.program_words("cpu.dig") is None
    assert ost.get_runtime_payload("cpu.dig", "rom") is None
    assert _listed()["program_extra"] is None
    p = tmp_path / "cpu.dig"
    p.write_text(_cpu([1, 2, 3], _builtin()), encoding="utf-8")
    assert check_rom_contents(str(p), "cpu.dig") is None
    ost.delete_test("cpu.dig")
    assert ost.get_runtime_payload("cpu.dig", "rom")


def test_layer_1_keeps_its_empty_rom_note_with_added_rows_registered():
    assert _save(_builtin() + "\nC 1 2", W1).status_code == 200
    r = client.post("/api/circuit", files=[
        ("files", ("cpu.dig", _cpu(None, _builtin()).encode(), "application/xml"))])
    assert r.status_code == 200
    issues = r.json()["files"][0]["issues"]
    notes = [i["message"] for i in issues if i["kind"] == "empty_rom"]
    assert notes and all("this lab registers the official contents of this ROM" in m for m in notes)


# ------------------------------------------------- Mode B: adopt, Mode A

def _session_with_coach_temp(tmp_path, rom_words, rows):
    r = client.post("/api/circuit", files=[
        ("files", ("cpu.dig", _cpu(ost.builtin_program("cpu.dig"), _builtin()).encode(),
                   "application/xml"))])
    assert r.status_code == 200
    sid = r.json()["session_id"]
    temp = tmp_path / "coach_temp.dig"
    temp.write_text(_cpu(rom_words, rows), encoding="utf-8")
    server._SESSIONS[sid]["files"].append({"name": "cpu__coach.dig", "path": str(temp)})
    server._SESSIONS[sid]["l3_temp"] = {
        "for": "cpu.dig", "name": "cpu__coach.dig", "path": str(temp),
        "spec_name": "cpu", "coach_rows": [23, 24]}
    return sid


def _adopt(sid):
    return client.post("/api/l3/adopt_official",
                       json={"session_id": sid, "filename": "cpu.dig"}).json()


def test_adopting_a_program_extension_stores_only_the_new_words(tmp_path):
    base = ost.builtin_program("cpu.dig")
    rows = _builtin() + "\nC 1 2\nC 3 4"
    sid = _session_with_coach_temp(tmp_path, base + [0x00628433, 0x40628533], rows)
    try:
        body = _adopt(sid)
        assert body["ok"] is True and body["rows"] == 25 and body["program_words"] == 2
        mine = _listed()
        assert mine["source"] == "override" and mine["program_extra"] == [W1, W2]
        assert ost.status_for("cpu.dig", rows) == "official"
        assert ost.program_words("cpu.dig") == (base, [0x00628433, 0x40628533])
        # what was adopted can be saved again by hand as it stands
        assert _save(mine["content"], "\n".join(mine["program_extra"])).status_code == 200
    finally:
        server._SESSIONS.pop(sid, None)


def test_adoption_reads_the_rom_as_mode_b_writes_it(tmp_path):
    """The temp circuit is built with the same two text edits Mode B's
    program extension uses: words appended to the ROM data, rows appended
    to the testcase."""
    from dlc.l3 import oracle

    base = ost.builtin_program("cpu.dig")
    text = _cpu(base, _builtin())
    text = oracle.extend_program_rom_text(text, [0x00628433, 0x40628533])
    text = oracle.inject_rows_text(text, 0, [oracle.InjectedRow("C 1 2"),
                                             oracle.InjectedRow("C 3 4")])
    sid = _session_with_coach_temp(tmp_path, base, _builtin())
    try:
        (tmp_path / "coach_temp.dig").write_text(text, encoding="utf-8")
        body = _adopt(sid)
        assert body["ok"] is True and body["rows"] == 25 and body["program_words"] == 2
        assert _listed()["program_extra"] == [W1, W2]
    finally:
        server._SESSIONS.pop(sid, None)


def test_adopted_rows_past_the_end_of_the_rom_get_the_zero_word(tmp_path):
    base = ost.builtin_program("cpu.dig")
    sid = _session_with_coach_temp(tmp_path, base + [0x00628433],
                                   _builtin() + "\nC 1 2\nC X X\nC X X")
    try:
        body = _adopt(sid)
        assert body["ok"] is True and body["program_words"] == 3
        assert _listed()["program_extra"] == [W1, "0x00000000", "0x00000000"]
        p = tmp_path / "cpu.dig"
        p.write_text(_cpu(base + [0x00628433], _builtin()), encoding="utf-8")
        assert check_rom_contents(str(p), "cpu.dig") is None
    finally:
        server._SESSIONS.pop(sid, None)


def test_adopting_from_another_program_stores_the_rows_without_words(tmp_path):
    sid = _session_with_coach_temp(tmp_path, [0x11, 0x22, 0x33],
                                   "clk ReadData1 ReadData2\nC 1 2\nC 3 4")
    try:
        body = _adopt(sid)
        assert body["ok"] is True and body["program_words"] == 0
        assert _listed()["program_extra"] is None
        assert ost.get_runtime_payload("cpu.dig", "rom") is None
    finally:
        server._SESSIONS.pop(sid, None)


# ------------------------------------- a table ROM: the control unit's

def _control_unit(rom_words, rows: str) -> str:
    return (_cpu(rom_words, rows)
            .replace("<entry><string>isProgramMemory</string><boolean>true</boolean></entry>", "")
            .replace("Instruction Memory", "Control ROM"))


def test_the_control_rom_table_is_checked_and_never_listed(tmp_path):
    table = ost.builtin_program("control-unit.dig")
    cu = _listed("control-unit.dig")
    assert (cu["source"], cu["editable"]) == ("default", False)
    assert cu["program_extra"] == [] and "runtime" not in cu

    p = tmp_path / "control-unit.dig"
    rows = ost.get_content("control-unit.dig")
    p.write_text(_control_unit(table, rows), encoding="utf-8")
    assert check_rom_contents(str(p), "control-unit.dig") is None

    wrong = table[:4] + [table[4] ^ 1] + table[5:]
    p.write_text(_control_unit(wrong, rows), encoding="utf-8")
    v = check_rom_contents(str(p), "control-unit.dig")
    assert (v["status"], v["reason"], v["first_bad_address"]) == ("mismatch", "official", 4)
    assert v["rom"] == "Control ROM" and "1 of 11 words differ" in v["message"]
    assert f"your word there is {wrong[4]:x}" in v["message"]

    top = tmp_path / "cpu.dig"
    top.write_text(_cpu(ost.builtin_program("cpu.dig"), _builtin()).replace(
        "</visualElements>",
        "<visualElement><elementName>control-unit.dig</elementName><elementAttributes/>"
        '<pos x="600" y="0"/></visualElement></visualElements>'), encoding="utf-8")
    v = check_rom_contents(str(top), "cpu.dig")
    assert v is not None and v["file"] == "control-unit.dig" and v["first_bad_address"] == 4


def test_rows_adopted_for_the_control_unit_bring_no_words_and_keep_the_check(tmp_path):
    table = ost.builtin_program("control-unit.dig")
    rows = ost.get_content("control-unit.dig")
    more = rows + "\n" + rows.split("\n")[1] + "\n" + rows.split("\n")[2]
    r = client.post("/api/circuit", files=[
        ("files", ("control-unit.dig", _control_unit(table, rows).encode(), "application/xml"))])
    sid = r.json()["session_id"]
    temp = tmp_path / "coach_temp.dig"
    temp.write_text(_control_unit(table, more), encoding="utf-8")
    server._SESSIONS[sid]["files"].append({"name": "control-unit__coach.dig", "path": str(temp)})
    try:
        body = client.post("/api/l3/adopt_official",
                           json={"session_id": sid, "filename": "control-unit.dig"}).json()
        assert body["ok"] is True and body["rows"] == 29 and body["program_words"] == 0
        cu = _listed("control-unit.dig")
        assert (cu["source"], cu["program_extra"]) == ("override", [])
        assert ost.program_words("control-unit.dig") == (table, [])
        p = tmp_path / "control-unit.dig"
        p.write_text(_control_unit(table[:-1] + [table[-1] ^ 1], rows), encoding="utf-8")
        assert check_rom_contents(str(p), "control-unit.dig")["reason"] == "official"
    finally:
        server._SESSIONS.pop(sid, None)


def test_mode_a_on_the_coach_temp_accepts_the_words_the_coach_appended(tmp_path, monkeypatch):
    def fake(path, **kw):
        fake.calls.append(path)
        return {"ok": True, "mode": "clear", "cards": [], "notes": []}
    fake.calls = []
    monkeypatch.setattr(debugger, "debug_circuit", fake)
    base = ost.builtin_program("cpu.dig")
    rows = _builtin() + "\nC 1 2\nC 3 4"

    sid = _session_with_coach_temp(tmp_path, base + [0x00628433, 0x40628533], rows)
    try:
        body = client.post("/api/llm/debug", json={"session_id": sid, "filename": "cpu.dig"}).json()
        assert body["mode"] == "clear" and body["on_coach_temp"] is True
        assert fake.calls and fake.calls[-1].endswith("coach_temp.dig")
    finally:
        server._SESSIONS.pop(sid, None)

    # the registered part still has to be right on the temp copy
    sid = _session_with_coach_temp(tmp_path, [base[0] ^ 1] + base[1:] + [0x00628433], rows)
    try:
        body = client.post("/api/llm/debug", json={"session_id": sid, "filename": "cpu.dig"}).json()
        assert body["mode"] == "rom_mismatch" and body["rom_check"]["reason"] == "official"
    finally:
        server._SESSIONS.pop(sid, None)
