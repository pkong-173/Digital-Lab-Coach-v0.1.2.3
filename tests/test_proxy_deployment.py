"""
Delete when necessary
"""

import re
from pathlib import Path

from dlc.llm import client

ROOT = Path(__file__).resolve().parents[1]


def _route_timeout_seconds() -> int:
    text = (ROOT / "proxy/openshift/dlc-proxy.yaml").read_text(encoding="utf-8")
    route = text.split("kind: Route", 1)[1]
    m = re.search(r"^\s*haproxy\.router\.openshift\.io/timeout:\s*\"?(\d+)(s|m)\"?\s*$",
                  route, re.MULTILINE)
    assert m, "the Route in dlc-proxy.yaml has no timeout annotation"
    return int(m.group(1)) * (60 if m.group(2) == "m" else 1)


def test_route_timeout_outlasts_the_student_tools_wait(monkeypatch):
    monkeypatch.delenv("DLC_LLM_TIMEOUT", raising=False)
    # _call_proxy waits the model-call limit plus 30 seconds for the course server
    assert _route_timeout_seconds() > client._request_timeout() + 30


def test_release_guide_and_proxy_readme_tell_the_operator():
    guide = (ROOT / "docs/RELEASE_GUIDE.md").read_text(encoding="utf-8")
    assert "haproxy.router.openshift.io/timeout" in guide and "HTTP 504" in guide
    readme = (ROOT / "proxy/README.md").read_text(encoding="utf-8")
    assert "haproxy.router.openshift.io/timeout" in readme
