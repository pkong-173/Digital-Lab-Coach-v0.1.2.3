"""
Pin geometry for covered Digital element types (details specified at docs/dev/digital_notes.md).

Digital component positions in the .dig file are anchor points. Each
component's actual pin positions are OFFSETS from that anchor. We use a registry 
mapping element_name to its pin offsets.

Digital uses x increasing rightward, y increasing downward. Offsets here are added to a
component's <pos x=.. y=..> to get the absolute pin location.

For elements whose pin geometry depends on attributes, we provide a function instead
of a static list.

Every offset in this file is the position Digital v0.31 itself reports for
the pin (VisualElement.getPins()), recorded for each element in all four
rotations and across its geometry attributes.
"""

from dataclasses import dataclass

from dlc.parser.models import Component, Position


@dataclass(frozen=True)
class PinSpec:

    name: str
    offset_x: int
    offset_y: int
    direction: str  
    inverted: bool = False

def _not_pins(comp: Component) -> list[PinSpec]:
    """Not: A at (0, 0); Y at (40, 0), or (60, 0) with 'wideShape'."""
    out_x = 60 if comp.attributes.get("wideShape") else 40
    return [
        PinSpec("A", offset_x=0, offset_y=0, direction="in"),
        PinSpec("Y", offset_x=out_x, offset_y=0, direction="out"),
    ]


_INPUT_PINS = [PinSpec("out", 0, 0, "out")]
_OUTPUT_PINS = [PinSpec("in", 0, 0, "in")]
_TUNNEL_PINS = [PinSpec("net", 0, 0, "bidir")]
_CONST_PINS = [PinSpec("out", 0, 0, "out")]
_CLOCK_PINS = [PinSpec("clk", 0, 0, "out")]
_GROUND_PINS = [PinSpec("out", 0, 0, "out")]
_VDD_PINS = [PinSpec("out", 0, 0, "out")]

# Switch-level devices (tier 2.5 transistor labs)
_PFET_PINS = [
    PinSpec("g", offset_x=0,  offset_y=0,  direction="in"),
    PinSpec("s", offset_x=20, offset_y=0,  direction="bidir"),
    PinSpec("d", offset_x=20, offset_y=40, direction="bidir"),
]
_NFET_PINS = [
    PinSpec("g", offset_x=0,  offset_y=40, direction="in"),
    PinSpec("d", offset_x=20, offset_y=0,  direction="bidir"),
    PinSpec("s", offset_x=20, offset_y=40, direction="bidir"),
]
_PULL_PINS = [PinSpec("out", 0, 0, "weak")]

_ADD_PINS = [
    PinSpec("a",   0,  0,  "in"),
    PinSpec("b",   0,  20, "in"),
    PinSpec("c_i", 0,  40, "in"),
    PinSpec("s",   60, 0,  "out"),
    PinSpec("c_o", 60, 20, "out"),
]

_BITEXTENDER_PINS = [
    PinSpec("in",  0,  0, "in"),
    PinSpec("out", 60, 0, "out"),
]

_BARREL_SHIFTER_PINS = [
    PinSpec("in",  0,  0,  "in"),
    PinSpec("sh",  0,  40, "in"),
    PinSpec("out", 60, 20, "out"),
]


_ROM_PINS = [
    PinSpec("A",   0,  0,  "in"),
    PinSpec("sel", 0,  40, "in"),
    PinSpec("D",   60, 20, "out"),
]

_REGISTER_FILE_PINS = [
    PinSpec("Din", offset_x=0,  offset_y=0,   direction="in"),
    PinSpec("we",  offset_x=0,  offset_y=20,  direction="in"),
    PinSpec("Rw",  offset_x=0,  offset_y=40,  direction="in"),
    PinSpec("C",   offset_x=0,  offset_y=60,  direction="in"),
    PinSpec("Ra",  offset_x=0,  offset_y=80,  direction="in"),
    PinSpec("Rb",  offset_x=0,  offset_y=100, direction="in"),
    PinSpec("Da",  offset_x=80, offset_y=0,   direction="out"),
    PinSpec("Db",  offset_x=80, offset_y=20,  direction="out"),
]

_SEVEN_SEG_PINS = [
    PinSpec("a",  offset_x=0,  offset_y=0, direction="in"),
    PinSpec("b",  offset_x=20, offset_y=0, direction="in"),
    PinSpec("c",  offset_x=40, offset_y=0, direction="in"),
    PinSpec("d",  offset_x=60, offset_y=0, direction="in"),
    PinSpec("e",  offset_x=0,  offset_y=140, direction="in"),
    PinSpec("f",  offset_x=20, offset_y=140, direction="in"),
    PinSpec("g",  offset_x=40, offset_y=140, direction="in"),
    PinSpec("dp", offset_x=60, offset_y=140, direction="in"),
]

STATIC_PIN_TABLE: dict[str, list[PinSpec]] = {
    "In":     _INPUT_PINS,
    "Out":    _OUTPUT_PINS,
    "Tunnel": _TUNNEL_PINS,
    "Const":  _CONST_PINS,
    "Clock":  _CLOCK_PINS,
    "Ground": _GROUND_PINS,
    "VDD":    _VDD_PINS,
    "Add":    _ADD_PINS,
    "BitExtender":   _BITEXTENDER_PINS,
    "BarrelShifter": _BARREL_SHIFTER_PINS,
    "ROM":    _ROM_PINS,
    "RegisterFile": _REGISTER_FILE_PINS,
    "Seven-Seg": _SEVEN_SEG_PINS,
    "PFET":     _PFET_PINS,
    "NFET":     _NFET_PINS,
    "PullUp":   _PULL_PINS,
    "PullDown": _PULL_PINS,
}

_NARY_GATE_ELEMENTS = frozenset({"And", "Or", "XOr", "NAnd", "NOr", "XNOr"})
_INVERTED_OUTPUT_GATES = frozenset({"NAnd", "NOr", "XNOr"})

def inverted_input_names(comp: Component) -> list[str]:
    if comp.element_name not in _NARY_GATE_ELEMENTS:
        return []
    names: list[str] = []
    for raw in (comp.attributes.get("inverterConfig") or []):
        if isinstance(raw, str) and raw.startswith("In_"):
            try:
                idx = int(raw[3:]) - 1
            except ValueError:
                continue
            if idx >= 0:
                names.append(f"in{idx}")
    return names

def _nary_gate_pins(comp: Component) -> list[PinSpec]:
    """
    Boolean gate (And/Or/XOr/NAnd/NOr/XNOr) with N inputs.

    Digital attribute 'Inputs' (<int>) controls input count; absent = 2.
    """
    n = int(comp.attributes.get("Inputs", 2))
    wide = bool(comp.attributes.get("wideShape", False))
    inverted_idxs = {int(nm[2:]) for nm in inverted_input_names(comp)}
    half = n // 2
    gap = 20 if n % 2 == 0 else 0
    pins: list[PinSpec] = []
    for i in range(n):
        pins.append(PinSpec(
            f"in{i}",
            offset_x=-20 if i in inverted_idxs else 0,
            offset_y=i * 20 + (gap if i >= half else 0),
            direction="in",
            inverted=(i in inverted_idxs),
        ))
    out_x = (80 if wide else 60) + (20 if comp.element_name in _INVERTED_OUTPUT_GATES else 0)
    pins.append(PinSpec("Y", offset_x=out_x, offset_y=half * 20, direction="out"))
    return pins


def _multiplexer_pins(comp: Component) -> list[PinSpec]:
    """
    Multiplexer. 'Selector Bits' = N -> 2^N data inputs (absent = 1 -> 2-to-1).

      2 inputs:   in0 (0, 0), in1 (0, 40), out (40, 20), sel (20, 40)
      4+ inputs:  in_i (0, i * 20), out (40, n * 10), sel (20, n * 20)

    'flipSelPos' moves sel to the top edge, at (20, 0) for every size.
    """
    sel_bits = int(comp.attributes.get("Selector Bits", 1))
    n_inputs = 2 ** sel_bits
    spacing = 40 if n_inputs == 2 else 20
    sel_y = 0 if comp.attributes.get("flipSelPos") else n_inputs * 20

    pins: list[PinSpec] = []
    for i in range(n_inputs):
        pins.append(PinSpec(f"in{i}", offset_x=0, offset_y=i * spacing, direction="in"))
    pins.append(PinSpec("sel", offset_x=20, offset_y=sel_y, direction="in"))
    pins.append(PinSpec("out", offset_x=40, offset_y=(n_inputs // 2) * 20, direction="out"))
    return pins


def _splitter_pins(comp: Component) -> list[PinSpec]:
    """
    Splitter.
    Splitter / merger. Bit-group sizes determine pin count. Inputs on the
    left edge, outputs on the right edge. Pin spacing = 20 * splitterSpreading.

    splitterSpreading defaults to 1 (20-unit spacing). When set (e.g. =2),
    pins are spaced 40 units apart.

    `mirror` (boolean) flips the shape vertically about the anchor row:
    pin i sits at -i*spacing instead of +i*spacing (SVG-verified on a
    real add-sub "32 -> 31,1" sign extractor: out0 stays on the anchor
    row, out1 lands one row ABOVE). Without this, the unwired 31-bit
    out0 loose-snapped onto the sign wire and produced width_mismatch
    errors on correct student files.
    """
    in_split = str(comp.attributes.get("Input Splitting", "1"))
    out_split = str(comp.attributes.get("Output Splitting", "1"))
    spread = int(comp.attributes.get("splitterSpreading", 1))
    in_groups = [s.strip() for s in in_split.split(",") if s.strip()]
    out_groups = [s.strip() for s in out_split.split(",") if s.strip()]
    spacing = 20 * spread
    if comp.attributes.get("mirror") in (True, "true", "True"):
        spacing = -spacing

    pins: list[PinSpec] = []
    for i, _ in enumerate(in_groups):
        pins.append(PinSpec(f"in{i}", offset_x=0, offset_y=i * spacing, direction="in"))
    for i, _ in enumerate(out_groups):
        pins.append(PinSpec(f"out{i}", offset_x=20, offset_y=i * spacing, direction="out"))
    return pins


def _register_pins(comp: Component) -> list[PinSpec]:
    """
    Register: D input, C clock, en write-enable, Q output.

      D  at offset (0, 0)   (top-left)
      C  at offset (0, 20)  (left, below D)
      en at offset (0, 40)  (left, below C) typically tied to Const(1).
      Q  at offset (60, 20) (right, between D and en heights)
    """
    return [
        PinSpec("D",  offset_x=0,  offset_y=0,  direction="in"),
        PinSpec("C",  offset_x=0,  offset_y=20, direction="in"),
        PinSpec("en", offset_x=0, offset_y=40, direction="in"),
        PinSpec("Q",  offset_x=60, offset_y=20, direction="out"),
    ]

def _decoder_pins(comp: Component) -> list[PinSpec]:
    """
    Decoder: 2^Selector Bits outputs on the right edge at x = 40, one sel
    input on the bottom edge.
    """
    sel_bits = int(comp.attributes.get("Selector Bits", 1))
    n_outputs = 2 ** sel_bits
    spacing = 40 if n_outputs == 2 else 20
    if comp.attributes.get("flipSelPos"):
        sel_y = 0
    else:
        sel_y = n_outputs * 20 if n_outputs <= 2 else (n_outputs - 1) * 20
    pins: list[PinSpec] = [
        PinSpec("sel", offset_x=20, offset_y=sel_y, direction="in"),
    ]
    for i in range(n_outputs):
        pins.append(PinSpec(f"out_{i}", offset_x=40, offset_y=i * spacing, direction="out"))
    return pins


def _demultiplexer_pins(comp: Component) -> list[PinSpec]:
    """
    Demultiplexer: routes one data input to 1 of 2^Selector Bits outputs.
    The mirror image of the Multiplexer:

      2 outputs:   out_0 (40, 0), out_1 (40, 40), in (0, 20), sel (20, 40)
      4+ outputs:  out_i (40, i * 20), in (0, n * 10), sel (20, n * 20)

    'flipSelPos' moves sel to the top edge, at (20, 0).
    """
    sel_bits = int(comp.attributes.get("Selector Bits", 1))
    n_outputs = 2 ** sel_bits
    spacing = 40 if n_outputs == 2 else 20
    sel_y = 0 if comp.attributes.get("flipSelPos") else n_outputs * 20
    pins: list[PinSpec] = [
        PinSpec("in", offset_x=0, offset_y=(n_outputs // 2) * 20, direction="in"),
        PinSpec("sel", offset_x=20, offset_y=sel_y, direction="in"),
    ]
    for i in range(n_outputs):
        pins.append(PinSpec(f"out_{i}", offset_x=40, offset_y=i * spacing,
                            direction="out"))
    return pins


def _priority_encoder_pins(comp: Component) -> list[PinSpec]:
    """
    PriorityEncoder: 2^Selector Bits priority inputs on the left, two
    outputs on the right: `num` (encoded selector) and `f` (1-bit "any
    input set" flag, directly below num). Students wire f as a chip
    select (e.g. PriorityEncoder.f -> ROM.sel).
    Example (Selector Bits=3, 8 inputs):
      in_i at (0, i*20)
      num  at (80, 0)
      f    at (80, 20)
    """
    sel_bits = int(comp.attributes.get("Selector Bits", 1))
    n_inputs = 2 ** sel_bits
    pins: list[PinSpec] = []
    for i in range(n_inputs):
        pins.append(PinSpec(f"in_{i}", offset_x=0, offset_y=i * 20, direction="in"))
    pins.append(PinSpec("num", offset_x=80, offset_y=0, direction="out"))
    pins.append(PinSpec("f",   offset_x=80, offset_y=20, direction="out"))
    return pins

def _comparator_pins(comp: Component) -> list[PinSpec]:
    """
    Comparator: A and B inputs left, greater/equal/less outputs right.

      Visual width = 60, so outputs sit at x=60.
      A  at (0, 0)  in
      B  at (0, 20) in
      gr at (60, 0)  out
      eq at (60, 20) out
      le at (60, 40) out
    """
    return [
        PinSpec("A",  offset_x=0,  offset_y=0,  direction="in"),
        PinSpec("B",  offset_x=0,  offset_y=20, direction="in"),
        PinSpec("gr", offset_x=60, offset_y=0,  direction="out"),
        PinSpec("eq", offset_x=60, offset_y=20, direction="out"),
        PinSpec("le", offset_x=60, offset_y=40, direction="out"),
    ]

DYNAMIC_PIN_TABLE: dict[str, callable] = {
    "Not":  _not_pins,
    "And":  _nary_gate_pins,
    "Or":   _nary_gate_pins,
    "XOr":  _nary_gate_pins,
    "NAnd": _nary_gate_pins,
    "NOr":  _nary_gate_pins,
    "XNOr": _nary_gate_pins,
    "Multiplexer":   _multiplexer_pins,
    "Demultiplexer": _demultiplexer_pins,
    "Splitter":    _splitter_pins,
    "Register":    _register_pins,
    "Comparator":  _comparator_pins,
    "Decoder":         _decoder_pins,
    "PriorityEncoder": _priority_encoder_pins,
}



# Public API

def get_pin_specs(component: Component) -> list[PinSpec]:
    """
    public look up, use static table for static pins, dynamic table for dynamic pins look up
    """
    name = component.element_name

    if name in STATIC_PIN_TABLE:
        return STATIC_PIN_TABLE[name]
    if name in DYNAMIC_PIN_TABLE:
        return DYNAMIC_PIN_TABLE[name](component)

    return []

def _rotate(dx: int, dy: int, rotation: int) -> tuple[int, int]:
    """
    Rotate a (dx, dy) offset by Digital's rotation index (0..3). Digital
    stores rotation as 0/1/2/3 in <rotation rotation="N"/>. Rotation 1
    means 90 degrees counter-clockwise as viewed in screen coordinates
    (y growing down).
    """
    r = rotation % 4
    if r == 0:
        return (dx, dy)
    if r == 1:
        return (dy, -dx)
    if r == 2:
        return (-dx, -dy)
    return (-dy, dx)  

def absolute_pin_positions(component: Component) -> list[tuple[Position, PinSpec]]:
    """
    Return each pin's absolute canvas position plus its spec.Applies the
    rotation attribute (if any) to the offsets before adding the anchor.
    Used for matching wire endpoints to pins.
    """
    pins = get_pin_specs(component)
    rotation = component.attributes.get("rotation", 0)
    if not isinstance(rotation, int):
        rotation = 0
    result = []
    for pin in pins:
        dx, dy = _rotate(pin.offset_x, pin.offset_y, rotation)
        absolute = Position(
            x=component.position.x + dx,
            y=component.position.y + dy,
        )
        result.append((absolute, pin))
    return result