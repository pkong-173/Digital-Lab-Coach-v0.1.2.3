"""
Digital's test-case language, expanded into plain rows.

DLC shows, runs and reasons about a Testcase row by row, so the text in a
Testcase element has to become exactly the rows Digital itself executes.
This module re-implements the grammar of Digital's
``de.neemann.digital.testing.parser`` (Parser, Tokenizer, the LineEmitters,
ValueAppenderBits and the three built-in functions) as of October 2026:

    header     : NAME NAME ... EOL          names are any non-blank text
    rows       : (row | statement | block)*
    row        : value+ EOL
    value      : NUMBER | C | X | Z | (expr) | bits(count, expr)
    statement  : let NAME = expr ;          a variable for the rows below
               | init NAME = [-]NUMBER ;    model: initial signal value
               | memory NAME(NUMBER) = NUMBER ;
               | program(NUMBER, ...)       model: program memory
               | declare NAME = expr ;      model: virtual signal
               | resetRandom ;
    block      : repeat(count) row          variable n = 0 .. count-1
               | loop(NAME, count) rows end loop
               | while(expr) rows end while
    expr       : | ^ & = != < <= > >= << >> + - * / %   ~ ! unary -,
                 parentheses, signExt(bits, v), random(max), ite(c, a, b),
                 loop and let variables
    NUMBER     : decimal, 0x hex, 0b binary, a leading 0 means octal,
                 1.5:4 fixed point, 0.5 float bits, 0.5d double bits
    #          : comment to the end of the line
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

MAX_LOOPS = 1 << 24          # Digital's own limit per loop
MAX_ROWS = 1_000_000         # DLC's safety net against a runaway while
_MASK64 = (1 << 64) - 1


class DigitalTestError(ValueError):
    """Digital would refuse this test text; `line` is 1-based."""

    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


class _Dynamic(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Cell:
    raw: str
    kind: str                  # "int" | "clock" | "highZ" | "dontcare"
    value: int | None


@dataclass
class ExpandedTest:
    headers: list[str] = field(default_factory=list)
    rows: list[list[Cell]] = field(default_factory=list)
    row_lines: list[int] = field(default_factory=list)
    preamble: list[str] = field(default_factory=list)
    dynamic: str | None = None       # why the rows could not be pre-computed
    error: str | None = None         # Digital refuses the whole test
    error_line: int | None = None
    row_error: str | None = None     # first row whose cell count is wrong

def _long(v: int) -> int:
    """Wrap to Java's signed 64-bit long."""
    v &= _MASK64
    return v - (1 << 64) if v >= (1 << 63) else v


def decode_number(text: str, parse_floats: bool = True) -> int:
    """Digital's Bits.decode: the value of a number literal."""
    s = text.strip()
    if not s:
        return 0
    if ":" in s:
        p = s.index(":")
        try:
            frac = abs(int(s[p + 1:]))
            floating = float(s[:p])
        except ValueError:
            raise DigitalTestError(f"Invalid number format {s!r}")
        return _long(math.floor(floating * (1 << frac) + 0.5))
    if parse_floats and "." in s:
        try:
            if s[-1] in "dD":
                return struct.unpack(">q", struct.pack(">d", float(s[:-1])))[0]
            return struct.unpack(">i", struct.pack(">f", float(s)))[0]
        except (ValueError, OverflowError, struct.error):
            raise DigitalTestError(f"Invalid number format {s!r}")
    p = 0
    neg = False
    if s[0] == "-":
        neg = True
        p = 1
    if p >= len(s):
        raise DigitalTestError(f"Invalid number format {s!r}")
    was_zero = False
    while p < len(s) and s[p] == "0":
        was_zero = True
        p += 1
    if p >= len(s):
        return 0
    if was_zero:
        if neg:
            raise DigitalTestError(f"Invalid number format {s!r}")
        if s[p] in "xX":
            radix, p = 16, p + 1
        elif s[p] in "bB":
            radix, p = 2, p + 1
        else:
            radix = 8
        if p >= len(s):
            raise DigitalTestError(f"Invalid number format {s!r}")
    else:
        radix = 10
    val = 0
    for ch in s[p:]:
        d = int(ch, 36) if ch.isalnum() and ch.isascii() else -1
        if d < 0 or d >= radix:
            raise DigitalTestError(f"Invalid number format {s!r}")
        val = val * radix + d
    val = _long(val)
    return _long(-val) if neg else val

_KEYWORDS = {
    "end": "END", "loop": "LOOP", "repeat": "REPEAT", "bits": "BITS",
    "let": "LET", "resetRandom": "RESETRANDOM", "while": "WHILE",
    "declare": "DECLARE", "program": "PROGRAM", "init": "INIT",
    "memory": "MEMORY",
}
_SINGLE = {"(": "OPEN", ")": "CLOSE", ";": "SEMICOLON", "&": "AND",
           "|": "OR", "^": "XOR", "+": "ADD", "-": "SUB", "*": "MUL",
           "%": "MOD", "/": "DIV", "~": "BIN_NOT", ",": "COMMA",
           "=": "EQUAL"}


@dataclass(frozen=True)
class _Tok:
    kind: str
    text: str
    line: int
    start: int
    end: int


def _strip_comments(text: str) -> str:
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


class _Scanner:
    def __init__(self, text: str, pos: int, line: int):
        self.s = text
        self.i = pos
        self.line = line
        self._peeked: _Tok | None = None

    def peek(self) -> _Tok:
        if self._peeked is None:
            self._peeked = self._scan()
        return self._peeked

    def next(self) -> _Tok:
        t = self.peek()
        self._peeked = None
        return t

    def _scan(self) -> _Tok:
        s = self.s
        while self.i < len(s) and s[self.i] in " \t":
            self.i += 1
        start = self.i
        if self.i >= len(s):
            return _Tok("EOF", "", self.line, start, start)
        c = s[self.i]
        if c == "\n":
            self.i += 1
            tok = _Tok("EOL", "\n", self.line, start, self.i)
            self.line += 1
            return tok
        two = s[self.i:self.i + 2]
        if two in ("<<", "<=", ">>", ">=", "!="):
            self.i += 2
            kind = {"<<": "SHL", "<=": "LE", ">>": "SHR", ">=": "GE",
                    "!=": "NE"}[two]
            return _Tok(kind, two, self.line, start, self.i)
        if c in "<>!":
            self.i += 1
            kind = {"<": "LT", ">": "GT", "!": "LOG_NOT"}[c]
            return _Tok(kind, c, self.line, start, self.i)
        if c in _SINGLE:
            self.i += 1
            return _Tok(_SINGLE[c], c, self.line, start, self.i)
        if c.isascii() and (c.isalpha() or c == "_"):
            while self.i < len(s) and s[self.i].isascii() and (
                    s[self.i].isalnum() or s[self.i] == "_"):
                self.i += 1
            word = s[start:self.i]
            return _Tok(_KEYWORDS.get(word, "IDENT"), word, self.line, start, self.i)
        if c.isdigit():
            while self.i < len(s) and (s[self.i].isdigit()
                                       or s[self.i] in "abcdefABCDEFxX:."):
                self.i += 1
            return _Tok("NUMBER", s[start:self.i], self.line, start, self.i)
        self.i += 1
        return _Tok("UNKNOWN", c, self.line, start, self.i)


# parser

_LEVELS = [("OR",), ("XOR",), ("AND",), ("EQUAL", "NE"),
           ("LT", "LE", "GT", "GE"), ("SHL", "SHR"), ("ADD", "SUB"),
           ("MUL", "DIV", "MOD")]


def _java_div(a: int, b: int) -> int:
    if b == 0:
        raise DigitalTestError("Division by zero in a test expression")
    q = abs(a) // abs(b)
    return _long(q if (a >= 0) == (b >= 0) else -q)


def _java_mod(a: int, b: int) -> int:
    if b == 0:
        raise DigitalTestError("Division by zero in a test expression")
    return _long(a - b * _java_div(a, b))


_BINARY = {
    "OR": lambda a, b: a | b, "XOR": lambda a, b: a ^ b,
    "AND": lambda a, b: a & b,
    "EQUAL": lambda a, b: 1 if a == b else 0,
    "NE": lambda a, b: 1 if a != b else 0,
    "LT": lambda a, b: 1 if a < b else 0, "LE": lambda a, b: 1 if a <= b else 0,
    "GT": lambda a, b: 1 if a > b else 0, "GE": lambda a, b: 1 if a >= b else 0,
    "SHL": lambda a, b: _long(a << (b & 63)), "SHR": lambda a, b: a >> (b & 63),
    "ADD": lambda a, b: _long(a + b), "SUB": lambda a, b: _long(a - b),
    "MUL": lambda a, b: _long(a * b), "DIV": _java_div, "MOD": _java_mod,
}


class _Context:
    def __init__(self, parent: "_Context | None" = None):
        self.parent = parent
        self.vars: dict[str, int] = {}

    def get(self, name: str) -> int:
        c: _Context | None = self
        while c is not None:
            if name in c.vars:
                return c.vars[name]
            c = c.parent
        raise _Dynamic(
            f"'{name}' is not a loop or let variable; Digital reads it from "
            f"the circuit while the test runs")

    def set(self, name: str, value: int) -> None:
        self.vars[name] = value


def _sign_ext(bits: int, value: int) -> int:
    if bits < 0 or bits > 63:
        raise DigitalTestError(f"Invalid value {bits} in function signExt")
    mask = (1 << bits) - 1
    sign = 1 << (bits - 1) if bits > 0 else 0
    if value & sign:
        return _long((value & mask) | ~mask)
    return value & mask


class _Parser:
    def __init__(self, text: str):
        self.text = _strip_comments(text)
        self.names: list[str] = []
        self.preamble: list[str] = []
        self.sc: _Scanner

    # header
    def parse(self) -> tuple[list, bool]:
        """Returns (program, empty). `program` is a list of emitters."""
        lines = self.text.split("\n")
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx >= len(lines):
            return [], True
        header_line = lines[idx]
        for name in header_line.split():
            if name in self.names:
                raise DigitalTestError(f"Signal {name} is used twice!", idx + 1)
            self.names.append(name)
        pos = sum(len(l) + 1 for l in lines[:idx + 1])
        self.sc = _Scanner(self.text, pos, idx + 2)
        program = self._rows(None)
        self._expect("EOF")
        return program, False

    # statements
    def _unexpected(self, t: _Tok) -> DigitalTestError:
        shown = t.text if t.kind in ("IDENT", "UNKNOWN", "NUMBER") else t.kind
        return DigitalTestError(f"Unexpected token ({shown}) in line {t.line}.", t.line)

    def _expect(self, kind: str) -> _Tok:
        t = self.sc.next()
        if t.kind != kind:
            raise self._unexpected(t)
        return t

    def _const_int(self) -> int:
        expr = self._expression()
        try:
            return expr(_Context())
        except _Dynamic as d:
            raise DigitalTestError(d.reason)

    def _rows(self, end_kind: str | None) -> list:
        out: list = []
        while True:
            t = self.sc.peek()
            k = t.kind
            if k == "EOL":
                self.sc.next()
            elif k == "EOF":
                if end_kind is not None:
                    raise self._unexpected(t)
                return out
            elif k in ("BITS", "OPEN", "IDENT", "NUMBER"):
                out.append(self._single_row())
            elif k == "INIT":
                self.sc.next()
                name = self._expect("IDENT").text
                self._expect("EQUAL")
                neg = ""
                if self.sc.peek().kind == "SUB":
                    self.sc.next()
                    neg = "-"
                num = self._expect("NUMBER")
                decode_number(num.text)
                self._expect("SEMICOLON")
                self.preamble.append(f"init {name}={neg}{num.text};")
            elif k == "MEMORY":
                self.sc.next()
                name = self._expect("IDENT").text
                self._expect("OPEN")
                addr = self._expect("NUMBER").text
                decode_number(addr)
                self._expect("CLOSE")
                self._expect("EQUAL")
                val = self._expect("NUMBER").text
                decode_number(val)
                self._expect("SEMICOLON")
                self.preamble.append(f"memory {name}({addr})={val};")
            elif k == "PROGRAM":
                self.sc.next()
                self._expect("OPEN")
                words = []
                while True:
                    w = self._expect("NUMBER")
                    decode_number(w.text)
                    words.append(w.text)
                    t2 = self.sc.next()
                    if t2.kind == "CLOSE":
                        break
                    if t2.kind != "COMMA":
                        raise self._unexpected(t2)
                self.preamble.append("program(" + ",".join(words) + ")")
            elif k == "DECLARE":
                start = self.sc.next()
                name = self._expect("IDENT").text
                self._expect("EQUAL")
                self._expression()
                semi = self._expect("SEMICOLON")
                self.preamble.append(
                    " ".join(self.text[start.start:semi.end].split()))
            elif k == "END":
                self.sc.next()
                if end_kind is None:
                    raise self._unexpected(self.sc.next())
                self._expect(end_kind)
                return out
            elif k == "LET":
                self.sc.next()
                name = self._expect("IDENT").text
                self._expect("EQUAL")
                expr = self._expression()
                self._expect("SEMICOLON")
                out.append(("let", name, expr))
            elif k == "RESETRANDOM":
                self.sc.next()
                self._expect("SEMICOLON")
            elif k == "REPEAT":
                self.sc.next()
                self._expect("OPEN")
                count = self._const_int()
                self._expect("CLOSE")
                if count > MAX_LOOPS:
                    raise DigitalTestError("Too many iterations in a loop.", t.line)
                out.append(("loop", "n", count, [self._single_row()]))
            elif k == "LOOP":
                self.sc.next()
                self._expect("OPEN")
                var = self._expect("IDENT").text
                self._expect("COMMA")
                count = self._const_int()
                self._expect("CLOSE")
                if count > MAX_LOOPS:
                    raise DigitalTestError("Too many iterations in a loop.", t.line)
                out.append(("loop", var, count, self._rows("LOOP")))
            elif k == "WHILE":
                self.sc.next()
                self._expect("OPEN")
                cond = self._expression()
                self._expect("CLOSE")
                out.append(("while", cond, self._rows("WHILE"), t.line))
            else:
                raise self._unexpected(t)

    def _single_row(self) -> tuple:
        appenders: list = []
        line = None
        while True:
            t = self.sc.next()
            if line is None:
                line = t.line
            k = t.kind
            if k == "NUMBER":
                value = decode_number(t.text)
                raw = t.text
                appenders.append(lambda ctx, raw=raw, value=value:
                                 [Cell(raw, "int", value)])
            elif k == "BITS":
                self._expect("OPEN")
                count = self._const_int()
                self._expect("COMMA")
                expr = self._expression()
                self._expect("CLOSE")
                appenders.append(lambda ctx, count=count, expr=expr:
                                 _bits(count, expr(ctx)))
            elif k == "IDENT":
                upper = t.text.upper()
                if upper == "C":
                    cell = Cell(t.text, "clock", None)
                elif upper == "X":
                    cell = Cell(t.text, "dontcare", None)
                elif upper == "Z":
                    cell = Cell(t.text, "highZ", None)
                else:
                    raise DigitalTestError(
                        f"Value {t.text} in line {t.line} is not a number!", t.line)
                appenders.append(lambda ctx, cell=cell: [cell])
            elif k == "OPEN":
                expr = self._expression()
                self._expect("CLOSE")
                appenders.append(lambda ctx, expr=expr: [_int_cell(expr(ctx))])
            elif k in ("EOL", "EOF"):
                return ("row", appenders, line)
            else:
                raise self._unexpected(t)

    # expressions
    def _expression(self, level: int = 0):
        if level >= len(_LEVELS):
            return self._primary()
        ops = _LEVELS[level]
        acc = self._expression(level + 1)
        while self.sc.peek().kind in ops:
            fn = _BINARY[self.sc.next().kind]
            left, right = acc, self._expression(level + 1)
            acc = (lambda ctx, fn=fn, left=left, right=right:
                   fn(left(ctx), right(ctx)))
        return acc

    def _primary(self):
        t = self.sc.next()
        k = t.kind
        if k == "IDENT":
            name = t.text
            if self.sc.peek().kind == "OPEN":
                args = []
                while True:
                    self.sc.next()               # "(" or ","
                    args.append(self._expression())
                    if self.sc.peek().kind != "COMMA":
                        break
                self._expect("CLOSE")
                return self._function(name, args, t.line)
            return lambda ctx, name=name: ctx.get(name)
        if k == "NUMBER":
            num = decode_number(t.text)
            return lambda ctx, num=num: num
        if k == "SUB":
            inner = self._primary()
            return lambda ctx, inner=inner: _long(-inner(ctx))
        if k == "BIN_NOT":
            inner = self._primary()
            return lambda ctx, inner=inner: _long(~inner(ctx))
        if k == "LOG_NOT":
            inner = self._primary()
            return lambda ctx, inner=inner: 1 if inner(ctx) == 0 else 0
        if k == "OPEN":
            inner = self._expression()
            self._expect("CLOSE")
            return inner
        raise self._unexpected(t)

    def _function(self, name: str, args: list, line: int):
        arity = {"signExt": 2, "random": 1, "ite": 3}.get(name)
        if arity is None:
            raise DigitalTestError(f"Function {name} not found in line {line}!", line)
        if len(args) != arity:
            raise DigitalTestError(
                f"Number of arguments in function {name} in line {line} not "
                f"correct (found {len(args)}, expected {arity})!", line)
        if name == "signExt":
            return lambda ctx: _sign_ext(args[0](ctx), args[1](ctx))
        if name == "ite":
            return lambda ctx: args[1](ctx) if args[0](ctx) != 0 else args[2](ctx)

        def _random(ctx):
            raise _Dynamic("random() values come from a clock-seeded "
                           "generator inside Digital")
        return _random


def _int_cell(value: int) -> Cell:
    return Cell(f"({value})" if value < 0 else str(value), "int", value)


def _bits(count: int, value: int) -> list[Cell]:
    if count <= 0:
        return []
    out = []
    mask = 1 << (count - 1)
    for _ in range(count):
        bit = 1 if (value & mask) != 0 else 0
        out.append(Cell(str(bit), "int", bit))
        mask >>= 1
    return out


# emission

def _emit(program: list, ctx: _Context, result: ExpandedTest, n_cols: int) -> None:
    for item in program:
        tag = item[0]
        if tag == "row":
            _, appenders, line = item
            cells: list[Cell] = []
            for ap in appenders:
                cells.extend(ap(ctx))
            if len(result.rows) >= MAX_ROWS:
                raise DigitalTestError(
                    f"More than {MAX_ROWS:,} rows; DLC stops expanding here.", line)
            if len(cells) != n_cols and result.row_error is None:
                result.row_error = (f"Expected {n_cols} but found {len(cells)} "
                                    f"values in line {line}!")
            result.rows.append(cells)
            result.row_lines.append(line)
        elif tag == "let":
            _, name, expr = item
            ctx.set(name, expr(ctx))
        elif tag == "loop":
            _, var, count, body = item
            child = _Context(ctx)
            for i in range(count):
                child.set(var, i)
                _emit(body, child, result, n_cols)
        elif tag == "while":
            _, cond, body, line = item
            n = 0
            while cond(ctx) != 0:
                n += 1
                if n > MAX_LOOPS:
                    raise DigitalTestError("Too many iterations in a loop.", line)
                _emit(body, ctx, result, n_cols)


def expand_test(text: str) -> ExpandedTest:
    """Expand a Testcase's data string into the rows Digital would run."""
    result = ExpandedTest()
    parser = _Parser(text or "")
    try:
        program, empty = parser.parse()
    except DigitalTestError as e:
        result.headers = list(parser.names)
        result.error, result.error_line = str(e), e.line
        return result
    result.headers = list(parser.names)
    result.preamble = list(parser.preamble)
    if empty:
        return result
    try:
        _emit(program, _Context(), result, len(parser.names))
    except _Dynamic as d:
        result.rows, result.row_lines, result.row_error = [], [], None
        result.dynamic = d.reason
    except DigitalTestError as e:
        result.rows, result.row_lines, result.row_error = [], [], None
        result.error, result.error_line = str(e), e.line
    return result
