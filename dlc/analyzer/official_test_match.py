from dlc.analyzer.wire_completeness import Issue, IssueSeverity

KIND = "official_test_mismatch"

_FIX_MISSING = "Open Settings → Official tests to see the rows that will run."
_FIX_COACH = ("Or open Layer 3 → Coverage coach (Mode B), generate new "
              "tests and run again.")


def _fix_modified() -> str:
    try:
        from dlc.l3 import official_store
        names = official_store.editable_defaults()
    except Exception:
        names = []
    if names:
        return ("Open Settings → Official tests to see the rows that run "
                "instead, or rewrite them there where that is allowed "
                f"(currently only {', '.join(names)}). {_FIX_COACH}")
    return ("Open Settings → Official tests to see the rows that run "
            f"instead. {_FIX_COACH}")


def check_official_test_match(circuit, filename: str) -> list[Issue]:
    from dlc.testing.inject import file_test_status

    try:
        status = file_test_status(circuit, filename)
    except Exception:
        return []
    if status not in ("modified", "missing"):
        return []

    if status == "missing":
        title = "This file has no test rows of its own"
        message = ("An official test is registered for this file, but the "
                   "file carries no rows. Test runs use the official test.")
        fix = _FIX_MISSING
    else:
        title = "Your testcase is not the official test"
        message = ""
        fix = _fix_modified()

    return [Issue(
        kind=KIND,
        severity=IssueSeverity.WARNING,
        title=title,
        message=message,
        suggested_fix=fix,
    )]
