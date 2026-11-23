"""SharePoint: documents whose statements are checked against the truth every day; stale
and contradicting statements are defects, and a scenario."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harness.app import create_app
from harness.truth import generate

TENANT = "kestrelvalley.example"


@pytest.fixture
def harness():
    truth = generate(people=600, seed=7)
    return TestClient(create_app(truth)), truth


def graph(client) -> dict:
    r = client.post(
        f"/entra/{TENANT}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": "veromesh-connector", "client_secret": "harness-secret"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def read(client, h, site: str, folder: str, name: str) -> str:
    s = client.get(f"/sharepoint/v1.0/sites?search={site}", headers=h).json()["value"][0]
    drive = client.get(f"/sharepoint/v1.0/sites/{s['id']}/drive", headers=h).json()["id"]
    f = next(i for i in client.get(f"/sharepoint/v1.0/drives/{drive}/root/children", headers=h).json()["value"] if i["name"] == folder)
    item = next(
        i for i in client.get(f"/sharepoint/v1.0/drives/{drive}/items/{f['id']}/children", headers=h).json()["value"] if i["name"] == name
    )
    r = client.get(f"/sharepoint/v1.0/drives/{drive}/items/{item['id']}/content", headers=h)
    assert r.headers["content-type"].startswith("text/markdown")
    return r.text


def test_sites_drives_and_documents(harness):
    client, truth = harness
    assert client.get("/sharepoint/v1.0/sites").status_code == 401
    h = graph(client)
    names = {s["displayName"] for s in client.get("/sharepoint/v1.0/sites?search=*", headers=h).json()["value"]}
    assert names == {"Finance", "Clinical Operations", "Information Technology", "Data Governance", "People"}
    text = read(client, h, "Finance", "Procedures", "Invoice exception escalation.md")
    assert truth.head_of("AP").name in text


def test_the_policy_contradicts_the_erp_from_the_start(harness):
    client, _ = harness
    text = read(client, graph(client), "Finance", "Policies", "Delegation of Authority Policy.md")
    assert "| APClerk | ApproveSupplierInvoice | 7,500 USD |" in text
    kinds = {d["kind"] for d in client.get("/harness/answer-key").json()["defects"] if d["system"] == "sharepoint"}
    assert kinds == {"policy-system-mismatch", "register-conflicts-with-catalog"}


def test_documents_go_stale_as_the_clock_moves(harness):
    client, truth = harness
    ap_head = truth.head_of("AP")
    client.post("/harness/clock", json={"days": 50})
    d = [x for x in client.get("/harness/answer-key").json()["defects"] if x["system"] == "sharepoint"]
    leaver = [x for x in d if x["kind"] == "document-names-leaver" and x["about"].get("person") == ap_head.id]
    assert any(x["about"]["document"].endswith("Invoice exception escalation.md") for x in leaver)
    outdated = {x["about"]["document"] for x in d if x["kind"] == "document-outdated"}
    assert "Clinical Operations/Charters/Clinical Informatics charter.md" in outdated
    # The charter still says what it said: documents are not rewritten.
    assert "reports to Clinical Operations" in read(client, graph(client), "Clinical", "Charters", "Clinical Informatics charter.md")


def test_the_council_minutes_are_true_when_written(harness):
    client, _ = harness
    client.post("/harness/clock", json={"days": 46})  # past 15 January
    key = client.get("/harness/answer-key").json()
    minutes = [p for p in key["documents"] if "/Minutes/" in p]
    assert len(minutes) == 2  # December and January
    jan = next(p for p in minutes if p.endswith("2027-01.md"))
    otp = next(c for c in key["documents"][jan] if c.get("id") == "OnTimePaymentRate")
    assert otp["person"] is None  # the AP Manager left on day 20; the post was vacant on 15 January


def test_the_document_scenario(harness):
    client, _ = harness
    client.post("/harness/clock", json={"days": 50})
    sc = next(s for s in client.get("/harness/scenarios").json()["scenarios"] if s["id"] == "document-claims")
    stale = sc["expected"]["statements"]
    assert "Finance/Policies/Delegation of Authority Policy.md#0" in stale and len(stale) >= 4
    r = client.post("/harness/score", json={"document-claims": {"statements": stale[:1]}}).json()
    res = next(x for x in r["results"] if x["id"] == "document-claims")
    assert res["fields"]["statements"]["precision"] == 1.0 and res["fields"]["statements"]["recall"] < 1.0
