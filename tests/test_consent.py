import io
import json
import re
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from dlc.telemetry import consent, sink, ship
from dlc.web.server import app


@pytest.fixture
def cenv(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_TELEMETRY_DB", str(tmp_path / "tele.db"))
    monkeypatch.setenv("DLC_MACHINE_CACHE", str(tmp_path / "machine.json"))
    monkeypatch.setenv("DLC_CONSENT_PATH", str(tmp_path / "consent.json"))
    monkeypatch.setenv("DLC_STUDY_CACHE", str(tmp_path / "study.json"))
    sheet = tmp_path / "sheet.md"
    sheet.write_text("# Sheet v1\n\nYou are asked to take part.\n", encoding="utf-8")
    monkeypatch.setenv("DLC_CONSENT_TEXT", str(sheet))
    monkeypatch.setenv("DLC_PROXY_DB", str(tmp_path / "proxy.db"))
    monkeypatch.setenv("DLC_COURSE_TOKEN", "tok")
    monkeypatch.setenv("DLC_PROXY_TOKEN", "tok")
    monkeypatch.delenv("DLC_PROXY_URL", raising=False)
    monkeypatch.delenv("DLC_STUDY_ID", raising=False)
    monkeypatch.delenv("DLC_LOCAL_STUDY_ID", raising=False)
    monkeypatch.delenv("DLC_ADMIN_TOKEN", raising=False)
    return tmp_path


def _wire_proxy(monkeypatch, study_id="26-2770"):
    """Make the app's httpx calls land on an in-process proxy TestClient."""
    from proxy import dlc_proxy
    if study_id:
        monkeypatch.setenv("DLC_STUDY_ID", study_id)
    pc = TestClient(dlc_proxy.app)
    import httpx

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        path = url.split("http://proxy.test", 1)[1]
        return pc.post(path, json=json, headers=headers or {})

    def fake_get(url, timeout=None, **kw):
        path = url.split("http://proxy.test", 1)[1]
        return pc.get(path)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("DLC_PROXY_URL", "http://proxy.test")
    return pc


# module

def test_version_follows_the_sheet_text(cenv):
    v1 = consent.consent_version()
    assert len(v1) == 12
    (cenv / "sheet.md").write_text("# Sheet v2\n\nChanged.\n", encoding="utf-8")
    assert consent.consent_version() != v1


def test_no_study_means_nothing_is_asked_and_nothing_ever_ships(cenv):
    assert consent.study_active() is False
    assert consent.consent_required() is False
    assert consent.telemetry_allowed() is True
    assert consent.shipping_allowed() is False
    assert sink.log_events("s", [{"kind": "upload", "count": 1}]) == 1


def test_study_from_env_requires_consent_until_decided(cenv, monkeypatch):
    monkeypatch.setenv("DLC_LOCAL_STUDY_ID", "26-2770")
    assert consent.study_active() and consent.consent_required()
    # pending: recorded locally, never shipped
    assert consent.telemetry_allowed() is True
    assert consent.shipping_allowed() is False
    assert ship.ship_pending()["reason"] == "no_proxy"


def test_agree_then_decline_purges_the_local_spool(cenv, monkeypatch):
    monkeypatch.setenv("DLC_LOCAL_STUDY_ID", "26-2770")
    with pytest.raises(ValueError):
        consent.record("agreed", name="")
    sink.log_events("s", [{"kind": "app_start"}])
    out = consent.record("agreed", name="Ada Lovelace",
                         signature="data:image/png;base64,iVBORw0KGgo=")
    assert out["decision"] == "agreed" and out["synced"] is False
    assert out["purged"] == 1
    assert consent.consent_required() is False
    assert consent.shipping_allowed() is True
    st = json.loads((cenv / "consent.json").read_text())
    assert st["name"] == "Ada Lovelace" and st["signature"].startswith("data:image/png")
    assert sink.log_events("s", [{"kind": "upload"}, {"kind": "tests_run_complete"}]) == 2
    out = consent.record("declined")
    assert out["purged"] == 2
    assert consent.telemetry_allowed() is False
    assert sink.log_events("s", [{"kind": "upload"}]) == 0
    assert ship.ship_pending()["reason"] == "no_proxy"
    assert consent.public_state()["decision"] == "declined"


def test_new_sheet_version_asks_again_but_keeps_the_last_decision(cenv, monkeypatch):
    monkeypatch.setenv("DLC_LOCAL_STUDY_ID", "26-2770")
    consent.record("agreed", name="Ada Lovelace")
    (cenv / "sheet.md").write_text("# Sheet v2\n\nChanged.\n", encoding="utf-8")
    assert consent.consent_required() is True
    assert consent.decision() == "agreed" and consent.shipping_allowed() is True


# app endpoints

def test_app_endpoints_state_text_and_decision(cenv, monkeypatch):
    monkeypatch.setenv("DLC_LOCAL_STUDY_ID", "26-2770")
    c = TestClient(app)
    st = c.get("/api/consent/state").json()
    assert st["study_id"] == "26-2770" and st["required"] is True and st["decision"] is None
    assert "Sheet v1" in c.get("/api/consent/text").text
    r = c.post("/api/consent", json={"decision": "agreed", "name": "A"})
    assert r.status_code == 400                     # too short a name
    r = c.post("/api/consent", json={"decision": "agreed", "name": "Ada Lovelace"})
    assert r.status_code == 200 and r.json()["state"]["decision"] == "agreed"
    assert c.get("/api/consent/state").json()["required"] is False
    kinds = [e["kind"] for e in sink.recent_events(10)]
    assert "consent_recorded" in kinds
    r = c.post("/api/consent", json={"decision": "declined"})
    assert r.json()["state"]["decision"] == "declined"
    r = c.post("/api/consent", json={"decision": "maybe"})
    assert r.status_code == 400


# with proxy

def test_study_is_read_from_the_course_server_health(cenv, monkeypatch):
    pc = _wire_proxy(monkeypatch, study_id="26-2770")
    h = pc.get("/v1/health").json()
    assert h["study_id"] == "26-2770" and 0 < h["survey_rate"] <= 1
    info = consent.study_info(refresh=True)
    assert info["study_id"] == "26-2770" and info["source"] == "proxy"
    assert consent.consent_required() is True
    assert consent.study_active() is True


def test_decision_reaches_the_proxy_and_decline_withdraws_events(cenv, monkeypatch):
    pc = _wire_proxy(monkeypatch, study_id="26-2770")
    consent.study_info(refresh=True)
    out = consent.record("agreed", name="Ada Lovelace",
                         signature="data:image/png;base64,iVBORw0KGgo=")
    assert out["synced"] is True
    # events now ship
    sink.log_events("s", [{"kind": "upload"}, {"kind": "l1_result"}])
    r = ship.ship_pending()
    assert r["reason"] == "ok" and r["shipped"] >= 2
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    hdr = {"X-DLC-Admin-Token": "adm"}
    res = pc.get("/admin/research", headers=hdr).json()
    assert res["counts"]["agreed"] == 1 and res["counts"]["declined"] == 0
    row = res["consents"][0]
    assert row["decision"] == "agreed" and row["has_signature"] is True
    assert "name" not in row and "signature" not in row      # not on the page
    r = pc.get("/admin/export.csv", headers=hdr, params={"table": "consents"})
    assert r.headers["content-disposition"] == 'attachment; filename="consents.csv"'
    lines = r.text.lstrip("\ufeff").strip().split("\n")
    assert lines[0] == ("id,install_id,study_id,sheet_version,decision,name,"
                        "signature_png,app_version,decided_at,received_at")
    cells = lines[1].split(",")
    assert cells[4:7] == ["agreed", "Ada Lovelace", "sig_1.png"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [A-Z]{2,5}", cells[8])
    assert "base64" not in r.text
    # the drawing itself: a PNG per agreed row, plus an index
    z = zipfile.ZipFile(io.BytesIO(pc.get("/admin/signatures.zip", headers=hdr).content))
    assert sorted(z.namelist()) == ["index.csv", "sig_1.png"]
    assert z.read("sig_1.png").startswith(b"\x89PNG")
    assert "Ada Lovelace" in z.read("index.csv").decode("utf-8")
    # the printable log carries the name and the signature inline
    page = pc.get("/admin/consents.html", headers=hdr).text
    assert "Ada Lovelace" in page and 'src="data:image/png;base64,' in page
    assert pc.get("/admin/consents.html").status_code == 401
    assert pc.get("/admin/signatures.zip").status_code == 401
    assert pc.get("/v1/health").json()["events"] >= 2
    out = consent.record("declined")
    assert out["synced"] is True
    assert pc.get("/v1/health").json()["events"] == 0
    res = pc.get("/admin/research", headers=hdr).json()
    assert res["counts"]["declined"] == 1 and res["counts"]["agreed"] == 0
    assert ship.ship_pending()["reason"] == "consent"


def test_survey_answers_are_counted_and_exported(cenv, monkeypatch):
    pc = _wire_proxy(monkeypatch, study_id="26-2770")
    consent.study_info(refresh=True)
    consent.record("agreed", name="Ada Lovelace")
    sink.log_events("s", [
        {"kind": "feedback_survey_shown", "feature": "modeA"},
        {"kind": "feedback_survey", "feature": "modeA", "filename": "alu.dig",
         "helpful": "yes", "answered": "partially", "comment": "show the row"},
        {"kind": "feedback_survey", "feature": "modeB",
         "helpful": "somewhat", "answered": "no", "comment": ""},
        {"kind": "feedback_survey_skipped", "feature": "explain"},
    ])
    assert ship.ship_pending()["reason"] == "ok"
    monkeypatch.setenv("DLC_ADMIN_TOKEN", "adm")
    hdr = {"X-DLC-Admin-Token": "adm"}
    s = pc.get("/admin/research", headers=hdr).json()["survey"]
    assert s["responses"] == 2 and s["shown"] == 1 and s["skipped"] == 1
    assert s["helpful"] == {"yes": 1, "somewhat": 1}
    assert s["answered"] == {"partially": 1, "no": 1}
    assert s["by_feature"] == {"modeA": 1, "modeB": 1}
    assert len(s["comments"]) == 1 and s["comments"][0]["comment"] == "show the row"
    csv = pc.get("/admin/export.csv", headers=hdr, params={"table": "surveys"}).text
    lines = csv.lstrip("\ufeff").strip().split("\n")
    assert lines[0] == "install_id,ts,feature,filename,helpful,answered,comment"
    assert len(lines) == 3 and "show the row" in csv
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [A-Z]{2,5}", lines[1].split(",")[1])
    assert lines[2].endswith(",modeB,,somewhat,no,")     # blank = not asked / not typed


def test_consent_endpoint_is_token_gated_and_validates(cenv, monkeypatch):
    from proxy import dlc_proxy
    pc = TestClient(dlc_proxy.app)
    body = {"install_id": "m1", "decision": "agreed", "name": "Ada",
            "version": "abc", "decided_at": 1700000000.0}
    assert pc.post("/v1/consent", json=body).status_code == 401
    hdr = {"X-DLC-Token": "tok"}
    assert pc.post("/v1/consent", json=body, headers=hdr).json()["stored"] == 1
    assert pc.post("/v1/consent", json=body, headers=hdr).json()["stored"] == 0
    r = pc.post("/v1/consent", json={**body, "decision": "later"}, headers=hdr)
    assert r.status_code == 400
