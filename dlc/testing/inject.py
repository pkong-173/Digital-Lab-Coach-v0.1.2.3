"""
Gradescope-style official-testcase injection for test runs.
"""

from __future__ import annotations

import os
import threading
import xml.etree.ElementTree as ET

_TEMP_LOCK = threading.Lock()
_TEMP_USERS: dict[str, int] = {}


def _official_testcase(filename: str) -> str | None:
    from dlc.l3 import official_store
    return official_store.get_content(filename)


def file_test_status(circuit, filename: str) -> str | None:
    from dlc.l3 import official_store
    from dlc.testing.spec import extract_test_specs

    if official_store.get_content(filename) is None:
        return None
    for comp in circuit.components:
        if comp.element_name != "Testcase":
            continue
        raw = comp.attributes.get("Testdata", "")
        if not isinstance(raw, str) or not raw.strip():
            continue
        if official_store.status_for(filename, raw) == "official":
            return "official"
    specs = [s for s in extract_test_specs(circuit) if s.rows]
    return "modified" if specs else "missing"

INJECTED_TEST_LABEL = "official"


def _whose_rows(filename: str) -> str:
    from dlc.l3 import official_store
    if official_store.source_of(_plain_name(filename)) == "default":
        return "Gradescope grades with the official tests"
    return "your own version from Settings → Official tests"


def _replace_testcases(root: ET.Element, content: str) -> bool:
    ves = root.find("visualElements")
    if ves is None:
        return False
    pos_x, pos_y = 0, 0
    for ve in list(ves.findall("visualElement")):
        if ve.findtext("elementName") == "Testcase":
            pos = ve.find("pos")
            if pos is not None:
                pos_x = pos.get("x", "0")
                pos_y = pos.get("y", "0")
            ves.remove(ve)
    target = ET.SubElement(ves, "visualElement")
    ET.SubElement(target, "elementName").text = "Testcase"
    attrs = ET.SubElement(target, "elementAttributes")
    lbl = ET.SubElement(attrs, "entry")
    ET.SubElement(lbl, "string").text = "Label"
    ET.SubElement(lbl, "string").text = INJECTED_TEST_LABEL
    entry = ET.SubElement(attrs, "entry")
    ET.SubElement(entry, "string").text = "Testdata"
    td = ET.SubElement(entry, "testData")
    ET.SubElement(td, "dataString").text = content
    pos = ET.SubElement(target, "pos")
    pos.set("x", str(pos_x))
    pos.set("y", str(pos_y))
    return True


def _data_words(raw, fmt: str = "hex") -> list[int]:
    base = {"hex": 16, "bin": 2, "oct": 8, "dec": 10, "def": 10}.get(
        str(fmt or "hex").lower(), 16)

    def one(tok: str) -> int:
        try:
            return int(tok, base)
        except ValueError:
            try:
                return int(tok, 16)
            except ValueError:
                return 0

    words: list[int] = []
    for tok in str(raw or "").replace(",", " ").split():
        count = 1
        if "*" in tok:
            head, _, tok = tok.partition("*")
            try:
                count = max(int(head, 10), 1)
            except ValueError:
                count = 1
        words.extend([one(tok)] * count)
    while words and words[-1] == 0:
        words.pop()
    return words


def _checked_roms(circuit) -> list[int]:
    roms = [i for i, c in enumerate(circuit.components)
            if c.element_name == "ROM"]
    flagged = [i for i in roms
               if circuit.components[i].attributes.get("isProgramMemory")]
    return flagged or roms


ROM_GATE_MESSAGES = {
    "missing": (
        "{file} is registered with official ROM contents for this lab, "
        "but the file has no ROM. Add the ROM, enter the official "
        "contents, run the tests again, then come back."),
    "empty": (
        "ROM '{rom}' in {file} is empty. Enter the official contents "
        "registered for this lab (double-click the ROM in Digital), run "
        "the tests again, then come back."),
    "mismatch": (
        "ROM '{rom}' in {file} does not hold the official contents "
        "registered for this lab: {differing} of {expected} words differ, "
        "first at address {address} (your word there is {word}). Fix the "
        "ROM before debugging — with the wrong contents every row can "
        "fail for the wrong reason."),
    "own_words": (
        "ROM '{rom}' in {file} holds the official program, but what follows "
        "it does not match the instruction words registered for the rows "
        "you added ({found} in the ROM, {registered} registered; first "
        "difference at address {address}). Make them agree: correct the "
        "ROM in Digital, or the words in Settings → Official tests."),
    "unregistered": (
        "ROM '{rom}' in {file} holds the official program, then more words "
        "that are not registered (count: {found}). To test them, add their "
        "rows and these words in Settings → Official tests. Otherwise "
        "remove them from the ROM."),
}


def _plain_name(filename) -> str:
    base = os.path.basename(str(filename or ""))
    if base.startswith(".dlc_injected__"):
        base = base[len(".dlc_injected__"):]
    return base


def _check_one(circuit, filename: str, base: list[int],
               extra: list[int] | None = None, *,
               can_register: bool = False,
               allow_tail: bool = False) -> dict | None:
    want = list(base) + list(extra or [])
    while want and want[-1] == 0:
        want.pop()
    roms = _checked_roms(circuit)
    if not roms:
        return {"status": "missing", "file": filename,
                "words_expected": len(want),
                "message": ROM_GATE_MESSAGES["missing"].format(file=filename)}
    for idx in roms:
        comp = circuit.components[idx]
        name = comp.label or f"ROM[{idx}]"
        got = _data_words(comp.attributes.get("Data", ""),
                          comp.attributes.get("intFormat", "hex"))
        info = {"file": filename, "rom": name, "component_index": idx,
                "words_expected": len(want), "words_found": len(got)}
        if not got:
            return {"status": "empty", **info,
                    "message": ROM_GATE_MESSAGES["empty"].format(
                        file=filename, rom=name)}
        if allow_tail and got[:len(want)] == want:
            continue
        if got != want:
            span = max(len(got), len(want))
            bad = [a for a in range(span)
                   if (got[a] if a < len(got) else 0)
                   != (want[a] if a < len(want) else 0)]
            first = bad[0]
            word = got[first] if first < len(got) else 0
            verdict = {"status": "mismatch", **info,
                       "differing": len(bad), "first_bad_address": first}
            official_intact = got[:len(base)] == list(base)
            if official_intact and extra:
                return {**verdict, "reason": "own_words",
                        "message": ROM_GATE_MESSAGES["own_words"].format(
                            file=filename, rom=name, address=first,
                            found=len(got) - len(base),
                            registered=len(extra))}
            if official_intact and can_register:
                return {**verdict, "reason": "unregistered",
                        "message": ROM_GATE_MESSAGES["unregistered"].format(
                            file=filename, rom=name,
                            found=len(got) - len(base))}
            return {**verdict, "reason": "official",
                    "message": ROM_GATE_MESSAGES["mismatch"].format(
                        file=filename, rom=name, differing=len(bad),
                        expected=len(want), address=first,
                        word=f"{word:x}")}
    return None


def appended_program_words(path: str,
                           filename: str) -> tuple[list[str], bool] | None:
    from dlc.l3 import official_store
    from dlc.parser.dig_parser import parse_dig_file

    base = official_store.builtin_program(_plain_name(filename))
    if base is None:
        return None
    circuit = parse_dig_file(path)
    roms = _checked_roms(circuit)
    if len(roms) != 1:
        return None
    comp = circuit.components[roms[0]]
    got = _data_words(comp.attributes.get("Data", ""),
                      comp.attributes.get("intFormat", "hex"))
    if got[:len(base)] != base:
        return None
    return ([f"0x{w:08x}" for w in got[len(base):]],
            bool(comp.attributes.get("isProgramMemory")))


def check_rom_contents(path: str, filename: str, *,
                       coach_temp: bool = False) -> dict | None:
    """coach_temp: `path` is the coach's temp copy of `filename`, whose ROM
    may carry the words the coach appended after the registered ones."""
    try:
        from dlc.l3 import official_store
        from dlc.parser.dig_parser import parse_dig_file

        editable = set(official_store.editable_defaults())
        queue = [(parse_dig_file(path), _plain_name(filename), coach_temp)]
        seen: set[str] = set()
        while queue:
            circuit, name, tail_ok = queue.pop(0)
            if name in seen:
                continue
            seen.add(name)
            parts = official_store.program_words(name)
            if parts:
                verdict = _check_one(circuit, name, parts[0], parts[1],
                                     can_register=name in editable,
                                     allow_tail=tail_ok)
                if verdict is not None:
                    return verdict
            for sub in circuit.subcircuits:
                ref = getattr(sub, "reference", None)
                child = getattr(sub, "child_circuit", None)
                if ref and child is not None:
                    queue.append((child, _plain_name(ref), False))
        return None
    except Exception:
        return None


def prepare_injected_run(path: str, filename: str) -> tuple[str | None, list[str]]:
    try:
        from dlc.parser.dig_parser import parse_dig_file

        circuit = parse_dig_file(path)
        status = file_test_status(circuit, filename)
        tree = ET.parse(path)
        root = tree.getroot()
        changed = False
        notes: list[str] = []

        if status in ("missing", "modified"):
            official = _official_testcase(filename)
            if official and _replace_testcases(root, official):
                changed = True
                if status == "missing":
                    notes.append(
                        "official testcase injected (this file has no "
                        f"test rows — {_whose_rows(filename)})")
                else:
                    notes.append(
                        "official testcase injected in place of this "
                        f"file's modified testcase ({_whose_rows(filename)})")

        if not changed:
            return None, []
        d, base = os.path.split(path)
        temp_path = os.path.join(d, f".dlc_injected__{base}")
        with _TEMP_LOCK:
            if not _TEMP_USERS.get(temp_path):
                tree.write(temp_path, encoding="utf-8", xml_declaration=True)
            _TEMP_USERS[temp_path] = _TEMP_USERS.get(temp_path, 0) + 1
        return temp_path, notes
    except Exception:
        return None, []


def inject_official_tests_in_place(path: str, filename: str) -> list[str]:
    try:
        from dlc.parser.dig_parser import parse_dig_file
        circuit = parse_dig_file(path)
        status = file_test_status(circuit, filename)
        if status not in ("missing", "modified"):
            return []
        official = _official_testcase(filename)
        if not official:
            return []
        tree = ET.parse(path)
        if not _replace_testcases(tree.getroot(), official):
            return []
        tree.write(path, encoding="utf-8", xml_declaration=True)
        if status == "missing":
            return ["official testcase injected (this file has no test "
                    f"rows — {_whose_rows(filename)})"]
        return ["official testcase injected in place of the modified "
                f"testcase ({_whose_rows(filename)})"]
    except Exception:
        return []


def cleanup_injected(temp_path: str | None) -> None:
    if not temp_path:
        return
    with _TEMP_LOCK:
        left = _TEMP_USERS.get(temp_path, 0) - 1
        if left > 0:
            _TEMP_USERS[temp_path] = left
            return
        _TEMP_USERS.pop(temp_path, None)
        try:
            os.unlink(temp_path)
        except OSError:
            pass
