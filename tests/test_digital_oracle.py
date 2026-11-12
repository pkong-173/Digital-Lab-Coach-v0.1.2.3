"""
Delete the test when necessary.
"""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "digital_oracle", Path("scripts/digital_oracle/oracle.py"))
oracle = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(oracle)

_AND = "data/sample_circuits/tier1_minimal/single_and.dig"


def _view_from_dlc(path):
    """A Digital-shaped answer built from DLC's own netlist: by construction
    the two agree, so any difference comes from an edit made in the test."""
    from dlc.parser.dig_parser import parse_dig_file
    from dlc.parser.netlist import build_netlist
    circuit = parse_dig_file(path)
    nl = build_netlist(circuit)
    elements = [{"i": i, "name": c.element_name, "x": c.position.x,
                 "y": c.position.y, "rot": 0, "pins": []}
                for i, c in enumerate(circuit.components)]
    nets = []
    for net in nl.nets:
        pins = []
        for p in net.pins:
            d = {"in": "input", "out": "output"}.get(p.direction, "both")
            elements[p.component_index]["pins"].append(
                {"n": p.pin_name, "d": d, "x": p.x, "y": p.y})
            pins.append([p.component_index, p.pin_name, p.x, p.y])
        nets.append({"points": [], "pins": pins, "labels": []})
    return circuit, {"file": path, "elements": elements, "nets": nets}


def test_identical_views_report_nothing():
    _circuit, view = _view_from_dlc(_AND)
    assert oracle.differences(_AND, view) == []


def test_a_pin_digital_has_elsewhere_is_reported_as_missed_and_phantom():
    circuit, view = _view_from_dlc(_AND)
    gate = next(i for i, c in enumerate(circuit.components) if c.element_name == "And")
    pin = next(p for p in view["elements"][gate]["pins"] if p["d"] == "input")
    old = (pin["x"], pin["y"])
    pin["y"] += 20                                   # Digital says: one row lower
    for net in view["nets"]:
        for entry in net["pins"]:
            if entry[0] == gate and (entry[2], entry[3]) == old:
                entry[3] += 20
    kinds = [k for k, _text in oracle.differences(_AND, view)]
    assert "MISSED" in kinds and "PHANTOM" in kinds


def test_a_file_digital_cannot_load_is_reported_not_crashed_on():
    assert oracle.differences(_AND, {"file": _AND, "error": "boom"})[0][0] == "DIGITAL"


def test_every_supported_element_has_recorded_variants():
    recorded = {name for _tag, name, _attrs in oracle._variants()}
    from dlc.parser.pin_geometry import DYNAMIC_PIN_TABLE, STATIC_PIN_TABLE
    assert set(STATIC_PIN_TABLE) | set(DYNAMIC_PIN_TABLE) <= recorded


def test_sample_circuits_are_wired_the_same_in_digital_and_dlc():
    from dlc.testing.runner import find_digital_jar
    if find_digital_jar() is None:
        pytest.skip("Digital.jar not configured")
    files = sorted(str(p) for p in Path("data/sample_circuits/tier3_realistic").glob("*.dig"))
    files += sorted(str(p) for p in Path("data/sample_circuits/l1_regressions").glob("*.dig"))
    for path, view in zip(files, oracle.digital_view([str(Path(f).resolve()) for f in files])):
        assert oracle.differences(path, view) == [], path
