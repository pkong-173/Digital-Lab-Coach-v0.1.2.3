"""
Pin positions against Digital itself.
"""

import json
from pathlib import Path

from dlc.analyzer import check_all_l1
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.models import Circuit, Component, Position, Wire
from dlc.parser.netlist import _all_predicted_pins, build_netlist
from dlc.parser.pin_geometry import absolute_pin_positions

TRUTH = json.loads(
    (Path(__file__).parent / "fixtures" / "digital_v0_31_pins.json")
    .read_text(encoding="utf-8"))
X0, Y0 = 400, 400


def _cls(direction: str) -> str:
    return {"in": "i", "out": "o"}.get(direction, "b")


def _same_direction(ours: str, digital: str) -> bool:
    return "b" in (ours, digital) or ours == digital


def _component(name, attrs, x=X0, y=Y0) -> Component:
    return Component(element_name=name, position=Position(x, y),
                     attributes=dict(attrs), label=attrs.get("Label"))


def test_the_record_covers_every_element_with_a_pin_table():
    from dlc.parser.pin_geometry import DYNAMIC_PIN_TABLE, STATIC_PIN_TABLE
    recorded = {row[0] for row in TRUTH["elements"]}
    assert set(STATIC_PIN_TABLE) | set(DYNAMIC_PIN_TABLE) <= recorded
    assert len(TRUTH["elements"]) > 1000 and len(TRUTH["subcircuits"]) > 100


def test_predicted_pins_are_where_digital_puts_them():
    wrong = []
    for name, attrs, pins in TRUTH["elements"]:
        ours = {(p.x - X0, p.y - Y0): _cls(spec.direction)
                for p, spec in absolute_pin_positions(_component(name, attrs))}
        digital = {(x, y): d for x, y, d in pins}
        if set(ours) != set(digital) or not all(
                _same_direction(ours[k], digital[k]) for k in digital):
            wrong.append((name, attrs, sorted(ours.items()), sorted(digital.items())))
    assert not wrong, f"{len(wrong)} variant(s) differ, first: {wrong[0]}"


def _stub_end(pin, pins):
    """Far end of a 60-long wire leaving the pin away from the element."""
    cx = sum(p[0] for p in pins) / len(pins)
    cy = sum(p[1] for p in pins) / len(pins)
    dx, dy = pin[0] - cx, pin[1] - cy
    if abs(dx) >= abs(dy):
        return (pin[0] + (60 if dx > 0 else -60), pin[1])
    return (pin[0], pin[1] + (60 if dy > 0 else -60))


def _unattached(circuit, pins):
    """Digital pins of component 0 that the netlist did not put on their
    own wire."""
    nl = build_netlist(circuit)
    missing = []
    for (x, y), far in pins:
        net = nl.net_at(x, y)
        on_pin = net is not None and any(
            p.component_index == 0 and (p.x, p.y) == (x, y) for p in net.pins)
        if not on_pin or far not in net.coords:
            missing.append((x, y))
    return missing


def test_a_wire_ending_on_each_real_pin_is_attached_to_it():
    wrong = []
    for name, attrs, pins in TRUTH["elements"]:
        if name == "Tunnel":
            continue
        pts = [(x + X0, y + Y0) for x, y, _ in pins]
        wired = [(p, _stub_end(p, pts)) for p in pts]
        circuit = Circuit(
            components=[_component(name, attrs)],
            wires=[Wire(Position(*p), Position(*far)) for p, far in wired],
        )
        missing = _unattached(circuit, wired)
        if missing:
            wrong.append((name, attrs, missing))
    assert not wrong, f"{len(wrong)} variant(s) lose a wire, first: {wrong[0]}"


def _ve(name, x, y, entries=""):
    return (f"<visualElement><elementName>{name}</elementName>"
            f"<elementAttributes>{entries}</elementAttributes>"
            f'<pos x="{x}" y="{y}"/></visualElement>')


def _entry(key, value, tag="string"):
    return f"<entry><string>{key}</string><{tag}>{value}</{tag}></entry>"


def _xml(elements, wires="", top=""):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<circuit><version>2</version>'
            f"<attributes>{top}</attributes><visualElements>{elements}</visualElements>"
            f"<wires>{wires}</wires></circuit>\n")


def _wire(a, b):
    return f'<wire><p1 x="{a[0]}" y="{a[1]}"/><p2 x="{b[0]}" y="{b[1]}"/></wire>'


def test_subcircuit_boxes_follow_digital_in_every_rotation(tmp_path):
    wrong = []
    for k, (n_in, n_out, width, rot, pins) in enumerate(TRUTH["subcircuits"]):
        folder = tmp_path / f"s{k}"
        folder.mkdir()
        child = "".join(_ve("In", 100, 100 + 60 * i, _entry("Label", f"A{i}"))
                        for i in range(n_in))
        child += "".join(_ve("Out", 500, 100 + 60 * i, _entry("Label", f"Y{i}"))
                         for i in range(n_out))
        (folder / "child.dig").write_text(
            _xml(child, top=_entry("Width", width, "int") if width else ""),
            encoding="utf-8")
        entries = (f'<entry><string>rotation</string><rotation rotation="{rot}"/></entry>'
                   if rot else "")
        pts = [(x + X0, y + Y0) for x, y, _ in pins]
        wired = [(p, _stub_end(p, pts)) for p in pts]
        (folder / "top.dig").write_text(
            _xml(_ve("child.dig", X0, Y0, entries),
                 "".join(_wire(p, far) for p, far in wired)),
            encoding="utf-8")
        circuit = parse_dig_file(str(folder / "top.dig"))
        ours = {(x - X0, y - Y0) for _i, (x, y), _s in _all_predicted_pins(circuit)}
        if ours != {(x, y) for x, y, _ in pins} or _unattached(circuit, wired):
            wrong.append((n_in, n_out, width, rot, sorted(ours), pins))
    assert not wrong, f"{len(wrong)} instance(s) differ, first: {wrong[0]}"


def _rotated(rot):
    return f'<entry><string>rotation</string><rotation rotation="{rot}"/></entry>'


def test_rotated_two_input_gate_fed_by_a_repeated_bit_is_clean(tmp_path):
    """The layout that was flagged: a 5-bit AND turned 90 degrees, one input
    from a 5-bit bus, the other from a splitter that repeats one bit five
    times. Its second input sits two grid steps beside the first."""
    bits5 = _entry("Bits", 5, "int")
    elements = (
        _ve("In", 800, 760, _entry("Label", "Bus") + bits5 + _rotated(1))
        + _ve("In", 840, 820, _entry("Label", "Bit") + _rotated(1))
        + _ve("And", 800, 740, _rotated(1) + bits5)
        + _ve("Splitter", 840, 780, _rotated(1)
              + _entry("Input Splitting", "1,1,1,1,1")
              + _entry("Output Splitting", "5"))
        + _ve("Out", 820, 660, _entry("Label", "Masked") + bits5 + _rotated(1))
    )
    wires = (
        _wire((800, 740), (800, 760))                    # bus -> first input
        + _wire((840, 740), (840, 760))                  # splitter out -> second input
        + _wire((820, 680), (820, 660))                  # gate output
        + "".join(_wire((840 + 20 * i, 780), (840 + 20 * i, 800)) for i in range(5))
        + "".join(_wire((840 + 20 * i, 800), (860 + 20 * i, 800)) for i in range(4))
        + _wire((840, 800), (840, 820))
    )
    path = tmp_path / "masked_bus.dig"
    path.write_text(_xml(elements, wires), encoding="utf-8")
    circuit = parse_dig_file(str(path))

    gate = next(i for i, c in enumerate(circuit.components) if c.element_name == "And")
    nl = build_netlist(circuit)
    at = {p.pin_name: (p.x, p.y) for net in nl.nets for p in net.pins
          if p.component_index == gate}
    assert at == {"in0": (800, 740), "in1": (840, 740), "Y": (820, 680)}
    assert check_all_l1(circuit).issues == []


def test_gates_with_an_even_number_of_inputs_are_clean_in_every_rotation(tmp_path):
    from dlc.parser.pin_geometry import _rotate
    for gate in ("And", "Or", "XOr", "NAnd", "NOr", "XNOr"):
        for n in (2, 4, 6):
            for rot in range(4):
                attrs = {"Inputs": n, "rotation": rot}
                truth = next(p for name, a, p in TRUTH["elements"]
                             if name == gate and a == ({} if n == 2 else {"Inputs": n}))
                pts = [tuple(a + b for a, b in zip(_rotate(x, y, rot), (X0, Y0)))
                       for x, y, _ in truth]
                wired = [(p, _stub_end(p, pts)) for p in pts]
                circuit = Circuit(
                    components=[_component(gate, attrs)],
                    wires=[Wire(Position(*p), Position(*far)) for p, far in wired],
                )
                assert _unattached(circuit, wired) == [], (gate, n, rot)


def test_a_tunnel_on_a_pin_is_not_shared_with_the_unused_output_beside_it():
    """A decoder turned 180 degrees with a tunnel straight on sel: the unused
    last output is one grid step from that tunnel and must stay unconnected."""
    circuit = parse_dig_file(
        "data/sample_circuits/l1_regressions/flipped_decoder_selfeed.dig")
    nl = build_netlist(circuit)
    on_tunnel = [(p.element_name, p.pin_name) for p in nl.net_at(380, 80).pins]
    assert ("Decoder", "sel") in on_tunnel
    assert not [p for p in on_tunnel if p[0] == "Decoder" and p[1] != "sel"]
