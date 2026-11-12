"""
Check DLC's wiring view of a circuit against Digital itself.
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

X0, Y0 = 400, 400


# ───────────────────────── Digital ─────────────────────────

def _jar() -> str:
    from dlc.testing.runner import find_digital_jar
    jar = find_digital_jar()
    if not jar:
        sys.exit("Digital.jar not found. Set it once in the app (Settings), "
                 "or set the DIGITAL_JAR environment variable.")
    return jar


def _ensure_class(jar: str) -> None:
    src, cls = HERE / "PinDump.java", HERE / "PinDump.class"
    if cls.exists() and cls.stat().st_mtime >= src.stat().st_mtime:
        return
    try:
        subprocess.run(["javac", "--release", "8", "-nowarn", "-cp", jar,
                        "-d", str(HERE), str(src)],
                       check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        if not cls.exists():
            sys.exit("PinDump.class is missing and javac is not available "
                     "to build it from PinDump.java.")


def digital_view(paths: list[str]) -> list[dict]:
    """Digital's own pins and nets for each circuit file, in the order given.

    Java is started inside each folder and handed bare file names, so a
    folder whose path the console code page cannot spell still works."""
    jar = _jar()
    _ensure_class(jar)
    by_dir: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    for i, p in enumerate(paths):
        by_dir[str(Path(p).parent)].append((i, Path(p).name))
    out: list[dict | None] = [None] * len(paths)
    for folder, items in by_dir.items():
        for k in range(0, len(items), 60):
            chunk = items[k:k + 60]
            proc = subprocess.run(
                ["java", "-Djava.awt.headless=true", "-cp",
                 os.pathsep.join([jar, str(HERE)]), "PinDump",
                 *[name for _i, name in chunk]],
                cwd=folder, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=600)
            lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
            if len(lines) != len(chunk):
                sys.exit("Digital did not answer for every file:\n"
                         + (proc.stderr or proc.stdout)[-800:])
            for (i, _name), ln in zip(chunk, lines):
                out[i] = json.loads(ln)
    return out


# ───────────────────────── check ─────────────────────────

def _cls(direction: str) -> str:
    d = str(direction).lower()
    return "in" if d.startswith("in") else ("out" if d.startswith("out") else "both")


def differences(path: str, view: dict) -> list[tuple[str, str]]:
    """(kind, text) for every pin DLC places or connects unlike Digital."""
    from dlc.parser.dig_parser import parse_dig_file
    from dlc.parser.netlist import build_netlist

    if view.get("error"):
        return [("DIGITAL", f"Digital could not load the file: {view['error'][:200]}")]
    circuit = parse_dig_file(path)
    nl = build_netlist(circuit)
    els = view["elements"]
    if len(els) != len(circuit.components):
        return [("COUNT", f"Digital has {len(els)} elements, DLC {len(circuit.components)}")]

    wired = {w.p1.as_tuple() for w in circuit.wires} | {w.p2.as_tuple() for w in circuit.wires}
    wired |= {c.position.as_tuple() for c in circuit.components if c.element_name == "Tunnel"}

    d_net = {}
    for k, net in enumerate(view.get("nets") or []):
        for idx, _name, x, y in net["pins"]:
            d_net[(idx, x, y)] = k
    o_net, o_pins = {}, collections.defaultdict(list)
    for net in nl.nets:
        for p in net.pins:
            o_pins[p.component_index].append((p.pin_name, p.direction, p.x, p.y))
            o_net[(p.component_index, p.x, p.y)] = net.net_id

    out: list[tuple[str, str]] = []
    if view.get("net_error"):
        out.append(("DIGITAL", f"Digital refuses the wiring: {view['net_error'][:200]}"))
    for i, e in enumerate(els):
        name = e["name"]
        if name in ("Testcase", "Text", "Rectangle"):
            continue
        if e.get("pin_error"):
            out.append(("DIGITAL", f"{name}[{i}]: Digital has no shape for it "
                                   f"({e['pin_error'][:120]})"))
            continue
        dpos = {(p["x"], p["y"]): (p["n"], _cls(p["d"])) for p in e["pins"]}
        opos = {(x, y): (n, d) for n, d, x, y in o_pins.get(i, [])}
        where = f"{name}[{i}] at ({e['x']},{e['y']})"
        for (x, y), (n, d) in dpos.items():
            if (i, x, y) in d_net and (x, y) not in opos:
                out.append(("MISSED", f"{where}: pin {n} at ({x},{y}) is wired in "
                                      f"Digital; DLC has its pins at "
                                      f"{sorted((nn, xx, yy) for nn, _, xx, yy in o_pins.get(i, []))[:8]}"))
        for (x, y), (n, d) in opos.items():
            if (x, y) not in dpos and (x, y) in wired:
                out.append(("PHANTOM", f"{where}: DLC attached {n} at ({x},{y}); "
                                       f"Digital's pins are "
                                       f"{sorted((p['n'], p['x'], p['y']) for p in e['pins'])[:8]}"))
            elif (x, y) in dpos and name != "Tunnel":
                dn, dd = dpos[(x, y)]
                od = d if d in ("in", "out") else "both"
                if "both" not in (od, dd) and od != dd:
                    out.append(("DIR", f"{where} at ({x},{y}): DLC {n} is {d}, "
                                       f"Digital {dn} is {dd}"))

    shared = [k for k in d_net if k in o_net]
    d_groups, o_groups = collections.defaultdict(set), collections.defaultdict(set)
    for key in shared:
        d_groups[d_net[key]].add(key)
        o_groups[o_net[key]].add(key)
    d_part = {frozenset(v) for v in d_groups.values()}
    o_part = {frozenset(v) for v in o_groups.values()}
    if d_part != o_part:
        only_d, only_o = d_part - o_part, o_part - d_part
        out.append(("NETS", f"{len(only_d)} net(s) only in Digital, {len(only_o)} only "
                            f"in DLC; e.g. Digital {sorted(next(iter(only_d)))[:3] if only_d else '-'}"
                            f" / DLC {sorted(next(iter(only_o)))[:3] if only_o else '-'}"))
    return out


def check(targets: list[str]) -> int:
    paths: list[str] = []
    for t in targets:
        p = Path(t)
        paths.extend(sorted(str(f.resolve()) for f in p.rglob("*.dig")) if p.is_dir()
                     else [str(p.resolve())])
    paths = [p for p in paths if not Path(p).name.startswith(".dlc_injected__")]
    if not paths:
        sys.exit("No .dig files found.")
    bad = 0
    for path, view in zip(paths, digital_view(paths)):
        try:
            diffs = differences(path, view)
        except Exception as exc:  # noqa: BLE001
            diffs = [("DLC", f"DLC could not read the file: {type(exc).__name__}: {exc}")]
        rel = os.path.relpath(path)
        if not diffs:
            print(f"same as Digital   {rel}")
            continue
        bad += 1
        print(f"DIFFERENT         {rel}")
        for kind, text in diffs[:40]:
            print(f"    {kind:8s} {text}")
        if len(diffs) > 40:
            print(f"    ... and {len(diffs) - 40} more")
    print(f"\n{len(paths)} file(s): {len(paths) - bad} same as Digital, {bad} different.")
    return 1 if bad else 0


# ───────────────────────── record ─────────────────────────

def _attr_xml(key, value) -> str:
    if key == "rotation":
        return f'<entry><string>rotation</string><rotation rotation="{value}"/></entry>'
    if key == "shapeType":
        return f"<entry><string>shapeType</string><shapeType>{value}</shapeType></entry>"
    if key == "inverterConfig":
        inner = "".join(f"<string>{escape(v)}</string>" for v in value)
        return f"<entry><string>inverterConfig</string><inverterConfig>{inner}</inverterConfig></entry>"
    if isinstance(value, bool):
        return f"<entry><string>{escape(key)}</string><boolean>{'true' if value else 'false'}</boolean></entry>"
    if isinstance(value, int):
        return f"<entry><string>{escape(key)}</string><int>{value}</int></entry>"
    return f"<entry><string>{escape(key)}</string><string>{escape(str(value))}</string></entry>"


def _circuit_xml(elements, top=None) -> str:
    ves = "".join(
        f"<visualElement><elementName>{escape(name)}</elementName><elementAttributes>"
        f"{''.join(_attr_xml(k, v) for k, v in attrs.items())}</elementAttributes>"
        f'<pos x="{x}" y="{y}"/></visualElement>' for name, attrs, x, y in elements)
    return ('<?xml version="1.0" encoding="utf-8"?>\n<circuit><version>2</version><attributes>'
            f"{''.join(_attr_xml(k, v) for k, v in (top or {}).items())}</attributes>"
            f"<visualElements>{ves}</visualElements><wires/></circuit>\n")


def _variants():
    """(tag, element, attributes) for every geometry the pin table models."""
    inv_sets = [None, ["In_1"], ["In_2"], ["In_1", "In_2"], ["In_3"], ["In_2", "In_4"]]
    for g, n, wide, inv in itertools.product(
            ("And", "Or", "XOr", "NAnd", "NOr", "XNOr"), range(2, 9), (False, True), inv_sets):
        if inv and any(int(s[3:]) > n for s in inv):
            continue
        a = {}
        if n != 2:
            a["Inputs"] = n
        if wide:
            a["wideShape"] = True
        if inv:
            a["inverterConfig"] = inv
        yield f"{g}_n{n}_w{int(wide)}_inv{'-'.join(s[3:] for s in inv) if inv else '0'}", g, a
    for wide in (False, True):
        yield f"Not_w{int(wide)}", "Not", ({"wideShape": True} if wide else {})
    for el in ("Multiplexer", "Demultiplexer", "Decoder"):
        for sb, flip in itertools.product(range(1, 6), (False, True)):
            a = {}
            if sb != 1:
                a["Selector Bits"] = sb
            if flip:
                a["flipSelPos"] = True
            yield f"{el}_s{sb}_f{int(flip)}", el, a
    for sb in range(1, 5):
        yield f"PriorityEncoder_s{sb}", "PriorityEncoder", ({"Selector Bits": sb} if sb != 1 else {})
    splits_in = ["4", "1,1,1,1", "4,4", "0-3", "8,8,8,8", "1,2,1", "0-0, 1-1, 2-2, 3-3, 4-4, 5-5", "32"]
    splits_out = ["4", "1,1,1,1", "8", "0-4", "31,1", "0-3,4-5,6-6,7-7", "1,1"]
    for si, so, spread, mirror in itertools.product(splits_in, splits_out, (1, 2, 3), (False, True)):
        a = {"Input Splitting": si, "Output Splitting": so}
        if spread != 1:
            a["splitterSpreading"] = spread
        if mirror:
            a["mirror"] = True
        yield (f"Splitter_{si.replace(',', '_').replace(' ', '')}__{so.replace(',', '_')}"
               f"_sp{spread}_m{int(mirror)}"), "Splitter", a
    for el in ("Register", "Comparator", "Add", "BitExtender", "BarrelShifter", "ROM",
               "RegisterFile", "Seven-Seg", "PFET", "NFET", "PullUp", "PullDown", "In",
               "Out", "Tunnel", "Const", "Clock", "Ground", "VDD"):
        yield f"{el}_plain", el, ({"NetName": "t"} if el == "Tunnel" else {})
    yield "Register_bits32", "Register", {"Bits": 32}
    yield "ROM_big", "ROM", {"AddrBits": 8, "Bits": 32}
    yield "Add_bits32", "Add", {"Bits": 32}
    yield "Comparator_signed", "Comparator", {"Bits": 32, "Signed": True}


def _layout_variants():
    """(ports, child attributes, box attributes) for sub-circuit boxes whose
    shape is set to Layout, on the box or in the child. A port is
    [kind, label, x, y, rotation] in FILE order."""
    lay = {"shapeType": "LAYOUT"}

    def lr(n, m):
        return ([["In", f"i{k}", 100, 100 + 40 * k, 0] for k in range(n)]
                + [["Out", f"o{k}", 500, 100 + 40 * k, 0] for k in range(m)])

    for n, m in itertools.product(range(7), range(7)):
        if n + m:
            yield lr(n, m), {}, lay
    yield lr(3, 4), lay, {}
    yield lr(3, 4), lay, {"shapeType": "SIMPLE"}
    yield lr(3, 4), lay, {"shapeType": "DEFAULT"}
    yield lr(3, 4), {}, {"shapeType": "SIMPLE"}
    for width, height in ((1, None), (6, None), (None, 2), (None, 6), (5, 9)):
        top = dict(lay)
        if width:
            top["Width"] = width
        if height:
            top["Height"] = height
        yield lr(3, 4), top, {}
        yield lr(1, 1), top, {}
    # the file lists the outputs bottom-up, a clock pin sits between inputs
    drawn = ([["Out", f"o{k}", 500, 100 + 40 * (3 - k), 0] for k in range(4)]
             + [["In", "a", 100, 100, 0], ["Clock", "ck", 100, 140, 0], ["In", "b", 100, 180, 0]])
    four = [["In", "a", 100, 100, 0], ["In", "b", 100, 200, 0], ["In", "t0", 300, 40, 3],
            ["In", "t1", 200, 40, 3], ["In", "bm", 260, 400, 1], ["In", "r", 500, 300, 2],
            ["Out", "y", 500, 100, 0], ["Out", "ot", 400, 40, 1], ["Out", "ob", 340, 400, 3],
            ["Out", "ol", 100, 300, 2], ["Clock", "ck", 100, 260, 0]]
    wide = ([["In", f"t{k}", 100 + 60 * k, 40, 3] for k in range(5)]
            + [["Out", f"b{k}", 100 + 60 * k, 400, 3] for k in range(2)]
            + [["In", "a", 40, 200, 0], ["Out", "y", 600, 200, 0]])
    for ports in (drawn, four, wide, lr(6, 2), lr(2, 5)):
        for rot in range(4):
            yield ports, {}, ({**lay, "rotation": rot} if rot else lay)


def record(dst: str) -> int:
    with tempfile.TemporaryDirectory(prefix="dlc-oracle-") as tmp:
        index: dict[str, dict] = {}
        layout_rows = list(_layout_variants())
        for k, (ports, top, box) in enumerate(layout_rows):
            Path(tmp, f"lay{k}").mkdir()
            Path(tmp, f"lay{k}", "child.dig").write_text(_circuit_xml(
                [(kind, {"Label": lab, **({"rotation": r} if r else {})}, x, y)
                 for kind, lab, x, y, r in ports], top), encoding="utf-8")
            Path(tmp, f"lay{k}", "top.dig").write_text(
                _circuit_xml([("child.dig", box, X0, Y0)]), encoding="utf-8")
        layout_views = digital_view([str(Path(tmp, f"lay{k}", "top.dig"))
                                     for k in range(len(layout_rows))])
        for tag, name, attrs in _variants():
            for rot in range(4):
                a = dict(attrs)
                if rot:
                    a["rotation"] = rot
                fn = f"{tag}_r{rot}.dig"
                Path(tmp, fn).write_text(_circuit_xml([(name, a, X0, Y0)]), encoding="utf-8")
                index[fn] = {"name": name, "attrs": a}
        for n_in, n_out, width in itertools.product(range(1, 7), range(1, 4), (None, 3, 5, 10)):
            child = f"child_i{n_in}_o{n_out}_w{width or 'd'}.dig"
            els = [("In", {"Label": f"A{k}"}, 100, 100 + 60 * k) for k in range(n_in)]
            els += [("Out", {"Label": f"Y{k}"}, 500, 100 + 60 * k) for k in range(n_out)]
            Path(tmp, child).write_text(
                _circuit_xml(els, {"Width": width} if width else None), encoding="utf-8")
            for rot in range(4):
                fn = f"SUB_i{n_in}_o{n_out}_w{width or 'd'}_r{rot}.dig"
                Path(tmp, fn).write_text(
                    _circuit_xml([(child, {"rotation": rot} if rot else {}, X0, Y0)]),
                    encoding="utf-8")
                index[fn] = {"name": child, "attrs": {}}
        names = sorted(index)
        views = digital_view([str(Path(tmp, fn)) for fn in names])

    bases = sorted({fn.rsplit("_r", 1)[0] for fn in names})
    rotated_too = {b for k, b in enumerate(bases) if k % 6 == 0}
    code = {"input": "i", "output": "o"}
    elements, subs = [], []
    for fn, view in zip(names, views):
        base, rot = fn[:-4].rsplit("_r", 1)
        rot = int(rot)
        if rot and base not in rotated_too:
            continue
        if view.get("error") or view["elements"][0].get("pin_error"):
            sys.exit(f"Digital could not place {fn}")
        e = view["elements"][0]
        pins = sorted([p["x"] - X0, p["y"] - Y0, code.get(p["d"], "b")] for p in e["pins"])
        if base.startswith("SUB_"):
            m = re.match(r"SUB_i(\d+)_o(\d+)_w(\w+)", base)
            subs.append([int(m.group(1)), int(m.group(2)),
                         None if m.group(3) == "d" else int(m.group(3)), rot, pins])
        else:
            elements.append([index[fn]["name"], index[fn]["attrs"], pins])
    layouts = []
    for (ports, top, box), view in zip(layout_rows, layout_views):
        if view.get("error") or view["elements"][0].get("pin_error"):
            sys.exit(f"Digital could not place a Layout box: {top} {box}")
        layouts.append([ports, top, box, sorted(
            [p["x"] - X0, p["y"] - Y0, code.get(p["d"], "b"), p["n"]]
            for p in view["elements"][0]["pins"])])
    old = {}
    if Path(dst).exists():
        old = json.loads(Path(dst).read_text(encoding="utf-8"))
    head = {
        "source": old.get("source") or
        "Digital (hneemann/Digital), VisualElement.getPins() for one element at the origin",
        "recorded": old.get("recorded") if (old.get("elements") == elements
                                            and old.get("subcircuits") == subs
                                            and old.get("layouts") == layouts)
        else __import__("datetime").date.today().isoformat(),
        "note": old.get("note") or "",
    }
    with open(dst, "w", encoding="utf-8") as f:
        f.write("{\n")
        for k in ("source", "recorded", "note"):
            f.write(f" {json.dumps(k)}: {json.dumps(head[k])},\n")
        f.write(' "elements": [\n' + ",\n".join(
            "  " + json.dumps(r, separators=(",", ":")) for r in elements) + "\n ],\n")
        f.write(' "subcircuits": [\n' + ",\n".join(
            "  " + json.dumps(r, separators=(",", ":")) for r in subs) + "\n ],\n")
        f.write(' "layouts": [\n' + ",\n".join(
            "  " + json.dumps(r, separators=(",", ":")) for r in layouts) + "\n ]\n}\n")
    changed = (old.get("elements") != elements or old.get("subcircuits") != subs
               or old.get("layouts") != layouts)
    print(f"{dst}: {len(elements)} element rows, {len(subs)} sub-circuit rows, "
          f"{len(layouts)} Layout-shape rows"
          f" ({'CHANGED' if changed and old else 'unchanged' if old else 'written'}).")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "check":
        return check(argv[1:])
    if len(argv) == 2 and argv[0] == "record":
        return record(argv[1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
