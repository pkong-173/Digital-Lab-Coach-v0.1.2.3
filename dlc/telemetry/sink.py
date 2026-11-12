"""Usage events are not recorded. `log_events` takes what the app describes
and keeps none of it: no file is written and nothing is sent."""

from __future__ import annotations


def log_events(session_id: str | None, events: list) -> int:
    return 0
