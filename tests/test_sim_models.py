"""
Formula models for known lab subcircuits (dlc.sim.models) and the
single-pass row replay (RowReplay / simulate_rows).
"""

from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.graph import build_signal_graph
from dlc.parser.netlist import build_netlist
from dlc.sim import models as M
from dlc.sim import simulator as sim
from dlc.sim.simulator import RowReplay, simulate, simulate_rows, simulate_sequential
from dlc.testing.spec import extract_test_specs

_BASE = "data/sample_circuits"


def _ev(name, **inp):
    model = M.BY_NAME[name]
    outs, _ = model.evaluate(inp, None)
    return outs


# per-model formulas

def test_alu_ops_and_zero_flag():
    assert _ev("rv32i_alu", A=7, B=7, ALUOp=6) == {"Result": 0, "FlagZ": 1}
    assert _ev("rv32i_alu", A=0xFFFFFFFF, B=5, ALUOp=2)["Result"] == 4
    assert _ev("rv32i_alu", A=0xFFFFFFFF, B=5, ALUOp=7)["Result"] == 1   # -1 < 5
    assert _ev("rv32i_alu", A=0xFFFFFFFF, B=5, ALUOp=9)["Result"] == 0   # unsigned
    assert _ev("rv32i_alu", A=7, B=7, ALUOp=4)["Result"] == 7 << 7        # A by B
    assert _ev("rv32i_alu", A=0xFFFFFFFF, B=5, ALUOp=8)["Result"] == 0xFFFFFFFF
    assert _ev("rv32i_alu", A=0xFFFFFFFF, B=5, ALUOp=5)["Result"] == 0x07FFFFFF
    assert _ev("rv32i_alu", A=7, B=7, ALUOp=12)["Result"] == 14           # falls to ADD


def test_control_tables():
    add = _ev("rv32i_control", opcode=0b0110011, funct3=0, funct7=0)
    assert (add["RegWrite"], add["ALUSrc"], add["ALUOp1"], add["ALUOp0"]) == (1, 0, 1, 0)
    beq = _ev("rv32i_control", opcode=0b1100011, funct3=0, funct7=0)
    assert beq["Branch"] == 1 and beq["RegWrite"] == 0
    assert (beq["ImmSrc2"], beq["ImmSrc1"], beq["ImmSrc0"]) == (0, 1, 0)
    unknown = _ev("rv32i_control", opcode=0b1111111, funct3=0, funct7=0)
    assert not any(unknown.values())
    old_unknown = _ev("lab5_control_bits", opcode=0b1111111, funct3=0, funct7=0)
    assert old_unknown["RegWrite"] == 1 and old_unknown["ALUOp1"] == 1
    assert "Branch" not in old_unknown

_LAB5_CONTROL_ROWS = """
51 0 0 1 0 0 2
51 0 32 1 0 0 6
51 7 0 1 0 0 0
51 6 0 1 0 0 1
51 2 0 1 0 0 7
19 0 0 1 1 0 2
19 0 32 1 1 0 2
19 0 63 1 1 0 2
19 0 64 1 1 0 2
19 0 127 1 1 0 2
19 2 0 1 1 0 7
19 2 32 1 1 0 7
19 2 63 1 1 0 7
19 2 64 1 1 0 7
19 2 127 1 1 0 7
19 7 0 1 1 0 0
19 7 32 1 1 0 0
19 7 63 1 1 0 0
19 7 64 1 1 0 0
19 7 127 1 1 0 0
51 1 0 1 0 0 4
51 5 0 1 0 0 5
51 5 32 1 0 0 8
0 0 0 0 0 0 2
51 0 1 0 0 0 2
51 4 0 0 0 0 2
19 5 32 0 0 0 2
"""


def test_lab5_control_follows_the_fall_2026_test_row_by_row():
    rows = [[int(v) for v in ln.split()] for ln in _LAB5_CONTROL_ROWS.split("\n") if ln.strip()]
    assert len(rows) == 27
    for opcode, f3, f7, rw, src, imm, alu in rows:
        got = _ev("lab5_control", opcode=opcode, funct3=f3, funct7=f7)
        assert got == {"RegWrite": rw, "ALUSrc": src, "ImmSrc": imm, "ALUOp": alu}, (opcode, f3, f7)


def test_shipped_control_unit_test_is_the_fall_2026_one():
    import json
    from pathlib import Path
    from dlc.l3 import manifest as mf
    entry = json.loads(Path("data/official_tests_defaults.json")
                       .read_text(encoding="utf-8"))["control-unit.dig"]
    lines = [ln for ln in entry["content"].split("\n") if ln.strip()]
    assert lines[0].split() == ["opcode", "funct3", "funct7", "RegWrite",
                                "ALUSrc", "ImmSrc", "ALUOp"]
    assert [ln.split() for ln in lines[1:]] == [
        ln.split() for ln in _LAB5_CONTROL_ROWS.split("\n") if ln.strip()]
    assert "runtime" not in entry
    lab5 = json.loads(Path("data/manifests/cpu.json").read_text(encoding="utf-8"))
    assert mf.normalized_test_hash(entry["content"]) == entry["sha1"] \
        == lab5["official_tests"]["control-unit.dig"]


def _control_child(tmp_path, outputs):
    """A control-unit file with the given output pins and no logic."""
    def ve(name, attrs, x, y):
        a = "".join(f"<entry><string>{k}</string><{t}>{v}</{t}></entry>" for k, t, v in attrs)
        return (f"<visualElement><elementName>{name}</elementName><elementAttributes>{a}"
                f'</elementAttributes><pos x="{x}" y="{y}"/></visualElement>')
    els = "".join(ve("In", [("Label", "string", n), ("Bits", "int", b)], 0, 40 * i)
                  for i, (n, b) in enumerate((("opcode", 7), ("funct3", 3), ("funct7", 7))))
    els += "".join(ve("Out", [("Label", "string", n), ("Bits", "int", b)], 400, 40 * i)
                   for i, (n, b) in enumerate(outputs))
    p = tmp_path / "control-unit.dig"
    p.write_text('<?xml version="1.0" encoding="utf-8"?>\n<circuit><version>2</version><attributes/>'
                 f"<visualElements>{els}</visualElements><wires/></circuit>\n", encoding="utf-8")
    from dlc.parser.dig_parser import parse_dig_file
    return parse_dig_file(str(p))


def test_both_control_unit_pin_sets_find_their_model(tmp_path):
    from dlc.sim import models
    buses = _control_child(tmp_path, (("RegWrite", 1), ("ALUSrc", 1), ("ImmSrc", 2), ("ALUOp", 4)))
    assert [m.name for m in models.candidates(buses)] == ["lab5_control"]
    bits = _control_child(tmp_path, tuple((n, 1) for n in (
        "RegWrite", "ALUSrc", "ImmSrc1", "ImmSrc0", "ALUOp3", "ALUOp2", "ALUOp1", "ALUOp0")))
    assert [m.name for m in models.candidates(bits)][0] == "lab5_control_bits"


def test_immediates_of_one_instruction_word():
    word = 0xFEC08FE3
    assert _ev("rv32i_immgen", Instr=word, ImmSrc=0)["Imm"] == 0xFFFFFFEC
    assert _ev("rv32i_immgen", Instr=word, ImmSrc=1)["Imm"] == 0xFFFFFFFF
    assert _ev("rv32i_immgen", Instr=word, ImmSrc=2)["Imm"] == 0xFFFFFFFE
    assert _ev("rv32i_immgen", Instr=word, ImmSrc=3)["Imm"] == 0xFEC08000
    assert _ev("rv32i_immgen", Instr=word, ImmSrc=4)["Imm"] == 0xFFF087EC
    assert _ev("rv32i_immgen", Instr=word, ImmSrc=6)["Imm"] == 0xFFFFFFEC


def test_branch_conditions_signed_vs_unsigned():
    lt = lambda f3: _ev("rv32i_branch_unit", A=0xFFFFFFFF, B=1, funct3=f3,
                        Branch=1, Jump=0)["Taken"]
    assert (lt(4), lt(5), lt(6), lt(7)) == (1, 0, 0, 1)
    assert _ev("rv32i_branch_unit", A=5, B=6, funct3=0, Branch=1, Jump=0)["Taken"] == 0
    assert _ev("rv32i_branch_unit", A=5, B=6, funct3=0, Branch=0, Jump=1)["Taken"] == 1


def test_register_file_state_and_x0():
    rf = M.BY_NAME["rv32i_register_file"]
    outs, st = rf.evaluate({"ReadReg1": 0, "ReadReg2": 5, "WriteReg": 5,
                            "WriteData": 0x55, "RegWrite": 1, "Clock": 1}, None)
    assert outs == {"ReadData1": 0, "ReadData2": 0}      # reads precede the edge
    outs, st = rf.evaluate({"ReadReg1": 5, "ReadReg2": 0, "WriteReg": 0,
                            "WriteData": 0x77, "RegWrite": 1, "Clock": 1}, st)
    assert outs["ReadData1"] == 0x55
    assert st.get(0, 0) == 0                              # x0 write ignored
    partial = rf.evaluate({"ReadReg1": 5, "ReadReg2": 5}, st)
    assert partial is not None and partial[0]["ReadData1"] == 0x55
    assert partial[1] == st                               # no commit without WriteData


def test_data_memory_sub_word_read_modify_write():
    dm = M.BY_NAME["rv32i_data_memory"]
    _, st = dm.evaluate({"Addr": 0, "WriteData": 0xAABBCCDD, "MemWrite": 1,
                         "funct3": 2, "Clock": 1}, None)
    assert dm.evaluate({"Addr": 0, "funct3": 0}, st)[0]["ReadData"] == 0xFFFFFFDD
    assert dm.evaluate({"Addr": 1, "funct3": 4}, st)[0]["ReadData"] == 0xCC
    assert dm.evaluate({"Addr": 2, "funct3": 5}, st)[0]["ReadData"] == 0xAABB
    _, st = dm.evaluate({"Addr": 1, "WriteData": 0x55, "MemWrite": 1,
                         "funct3": 0, "Clock": 1}, st)
    assert dm.evaluate({"Addr": 0, "funct3": 2}, st)[0]["ReadData"] == 0xAABB55DD
    assert dm.evaluate({"Addr": 128, "funct3": 2}, st)[0]["ReadData"] == 0xAABB55DD
    assert dm.evaluate({"Addr": 3, "funct3": 2}, st)[0]["ReadData"] == 0xAABB55DD


def test_add_sub_boolean_shifter_slt():
    assert _ev("add_sub", A=0x7FFFFFFF, B=1, Sub=0) == {"Out": 0x80000000, "Overflow": 1, "Sign": 1}
    assert _ev("add_sub", A=3, B=5, Sub=1) == {"Out": 0xFFFFFFFE, "Overflow": 0, "Sign": 1}
    assert _ev("boolean_unit", A=0xF0F0F0F0, B=0x0FF0FF0F, Bool=3)["Out"] == 0x000F0000
    assert _ev("bidirectional_shifter", A=1, B=0x80000001, Bool=3)["Out"] == 0xC0000000
    assert _ev("bidirectional_shifter", A=33, B=0x80000001, Bool=0)["Out"] == 0
    assert _ev("bidirectional_shifter", A=4, B=0x80000001, Bool=1)["Out"] == 0
    assert _ev("slt_unit", Sign=1, Overflow=1)["Out"] == 0


# matching, validation and manifest overrides

def _child_xml(ins, outs, testdata=None):
    def elem(name, label, bits, x, y):
        return (f'<visualElement><elementName>{name}</elementName>'
                '<elementAttributes>'
                f'<entry><string>Label</string><string>{label}</string></entry>'
                f'<entry><string>Bits</string><int>{bits}</int></entry>'
                f'</elementAttributes><pos x="{x}" y="{y}"/></visualElement>')
    body = "".join(elem("In", n, b, 0, i * 60) for i, (n, b) in enumerate(ins))
    body += "".join(elem("Out", n, b, 400, i * 60) for i, (n, b) in enumerate(outs))
    if testdata:
        body += ('<visualElement><elementName>Testcase</elementName>'
                 '<elementAttributes><entry><string>Testdata</string>'
                 f'<testData><dataString>{testdata}</dataString></testData>'
                 '</entry></elementAttributes><pos x="0" y="500"/></visualElement>')
    return ('<?xml version="1.0" encoding="utf-8"?><circuit><version>2</version>'
            f'<attributes/><visualElements>{body}</visualElements><wires/></circuit>')


_BOOL_IF = ([("A", 32), ("B", 32), ("Bool", 2)], [("Out", 32)])


def test_identical_interfaces_are_told_apart_by_the_childs_own_test(tmp_path):
    p = tmp_path / "boolean-unit.dig"
    p.write_text(_child_xml(*_BOOL_IF, testdata="A B Bool Out\n12 10 0 8\n12 10 1 14\n"))
    child = parse_dig_file(str(p))
    names = [m.name for m in M.candidates(child)]
    assert names == ["boolean_unit", "bidirectional_shifter"]
    assert M.validate(M.BY_NAME["boolean_unit"], child)[0] is True
    ok, detail = M.validate(M.BY_NAME["bidirectional_shifter"], child)
    assert ok is False and "row 0" in detail


def test_child_without_testcase_is_not_substituted(tmp_path):
    p = tmp_path / "boolean-unit.dig"
    p.write_text(_child_xml(*_BOOL_IF))
    child = parse_dig_file(str(p))
    assert M.validate(M.BY_NAME["boolean_unit"], child) == (False, "no testcase")


def _wrap_parent(tmp_path, child_name):
    parent = tmp_path / "parent.dig"
    parent.write_text(
        '<?xml version="1.0" encoding="utf-8"?><circuit><version>2</version>'
        '<attributes/><visualElements>'
        f'<visualElement><elementName>{child_name}</elementName>'
        '<elementAttributes/><pos x="0" y="0"/></visualElement>'
        '</visualElements><wires/></circuit>')
    return parse_dig_file(str(parent))


def test_resolver_auto_validated_manifest_forced_and_disabled(tmp_path):
    (tmp_path / "boolean-unit.dig").write_text(
        _child_xml(*_BOOL_IF, testdata="A B Bool Out\n12 10 0 8\n12 10 2 6\n"))
    top = _wrap_parent(tmp_path, "boolean-unit.dig")
    child = top.subcircuits[0].child_circuit

    auto = M.resolver_for(top, None)
    assert auto(child).name == "boolean_unit"
    assert auto.decided == {"boolean-unit.dig": "boolean_unit"}

    forced = M.resolver_for(top, {"subcircuits": {
        "boolean-unit.dig": {"model": "bidirectional_shifter"}}})
    assert forced(child) is None          # named model disagrees with the test

    off = M.resolver_for(top, {"subcircuits": {"boolean-unit.dig": {"model": "simulate"}}})
    assert off(child) is None and off.decided == {}

    (tmp_path / "boolean-unit.dig").write_text(_child_xml(*_BOOL_IF))   # no testcase
    top2 = _wrap_parent(tmp_path, "boolean-unit.dig")
    child2 = top2.subcircuits[0].child_circuit
    assert M.resolver_for(top2, None)(child2) is None
    vouched = M.resolver_for(top2, {"subcircuits": {
        "boolean-unit.dig": {"model": "boolean_unit", "role": "bitwise ops"}}})
    assert vouched(child2).name == "boolean_unit"
    assert M.role_for(child2, {"subcircuits": {"boolean-unit.dig": {"role": "bitwise ops"}}},
                      "boolean-unit.dig") == "bitwise ops"
    assert M.role_for(child2, None, "boolean-unit.dig").startswith("Boolean unit")


# single-pass replay

def _load(path):
    c = parse_dig_file(path)
    nl = build_netlist(c)
    return c, nl, build_signal_graph(c, nl)


def _clocked_fixture():
    """A repo fixture with a clock column and at least three test rows."""
    for name in ("tier3_realistic/tier3_latched_display.dig",
                 "tier3_realistic/tier3_rom_machine.dig",
                 "tier3_realistic/pipelined_adder_correct.dig"):
        c, nl, g = _load(f"{_BASE}/{name}")
        specs = [s for s in extract_test_specs(c) if len(s.rows) >= 3]
        if specs and any(sim._row_has_clock_edge(c, specs[0].headers, r)
                         for r in specs[0].rows):
            return c, nl, g, specs[0]
    raise AssertionError("no clocked fixture with a testcase")


def test_simulate_rows_matches_per_row_replay_on_clocked_fixture():
    c, nl, g, spec = _clocked_fixture()
    rows = [r.line_index for r in spec.rows if not r.is_malformed]
    assert len(rows) >= 3
    once = simulate_rows(c, nl, g, spec)
    for i in rows:
        one = simulate_sequential(c, nl, g, spec, i)
        assert once[i].net_values == one.net_values
        assert once[i].output_values == one.output_values


def test_row_replay_is_incremental_and_ignores_unknown_rows():
    c, nl, g, spec = _clocked_fixture()
    rows = [r.line_index for r in spec.rows if not r.is_malformed]
    replay = RowReplay(c, nl, g, spec)
    first = replay.upto(rows[1])
    assert set(replay.results) == set(rows[:2])
    later = replay.upto(rows[-1])
    assert set(replay.results) == set(rows)
    assert first is replay.results[rows[1]]
    assert later.output_values == simulate_sequential(c, nl, g, spec, rows[-1]).output_values
    assert replay.upto(10_000).net_values == {}


# substitution end to end, through a manifest entry on a fixture child

def test_manifest_model_replaces_child_and_keeps_top_level_values(monkeypatch):
    c, nl, g = _load(f"{_BASE}/tier3_realistic/tier3_calculator.dig")
    spec = extract_test_specs(c)[0]
    rows = [r.line_index for r in spec.rows if not r.is_malformed]
    gate = simulate_rows(c, nl, g, spec, rows)

    def bool_unit(i, _s):
        a, b = i["A"] & 0xF, i["B"] & 0xF
        return {"Result": (a | b) if i["LogSel"] & 1 else (a & b)}, None

    test_model = M.FormulaModel(
        "test_bool_unit", "AND / OR of two nibbles",
        (("A", 4), ("B", 4), ("LogSel", 1)), (("Result", 4),), bool_unit)
    monkeypatch.setattr(M, "MODELS", M.MODELS + (test_model,))
    monkeypatch.setattr(M, "BY_NAME", {**M.BY_NAME, "test_bool_unit": test_model})

    calls = []
    real = sim._eval_subcircuit

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)
    monkeypatch.setattr(sim, "_eval_subcircuit", counting)

    notes = []
    resolver = M.resolver_for(c, {"subcircuits": {"bool_unit.dig": {"model": "test_bool_unit"}}}, notes)
    assert resolver.decided == {"bool_unit.dig": "test_bool_unit"}
    modeled = simulate_rows(c, nl, g, spec, rows, model_resolver=resolver)
    assert not calls                                   # child never simulated
    for i in rows:
        assert modeled[i].net_values == gate[i].net_values
        assert modeled[i].output_values == gate[i].output_values
