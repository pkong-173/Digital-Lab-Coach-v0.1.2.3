from __future__ import annotations

import json
import os
import re
from pathlib import Path

_DEFAULTS_PATH = (Path(__file__).parent.parent.parent
                  / "data" / "official_tests_defaults.json")


def store_path() -> Path:
    env = os.environ.get("DLC_OFFICIAL_TESTS_PATH")
    return Path(env) if env else Path.home() / ".dlc" / "official_tests.json"


def validate_test_content(content: str) -> None:
    """Refuse text Digital itself would refuse, plus two DLC rules: the
    first line is the header, and there is at least one row."""
    from dlc.testing.spec import _strip_inline_comment, _tokenize
    from dlc.testing.testlang import expand_test

    first = None
    for i, line in enumerate((content or "").splitlines(), start=1):
        text = _strip_inline_comment(line).strip()
        if text:
            first = (i, text)
            break
    if first is None:
        raise ValueError("not Digital test format: no header line found.")
    i, text = first
    kinds = {_tokenize(c).kind for c in text.split()}
    if kinds <= {"int", "clock", "highZ", "dontcare", "loop_expr"}:
        raise ValueError(
            "not Digital test format: the first line must be the "
            "header (signal names), but line "
            f"{i} looks like a value row: {text!r}")
    ex = expand_test(content)
    if ex.error:
        raise ValueError(f"not Digital test format: {ex.error}")
    if ex.row_error:
        raise ValueError(
            f"not Digital test format: {ex.row_error[:-1]} "
            f"(the header has {len(ex.headers)} columns).")
    if not ex.rows and not ex.dynamic:
        raise ValueError("not Digital test format: a header but no test "
                         "rows.")


def _defaults() -> dict[str, dict]:
    env = os.environ.get("DLC_OFFICIAL_DEFAULTS_PATH")
    p = Path(env) if env else _DEFAULTS_PATH
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {name: e for name, e in data.items()
            if isinstance(e, dict) and not name.startswith("_")}


def editable_defaults() -> list[str]:
    return sorted(n for n, e in _defaults().items() if e.get("editable"))


_WORD_RE = re.compile(r"0[xX][0-9a-fA-F]{1,8}\Z")


def parse_program_words(words) -> list[str]:
    if words is None:
        return []
    tokens = (words.replace(",", " ").split() if isinstance(words, str)
              else [str(w).strip() for w in words])
    out: list[str] = []
    for i, tok in enumerate(tokens, start=1):
        if not _WORD_RE.match(tok):
            raise ValueError(
                f"Instruction word {i} is not valid: {tok[:24]}. Write each "
                f"word as 0x followed by up to 8 hex digits, for example "
                f"0x00628433.")
        out.append(f"0x{int(tok, 16):08x}")
    return out


def _runtime(entry: dict | None, key: str) -> str | None:
    import base64
    blob = (entry or {}).get("runtime")
    if not blob:
        return None
    try:
        data = json.loads(base64.b64decode(blob).decode("utf-8"))
    except Exception:
        return None
    v = data.get(key) if isinstance(data, dict) else None
    return v if isinstance(v, str) and v.strip() else None


def _program_of(default: dict | None) -> list[int] | None:
    from dlc.testing.inject import _data_words
    rom = _runtime(default, "rom")
    return _data_words(rom, "hex") if rom else None


def _row_count_of(default: dict | None) -> int | None:
    from dlc.testing.testlang import expand_test
    content = (default or {}).get("content")
    if not (content or "").strip():
        return None
    ex = expand_test(content)
    return None if (ex.error or ex.dynamic) else len(ex.rows)


def builtin_program(filename: str) -> list[int] | None:
    """The ROM words registered with the built-in test of `filename`, or
    None when that file has none. Never shown to the student."""
    return _program_of(_defaults().get(filename))


def builtin_row_count(filename: str) -> int | None:
    return _row_count_of(_defaults().get(filename))


def added_row_count(filename: str, content: str) -> int:
    """How many rows `content` adds after the built-in test of `filename`.
    Raises ValueError when the header or one of the built-in rows was
    changed: in a test that runs a registered program those rows go with
    the program, so a student's own rows come after them."""
    from dlc.testing.testlang import expand_test
    base = expand_test((_defaults().get(filename) or {}).get("content") or "")
    new = expand_test(content or "")
    if base.dynamic or base.error or new.dynamic or new.error:
        raise ValueError(
            f"The rows of '{filename}' cannot be counted "
            f"({new.dynamic or new.error or base.dynamic or base.error}). "
            f"Write the rows you add as plain value rows.")
    if new.headers != base.headers:
        raise ValueError(
            f"The header line of '{filename}' must stay as it is: "
            f"{' '.join(base.headers)}")

    def cells(row):
        return [(c.kind, c.value) for c in row]

    n = len(base.rows)
    for k in range(n):
        if k >= len(new.rows) or cells(new.rows[k]) != cells(base.rows[k]):
            raise ValueError(
                f"The first {n} rows of '{filename}' are the built-in test "
                f"and must stay as they are; row {k + 1} was changed or "
                f"removed. Add your own rows after them.")
    return len(new.rows) - n


def _load() -> dict[str, dict]:
    p = store_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, dict]) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")


def list_tests() -> list[dict]:
    user = _load()
    defaults = _defaults()
    merged: dict[str, dict] = {}
    for name, e in defaults.items():
        merged[name] = {**e, "_source": "default",
                        "_editable": bool(e.get("editable"))}
    for name, e in user.items():
        over = merged.get(name)
        merged[name] = {**e,
                        "_source": "override" if over else "user",
                        "_editable": over["_editable"] if over else True}
    out = []
    for name, e in sorted(merged.items()):
        item = {"filename": name, "sha1": e.get("sha1", ""),
                "content": e.get("content", ""), "source": e["_source"],
                "editable": e["_editable"], "has_program": False}
        default = defaults.get(name)
        if _program_of(default) is not None:
            extra = e.get("program_extra") if e["_source"] != "default" else []
            item.update(
                has_program=True, base_rows=_row_count_of(default),
                program_extra=list(extra) if isinstance(extra, list) else None)
        out.append(item)
    return out


def save_test(filename: str, content: str, *,
              allow_default_override: bool = False,
              program_extra=None) -> dict:
    """Store the user's version of an official test."""
    from dlc.l3.manifest import normalized_test_hash
    name = (filename or "").strip()
    if not name:
        raise ValueError("Filename is required.")
    if "/" in name or "\\" in name or not name.lower().endswith(".dig"):
        raise ValueError("Filename must be a plain .dig name, e.g. cpu.dig")
    if not (content or "").strip():
        raise ValueError("Testcase content is required.")
    default = _defaults().get(name)
    if (default is not None and not allow_default_override
            and not default.get("editable")):
        raise ValueError(
            f"'{name}' is a built-in default and can't be edited by hand — "
            f"run Mode B to all-set and use 'Adopt into official tests' to "
            f"extend it.")
    validate_test_content(content)
    data = _load()
    entry = {"content": content, "sha1": normalized_test_hash(content)}
    has_program = _program_of(default) is not None
    if has_program and allow_default_override:
        if program_extra is not None:
            entry["program_extra"] = parse_program_words(program_extra)
    elif has_program:
        added = added_row_count(name, content)
        words = parse_program_words(program_extra)
        if len(words) != added:
            raise ValueError(
                f"Rows added after the built-in {_row_count_of(default)}: "
                f"{added}. Instruction words given: {len(words)}. The two "
                f"counts must be equal: one word for each added row.")
        if added == 0:
            # nothing of the student's own to keep: the built-in test stands
            if data.pop(name, None) is not None:
                _save(data)
            return {"filename": name, "content": default.get("content", ""),
                    "sha1": default.get("sha1", ""), "source": "default"}
        entry["program_extra"] = words
    elif parse_program_words(program_extra):
        raise ValueError(
            f"'{name}' has no registered program, so it takes no "
            f"instruction words. Leave that box empty.")
    data[name] = entry
    _save(data)
    return {"filename": name, **entry,
            "source": "override" if default is not None else "user"}


def delete_test(filename: str) -> bool:
    data = _load()
    if filename not in data:
        return False
    del data[filename]
    _save(data)
    return True


def get_content(filename: str) -> str | None:
    entry = _load().get(filename) or _defaults().get(filename)
    content = (entry or {}).get("content")
    return content if (content or "").strip() else None


def source_of(filename: str) -> str | None:
    """'default', 'override' (the user's version stands in for a built-in
    test), 'user', or None when nothing is registered."""
    if filename in _load():
        return "override" if filename in _defaults() else "user"
    return "default" if filename in _defaults() else None


def program_words(filename: str) -> tuple[list[int], list[int]] | None:
    base = builtin_program(filename)
    if base is None:
        return None
    user = _load().get(filename)
    if user is None:
        return base, []
    extra = user.get("program_extra")
    if not isinstance(extra, list):
        return None
    try:
        return base, [int(w, 16) for w in parse_program_words(extra)]
    except ValueError:
        return None


def get_runtime_payload(filename: str, key: str) -> str | None:
    if key != "rom":
        return (_runtime(_defaults().get(filename), key)
                if filename not in _load() else None)
    parts = program_words(filename)
    if parts is None:
        return None
    return ",".join(f"{w:x}" for w in parts[0] + parts[1])


def status_for(filename: str, raw_data_string: str) -> str | None:
    entry = _load().get(filename) or _defaults().get(filename)
    if not entry or not entry.get("sha1"):
        return None
    from dlc.l3.manifest import normalized_test_hash
    return ("official"
            if normalized_test_hash(raw_data_string) == entry["sha1"]
            else "modified")
