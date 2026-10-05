"""The Digital test-language expander, checked against the cases in
Digital's own parser tests (ParserTest, ParserLoopTest, ParserLetTest,
ParserExpressionTest) and the shapes seen in COMP 311 labs."""

import struct

import pytest

from dlc.testing.testlang import (
    DigitalTestError, decode_number, expand_test,
)


def rows_of(text):
    r = expand_test(text)
    assert r.error is None, r.error
    return r


def ints(r, col):
    return [row[col].value for row in r.rows]


# Digital's ParserTest

def test_ok_rows_and_dontcare():
    r = rows_of("A B\n0 1\n1 0\nX x")
    assert r.headers == ["A", "B"] and len(r.rows) == 3
    assert [c.value for c in r.rows[0]] == [0, 1]
    assert [c.kind for c in r.rows[2]] == ["dontcare", "dontcare"]
    assert r.rows[2][1].raw == "x"                 # lower case is accepted


def test_number_literals_like_bits_decode():
    assert ints(rows_of("A B\n0 0xff"), 1) == [255]
    assert ints(rows_of("A B\n0 0b11111111"), 1) == [255]
    assert ints(rows_of("A B\n0 0.5:4"), 1) == [8]
    assert ints(rows_of("A B\n0 0.5"), 1) == [struct.unpack(">i", struct.pack(">f", 0.5))[0]]
    assert ints(rows_of("A B\n0 0.5d"), 1) == [struct.unpack(">q", struct.pack(">d", 0.5))[0]]
    assert decode_number("010") == 8               # leading zero is octal in Digital
    assert decode_number("0") == 0 and decode_number("00") == 0
    assert decode_number("-5") == -5
    with pytest.raises(DigitalTestError):
        decode_number("0x")
    with pytest.raises(DigitalTestError):
        decode_number("09")


def test_missing_value_is_a_row_error_not_fatal():
    r = expand_test("A B\n0 0\n1")
    assert r.error is None and len(r.rows) == 2
    assert r.row_error == "Expected 2 but found 1 values in line 3!"
    assert len(r.rows[1]) == 1


def test_invalid_value_is_fatal():
    r = expand_test("A B\n0 0\n1 u")
    assert r.error == "Value u in line 3 is not a number!" and r.rows == []


def test_clock():
    r = rows_of("A B\nC 1\nC 0")
    assert [row[0].kind for row in r.rows] == ["clock", "clock"]
    assert ints(r, 1) == [1, 0]


def test_repeat_with_n():
    r = rows_of("A B\nrepeat(10) C (n*2)\n")
    assert len(r.rows) == 10
    assert all(row[0].kind == "clock" for row in r.rows)
    assert ints(r, 1) == [i * 2 for i in range(10)]


def test_repeat_with_bits():
    r = rows_of("A B C D \nrepeat(8) X bits(3,n)\n")
    assert r.headers == ["A", "B", "C", "D"] and len(r.rows) == 8
    for i, row in enumerate(r.rows):
        assert row[0].kind == "dontcare"
        assert [c.value for c in row[1:]] == [(i >> 2) & 1, (i >> 1) & 1, i & 1]
        assert [c.raw for c in row[1:]] == [str((i >> 2) & 1), str((i >> 1) & 1), str(i & 1)]


def test_comment_before_header_and_after_rows():
    assert len(rows_of("#test\nA B\n1 1").rows) == 1
    r = rows_of("A B Y\n1 1 1\n#test")
    assert r.headers == ["A", "B", "Y"] and len(r.rows) == 1


def test_header_spacing_and_tabs():
    assert rows_of("A   B     C  D\n1 1 1 1").headers == ["A", "B", "C", "D"]
    r = rows_of("A\tB\tC \t D\n1\t1\t1\t1")
    assert r.headers == ["A", "B", "C", "D"] and len(r.rows[0]) == 4


def test_empty_lines_and_odd_header_names():
    r = rows_of("A_i B_i C_i-1 C_i S_i\n 0 0 0 0 0\n 0 0 1 0 1\n\n 0 1 0 0 1\n")
    assert r.headers[2] == "C_i-1" and len(r.rows) == 3


def test_big_repeat_with_shifts_bug1():
    r = rows_of("C_i-1 A B    C   S\nrepeat(1<<16) 0 (n>>8) (n&255) ((n>>8)*(n&255)) 0")
    assert len(r.rows) == 1 << 16
    assert [c.value for c in r.rows[257]] == [0, 1, 1, 1, 0]


def test_let_reading_a_circuit_signal_is_dynamic():
    r = expand_test("A B Y\nlet a=A+B;\n1 1 1\n#test")
    assert r.error is None and r.rows == []
    assert r.dynamic and "'A'" in r.dynamic


# ParserLoopTest, LetTest

def test_loop_and_loop_var():
    for var in ("n", "i"):
        r = rows_of(f"A B\nloop({var},10)\n C ({var}*2)\nend loop")
        assert len(r.rows) == 10 and ints(r, 1) == [i * 2 for i in range(10)]


def test_nested_loops():
    r = rows_of("A B\nloop(i,10)\nloop(j,10)\n C (i+j*2)\nend loop\nend loop")
    assert len(r.rows) == 100
    assert ints(r, 1) == [i + j * 2 for i in range(10) for j in range(10)]


def test_loop_with_two_lines():
    r = rows_of("A B\nloop(i,10)\n C (i*2)\n C (i*2+1)\nend loop")
    assert ints(r, 1) == list(range(20))


def test_loop_errors():
    assert expand_test("A B\nloop(i,10) C (i)").error.startswith("Unexpected token (EOF)")
    assert expand_test("A B\n C 1\nend loop").error.startswith("Unexpected token")
    assert expand_test("A B\n C 1\nend").error.startswith("Unexpected token")


def test_let_inside_loops():
    r = rows_of("A B\nloop(n,10)\n let a=n*2; let b = a * 2;\nC (b)\nend loop")
    assert ints(r, 1) == [i * 4 for i in range(10)]
    r = rows_of("A B\nloop(n,10)\n let n=n*2;\nC (n)\nend loop")
    assert ints(r, 1) == [i * 2 for i in range(10)]
    r = rows_of("A B\nloop(n,3)\n  let a=n*2;\n  loop(m,3)\n    let b=m*3+a;\n"
                "    (a) (b)\n  end loop\nend loop")
    assert [(row[0].value, row[1].value) for row in r.rows] == [
        (n * 2, m * 3 + n * 2) for n in range(3) for m in range(3)]


# ParserExpressionTest

def _val(expr, **vars_):
    lets = "".join(f"let {k}={v};\n" for k, v in vars_.items())
    return rows_of(f"A\n{lets}({expr})\n").rows[0][0].value


def test_expression_semantics():
    assert _val("2+5") == 7 and _val("9-2") == 7
    assert _val("2*n", n=3) == 6 and _val("2*n+1", n=3) == 7 and _val("1+2*n", n=3) == 7
    assert _val("2*(1+n)", n=3) == 8 and _val("2*(n-1)", n=3) == 4 and _val("(2*n)/3", n=3) == 2
    assert _val("-1") == -1 and _val("-1-1") == -2
    assert _val("7%8") == 7 and _val("8%8") == 0 and _val("9%8") == 1
    assert _val("1<<3") == 8 and _val("8>>2") == 2
    assert _val("1<3") == 1 and _val("3<1") == 0 and _val("3>1") == 1 and _val("1>3") == 0
    assert _val("1=3") == 0 and _val("3=3") == 1 and _val("1!=2") == 1 and _val("2!=2") == 0
    assert _val("1<=3") == 1 and _val("3<=3") == 1 and _val("4<=3") == 0
    assert _val("3>=1") == 1 and _val("3>=3") == 1 and _val("3>=4") == 0
    assert _val("3|4") == 7 and _val("7&2") == 2 and _val("7^2") == 5
    assert _val("~0") == -1 and _val("~1") == -2
    assert _val("!0") == 1 and _val("!1") == 0 and _val("!2") == 0
    assert _val("(n>>8)*(n&255)", n=257) == 1
    assert _val("0x10+1") == 0x11 and _val("0b10+1") == 0b11
    assert _val("a*b", a=2, b=3) == 6
    assert _val("signExt(4,15)") == -1 and _val("signExt(4,14)") == -2
    assert _val("signExt(4,1)") == 1 and _val("signExt(4,2)") == 2
    assert _val("ite(1=1,8,0)") == 8 and _val("ite(1=0,8,2)") == 2
    assert _val("ite(1<1,8,2)") == 2 and _val("ite(1>0,8,2)") == 8
    assert _val("(!1&!0) | (1&1)") == 1


def test_expression_errors():
    assert "n" in expand_test("A\n(n*3)\n").dynamic
    assert expand_test("A\n(n*3))\n").error.startswith("Unexpected token")
    assert "Division by zero" in expand_test("A\n(1/0)\n").error
    assert "not found" in expand_test("A\n(foo(1))\n").error
    assert "arguments" in expand_test("A\n(ite(1,2))\n").error


# COMP 311 shapes

EX1 = """A B C X Y Z

loop(A,2)
   loop(B,2)
      loop(C,2)
         bits(1,A) bits(1,B) bits(1,C) bits(1, (!A&!C) | (A&B)) bits(1, !B|!C) bits(1, (!A&!B&!C) | (A&C))
      end loop
   end loop
end loop"""


def test_nested_loops_with_bits_and_boolean_expressions():
    r = rows_of(EX1)
    assert r.headers == ["A", "B", "C", "X", "Y", "Z"] and len(r.rows) == 8
    assert r.row_error is None and r.dynamic is None
    for row in r.rows:
        a, b, c, x, y, z = [cell.value for cell in row]
        assert x == ((not a and not c) or (a and b))
        assert y == ((not b) or (not c))
        assert z == ((not a and not b and not c) or (a and c))
    assert [c.raw for c in r.rows[5]] == ["1", "0", "1", "0", "1", "1"]


def test_register_file_loops_match_the_old_expansion():
    text = ("WriteReg WriteData RegWrite Clock ReadReg1 ReadReg2 ReadData1 ReadData2\n"
            "1 25 1 C 1 0 25 0\nloop(N, 30)\n\t(N+1) (N-60) 1 C (N+1) (N+1) (N-60) (N-60)\nend loop\n")
    r = rows_of(text)
    assert len(r.rows) == 31
    assert [c.raw for c in r.rows[1]] == ["1", "(-60)", "1", "C", "1", "1", "(-60)", "(-60)"]
    assert r.rows[30][1].value == -31


def test_model_statements_become_the_preamble():
    assert expand_test("A\nprogram(1,\n2)\n1\n").error.startswith("Unexpected token (EOL)")
    text = ("Clk Out\ninit Reg=5;\nmemory ram(3) = 0x1f;\n"
            "program(1, 2, 3)\ndeclare half = Out >> 1 ;\nC 0\n")
    r = rows_of(text)
    assert r.preamble == ["init Reg=5;", "memory ram(3)=0x1f;", "program(1,2,3)",
                          "declare half = Out >> 1 ;"]
    assert len(r.rows) == 1
    assert rows_of("A\ninit A=-3;\n1\n").preamble == ["init A=-3;"]


def test_while_on_variables_runs_and_on_signals_is_dynamic():
    r = rows_of("A\nlet i=0;\nwhile(i<3)\n(i)\nlet i=i+1;\nend while\n")
    assert ints(r, 0) == [0, 1, 2]
    r = expand_test("A Q\nwhile(Q!=3)\nC 0\nend while\n")
    assert r.rows == [] and "'Q'" in r.dynamic
    err = expand_test("A\nwhile(1)\n1\nend while\n").error
    assert err and ("iterations" in err or "rows" in err)


def test_random_is_dynamic_and_resetrandom_is_accepted():
    r = expand_test("A\nresetRandom;\n(random(4))\n")
    assert r.rows == [] and "random()" in r.dynamic


def test_bare_negative_number_is_not_digital_syntax():
    assert expand_test("A\n-5\n").error.startswith("Unexpected token (SUB)")
    assert rows_of("A\n(-5)\n").rows[0][0].raw == "(-5)"


def test_empty_and_comment_only_texts_are_empty_not_errors():
    for text in ("", "   \n  \n", "# just a comment\n# another\n"):
        r = expand_test(text)
        assert r.headers == [] and r.rows == [] and r.error is None


def test_duplicate_header_name():
    assert expand_test("A A\n1 1\n").error == "Signal A is used twice!"


def test_repeat_needs_its_row_on_the_same_line():
    r = expand_test("A B\nrepeat(3)\n1 0\n")
    assert r.row_error == "Expected 2 but found 0 values in line 2!"
