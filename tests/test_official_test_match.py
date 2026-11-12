from pathlib import Path

from fastapi.testclient import TestClient

from dlc.analyzer.official_test_match import KIND, check_official_test_match
from dlc.l3 import official_store
from dlc.parser.dig_parser import parse_dig_file
from dlc.web.server import app

NAME = "lab_with_official.dig"


def _xml_circuit(elements: str, wires: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<circuit>\n'
        "  <version>2</version>\n  <attributes/>\n"
        f"  <visualElements>\n{elements}  </visualElements>\n"
        f"  <wires>\n{wires}  </wires>\n</circuit>\n"
    )


def _ve(name, x, y, entries="") -> str:
    attrs = f"<elementAttributes>{entries}</elementAttributes>" if entries \
        else "<elementAttributes/>"
    return (f"    <visualElement><elementName>{name}</elementName>"
            f"{attrs}<pos x=\"{x}\" y=\"{y}\"/></visualElement>\n")


def _entry(k, v, tag="string") -> str:
    return f"<entry><string>{k}</string><{tag}>{v}</{tag}></entry>"


def _testcase(data: str) -> str:
    return _ve("Testcase", 0, 200,
               _entry("Label", "t") + _entry(
                   "Testdata",
                   f"<dataString>{data}</dataString>", tag="testData"))


def _wire(x1, y1, x2, y2) -> str:
    return (f"    <wire><p1 x=\"{x1}\" y=\"{y1}\"/>"
            f"<p2 x=\"{x2}\" y=\"{y2}\"/></wire>\n")


_IO = (_ve("In", 0, 0, _entry("Label", "a"))
       + _ve("Out", 100, 0, _entry("Label", "f")))
_OFFICIAL_ROWS = "a f\n0 0\n1 1"
_STUDENT_ROWS = "a f\n0 0"


def _official(monkeypatch, *, registered=True, matching_raw=None):
    """Pin the official store so the test does not depend on shipped data."""
    monkeypatch.setattr(official_store, "get_content",
                        lambda fn: (_OFFICIAL_ROWS if registered else None))
    monkeypatch.setattr(
        official_store, "status_for",
        lambda fn, raw: ("official"
                         if matching_raw is not None
                         and raw.strip() == matching_raw.strip()
                         else "modified"))


def _cards(tmp_path: Path, xml: str, name: str = NAME):
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return check_official_test_match(parse_dig_file(str(p)), name)


def test_edited_testcase_earns_one_teal_warning(tmp_path, monkeypatch):
    _official(monkeypatch, matching_raw=_OFFICIAL_ROWS)
    cards = _cards(tmp_path, _xml_circuit(
        _IO + _testcase(_STUDENT_ROWS), _wire(0, 0, 100, 0)))
    assert len(cards) == 1
    card = cards[0]
    assert card.kind == KIND
    assert card.severity.value == "warning"
    assert "not the official" in card.title
    assert card.message == ""
    assert "Official tests" in card.suggested_fix
    assert "rows that run instead" in card.suggested_fix
    assert "currently only cpu.dig" in card.suggested_fix
    assert "Coverage coach (Mode B)" in card.suggested_fix
    assert card.component_indices == []


def test_the_cards_never_mention_the_grade(tmp_path, monkeypatch):
    _official(monkeypatch, matching_raw=_OFFICIAL_ROWS)
    edited = _cards(tmp_path, _xml_circuit(
        _IO + _testcase(_STUDENT_ROWS), _wire(0, 0, 100, 0)))[0]
    empty = _cards(tmp_path, _xml_circuit(_IO, _wire(0, 0, 100, 0)))[0]
    for card in (edited, empty):
        text = f"{card.title} {card.message} {card.suggested_fix}".lower()
        assert "grade" not in text
    assert empty.message.endswith("Test runs use the official test.")


def test_the_fix_names_no_file_when_none_is_editable(tmp_path, monkeypatch):
    from dlc.l3 import official_store
    _official(monkeypatch, matching_raw=_OFFICIAL_ROWS)
    monkeypatch.setattr(official_store, "editable_defaults", lambda: [])
    card = _cards(tmp_path, _xml_circuit(
        _IO + _testcase(_STUDENT_ROWS), _wire(0, 0, 100, 0)))[0]
    assert card.suggested_fix.startswith(
        "Open Settings → Official tests to see the rows that run instead. Or open")


def test_file_without_rows_says_so(tmp_path, monkeypatch):
    _official(monkeypatch, matching_raw=_OFFICIAL_ROWS)
    cards = _cards(tmp_path, _xml_circuit(_IO, _wire(0, 0, 100, 0)))
    assert len(cards) == 1
    assert cards[0].kind == KIND
    assert cards[0].severity.value == "warning"
    assert "no test rows" in cards[0].title
    assert "will run" in cards[0].suggested_fix


def test_matching_testcase_stays_silent(tmp_path, monkeypatch):
    _official(monkeypatch, matching_raw=_STUDENT_ROWS)
    assert _cards(tmp_path, _xml_circuit(
        _IO + _testcase(_STUDENT_ROWS), _wire(0, 0, 100, 0))) == []


def test_no_official_test_registered_means_no_card(tmp_path, monkeypatch):
    _official(monkeypatch, registered=False)
    assert _cards(tmp_path, _xml_circuit(
        _IO + _testcase(_STUDENT_ROWS), _wire(0, 0, 100, 0))) == []


def test_upload_endpoint_ships_the_teal_card(tmp_path, monkeypatch):
    _official(monkeypatch, matching_raw=_OFFICIAL_ROWS)
    xml = _xml_circuit(_IO + _testcase(_STUDENT_ROWS), _wire(0, 0, 100, 0))
    client = TestClient(app)
    resp = client.post("/api/circuit", files=[
        ("files", (NAME, xml.encode("utf-8"), "application/octet-stream")),
    ])
    assert resp.status_code == 200
    issues = resp.json()["files"][0]["issues"]
    teal = [i for i in issues if i["kind"] == KIND]
    assert len(teal) == 1
    assert teal[0]["severity"] == "warning"
    # and it must not have turned anything into a blocking error
    assert not [i for i in issues if i["severity"] == "error"]
