import time

import pytest
from fastapi.testclient import TestClient

import dlc.web.server as srv


class _FakeServer:
    should_exit = False


@pytest.fixture
def auto_exit(monkeypatch):
    monkeypatch.setenv("DLC_AUTO_EXIT", "1")
    monkeypatch.setenv("DLC_AUTO_EXIT_GRACE", "0.4")
    fake = _FakeServer()
    monkeypatch.setattr(srv, "_SERVER", fake)
    monkeypatch.setitem(srv._PRESENCE, "last", 0.0)
    monkeypatch.setitem(srv._PRESENCE, "exit_reason", None)
    return fake


def test_without_the_launcher_flag_a_goodbye_changes_nothing(monkeypatch):
    monkeypatch.delenv("DLC_AUTO_EXIT", raising=False)
    fake = _FakeServer()
    monkeypatch.setattr(srv, "_SERVER", fake)
    c = TestClient(srv.app)
    assert c.post("/api/presence").json()["ok"] is True
    assert c.post("/api/presence/bye").json()["auto_exit"] is False
    time.sleep(0.3)
    assert fake.should_exit is False


def test_last_tab_closed_stops_the_server(auto_exit):
    c = TestClient(srv.app)
    c.post("/api/presence")
    assert c.post("/api/presence/bye").json()["auto_exit"] is True
    time.sleep(0.8)
    assert auto_exit.should_exit is True
    assert srv._PRESENCE["exit_reason"] == "page closed"


def test_a_reload_or_second_tab_keeps_it_alive(auto_exit):
    c = TestClient(srv.app)
    c.post("/api/presence")
    c.post("/api/presence/bye")
    time.sleep(0.2)
    c.post("/api/presence")
    time.sleep(0.5)
    assert auto_exit.should_exit is False
