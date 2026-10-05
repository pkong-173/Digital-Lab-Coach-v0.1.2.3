from dataclasses import dataclass, field

from dlc.parser.models import Circuit

@dataclass(frozen=True)
class Token:

    raw: str
    kind: str            # "int" | "clock" | "highZ" | "dontcare" | "loop_expr" | "unknown"
    value: int | None    


@dataclass
class TestRow:
    __test__ = False

    raw: str
    values: list[Token] = field(default_factory=list)
    line_index: int = 0
    is_malformed: bool = False


@dataclass(frozen=True)
class VariableBinding:
    name: str
    role: str                       # "input" | "output" | "clock" | "unbound"
    component_index: int | None
    bit_width: int | None


@dataclass
class TestSpec:
    """One Testcase from a circuit, parsed into rows."""
    __test__ = False

    name: str
    component_index: int
    headers: list[str]
    rows: list[TestRow]
    raw_data_string: str
    has_unexpanded_loops: bool
    preamble: list[str] = field(default_factory=list)
    unexpanded_reason: str | None = None
    # Digital's own objection to the text, when it has one
    parse_error: str | None = None

    def row_count(self) -> int:
        return len(self.rows)

    def well_formed_row_count(self) -> int:
        return sum(1 for r in self.rows if not r.is_malformed)

# Tokenization

def _tokenize(raw: str) -> Token:
    """Parse a single whitespace-stripped cell into a Token. Used for cells
    DLC builds itself (coach rows, official-test cells); a whole Testcase
    goes through parse_data_string, which knows the full language."""
    s = raw.strip()
    if not s:
        return Token(raw=s, kind="unknown", value=None)

    if s in ("c", "C"):
        return Token(raw=s, kind="clock", value=None)
    if s in ("z", "Z"):
        return Token(raw=s, kind="highZ", value=None)
    if s in ("x", "X"):
        return Token(raw=s, kind="dontcare", value=None)

    # Parenthesized:
    if s.startswith("(") and s.endswith(")") and len(s) >= 3:
        inner = s[1:-1].strip()
        try:
            return Token(raw=s, kind="int", value=int(inner))
        except ValueError:
            return Token(raw=s, kind="loop_expr", value=None)

    # Hex
    if len(s) > 2 and s[0] == "0" and s[1] in ("x", "X"):
        try:
            return Token(raw=s, kind="int", value=int(s, 16))
        except ValueError:
            return Token(raw=s, kind="unknown", value=None)

    # Binary
    if len(s) > 2 and s[0] == "0" and s[1] in ("b", "B"):
        try:
            return Token(raw=s, kind="int", value=int(s[2:], 2))
        except ValueError:
            return Token(raw=s, kind="unknown", value=None)

    # Plain decimal
    try:
        return Token(raw=s, kind="int", value=int(s))
    except ValueError:
        return Token(raw=s, kind="unknown", value=None)


# Line-level parsing

def _strip_inline_comment(line: str) -> str:
    """Drop everything from the first `#` to the end of the line."""
    idx = line.find("#")
    if idx < 0:
        return line
    return line[:idx]


@dataclass
class ParsedTestData:
    __test__ = False

    headers: list[str]
    rows: list[TestRow]
    has_unexpanded: bool
    preamble: list[str]
    unexpanded_reason: str | None
    parse_error: str | None


def parse_data_string_full(text: str) -> ParsedTestData:
    from dlc.testing.testlang import expand_test

    ex = expand_test(text or "")
    rows: list[TestRow] = []
    n = len(ex.headers)
    for i, cells in enumerate(ex.rows):
        raw = " ".join(c.raw for c in cells)
        if len(cells) != n:
            rows.append(TestRow(raw=raw, values=[], line_index=i,
                                is_malformed=True))
        else:
            rows.append(TestRow(
                raw=raw, line_index=i, is_malformed=False,
                values=[Token(raw=c.raw, kind=c.kind, value=c.value)
                        for c in cells]))
    return ParsedTestData(
        headers=list(ex.headers), rows=rows,
        has_unexpanded=ex.dynamic is not None,
        preamble=list(ex.preamble),
        unexpanded_reason=ex.dynamic,
        parse_error=ex.error or ex.row_error)


def parse_data_string(text: str) -> tuple[list[str], list[TestRow], bool]:
    p = parse_data_string_full(text)
    return p.headers, p.rows, p.has_unexpanded


# Public API

def extract_test_specs(circuit: Circuit) -> list[TestSpec]:
    """Build a TestSpec for every Testcase element in `circuit`.
    """
    specs: list[TestSpec] = []
    for idx, comp in enumerate(circuit.components):
        if comp.element_name != "Testcase":
            continue
        raw = comp.attributes.get("Testdata", "")
        if not isinstance(raw, str):
            raw = ""
        p = parse_data_string_full(raw)
        name = comp.label or f"Testcase_{idx}"
        specs.append(TestSpec(
            name=name,
            component_index=idx,
            headers=p.headers,
            rows=p.rows,
            raw_data_string=raw,
            has_unexpanded_loops=p.has_unexpanded,
            preamble=p.preamble,
            unexpanded_reason=p.unexpanded_reason,
            parse_error=p.parse_error,
        ))
    return specs


def match_variables_to_io(
    headers: list[str], circuit: Circuit
) -> dict[str, VariableBinding]:
    """Resolve each header column name against the circuit's top-level
    In, Out, and Clock components by Label."""
    by_in: dict[str, tuple[int, object]] = {}
    by_out: dict[str, tuple[int, object]] = {}
    by_clock: dict[str, tuple[int, object]] = {}
    for i, comp in enumerate(circuit.components):
        if comp.label is None:
            continue
        if comp.is_input():
            by_in[comp.label] = (i, comp)
        elif comp.is_output():
            by_out[comp.label] = (i, comp)
        elif comp.element_name == "Clock":
            by_clock[comp.label] = (i, comp)

    out: dict[str, VariableBinding] = {}
    for var in headers:
        if var in by_in:
            i, comp = by_in[var]
            out[var] = VariableBinding(var, "input", i, comp.bit_width())
        elif var in by_out:
            i, comp = by_out[var]
            out[var] = VariableBinding(var, "output", i, comp.bit_width())
        elif var in by_clock:
            i, comp = by_clock[var]
            out[var] = VariableBinding(var, "clock", i, 1)
        else:
            out[var] = VariableBinding(var, "unbound", None, None)
    return out
