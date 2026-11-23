"""The ERP (Oracle Fusion REST), the EMR (FHIR R4), dbt's artifacts, and the scenarios
with their scorer."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from harness.app import create_app
from harness.truth import generate

ERP = {"Authorization": "Basic " + base64.b64encode(b"integration.user:harness").decode()}
FSCM = "/erp/fscmRestApi/resources/11.13.18.05"


@pytest.fixture
def harness():
    truth = generate(people=600, seed=7)
    return TestClient(create_app(truth)), truth


def emr_token(client) -> dict:
    r = client.post(
        "/emr/oauth2/token", data={"grant_type": "client_credentials", "client_id": "veromesh-connector", "client_secret": "harness-secret"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ------------------------------------------------------------------ ERP


def test_invoices_are_approved_within_authority(harness):
    client, truth = harness
    assert client.get(f"{FSCM}/invoices").status_code == 401
    r = client.get(f"{FSCM}/invoices?limit=500&totalResults=true&q=InvoiceAmount>25000;InvoiceAmount<=50000", headers=ERP).json()
    assert r["totalResults"] > 0 and r["hasMore"] in (True, False)
    clerks = {p.email.split("@")[0].upper() for p in truth.people.values() if any("APClerk" in h.roles for h in p.history)}
    big = {i["InvoiceId"] for i in r["items"] if i["InvoiceAmount"] > 25_000}
    hist = client.get(
        f"{FSCM}/invoiceApprovalsAndNotificationsHistory?limit=500&q=Action=Approved;ReviewedAmount>25000;ReviewedAmount<=50000",
        headers=ERP,
    ).json()["items"]
    approvers: dict[int, list[str]] = {}
    for h in hist:
        approvers.setdefault(h["InvoiceId"], []).append(h["Approver"])
    assert big <= set(approvers) and not any(a in clerks for i in big for a in approvers[i])
    assert sum(1 for i in big if len(approvers[i]) == 2) >= len(big) - 3  # a few missing co-approvals


def test_erp_quirks_and_user_roles(harness):
    client, _ = harness
    key = client.get("/harness/answer-key").json()
    kinds = {d["kind"] for d in key["defects"] if d["system"] == "erp"}
    assert {"approved-over-limit", "self-approved-po"} <= kinds
    accounts = client.get("/erp/hcmRestApi/resources/11.13.18.05/userAccounts?limit=500", headers=ERP).json()["items"]
    assert any(r["RoleName"] == "Accounts Payable Manager" for a in accounts for r in a["roles"])
    pos = client.get(f"{FSCM}/purchaseOrders?limit=5&q=Status=OPEN", headers=ERP).json()
    assert pos["items"] and all(p["Status"] == "OPEN" for p in pos["items"])


def test_the_controller_approves_while_the_ap_manager_post_is_vacant(harness):
    client, truth = harness
    client.post("/harness/clock", json={"days": 45})  # the AP Manager left on day 20
    managers = {p.email.split("@")[0].upper() for p in truth.people.values() if any("APManager" in h.roles for h in p.history)}
    start = (truth.start + __import__("datetime").timedelta(days=31)).isoformat()  # after the ERP end-dated her account
    r = client.get(
        f"{FSCM}/invoiceApprovalsAndNotificationsHistory?limit=500&q=Action=Approved;LastUpdateDate>={start};ReviewedAmount>5000;ReviewedAmount<=25000",
        headers=ERP,
    ).json()["items"]
    assert r and not any(h["Approver"] in managers for h in r)


# ------------------------------------------------------------------ EMR


def test_practitioners_roles_and_privileges(harness):
    client, truth = harness
    h = emr_token(client)
    assert client.get("/emr/fhir/Practitioner").status_code == 401
    assert client.get("/emr/fhir/metadata").json()["fhirVersion"] == "4.0.1"
    b = client.get("/emr/fhir/Practitioner?_count=20", headers=h).json()
    assert b["resourceType"] == "Bundle" and b["total"] > 20 and any(link["relation"] == "next" for link in b["link"])
    doc = next(p for p in truth.active_people() if "Prescriber" in p.at(truth.today).roles and p.npi)
    found = client.get(f"/emr/fhir/Practitioner?identifier=http://hl7.org/fhir/sid/us-npi|{doc.npi}", headers=h).json()
    if found["total"]:  # unless this one is missing its NPI
        prac = found["entry"][0]["resource"]
        roles = client.get(f"/emr/fhir/PractitionerRole?practitioner={prac['id']}", headers=h).json()["entry"][0]["resource"]
        codes = {c["code"] for cc in roles["code"] for c in cc["coding"]}
        assert "prescribe" in codes and "doctor" in codes


def test_medication_requests_name_real_prescribers(harness):
    client, truth = harness
    h = emr_token(client)
    meds = client.get("/emr/fhir/MedicationRequest?_count=50", headers=h).json()["entry"]
    by_id = {f"Practitioner/prac-{p.id}": p for p in truth.people.values()}
    assert meds and all(m["resource"]["requester"]["reference"] in by_id for m in meds)
    pat = meds[0]["resource"]["subject"]["reference"].split("/")[1]
    assert client.get(f"/emr/fhir/Encounter?subject={pat}", headers=h).json()["total"] >= 1


# ------------------------------------------------------------------ dbt


def test_dbt_artifacts_and_the_failing_model(harness):
    client, _ = harness
    m = client.get("/dbt/manifest.json").json()
    model = m["nodes"]["model.kestrel.finance_silver_supplier_invoices"]
    assert model["depends_on"]["nodes"] == ["source.kestrel.finance_bronze.erp_invoices"]
    assert model["columns"]["amount_usd"]["meta"]["classification"] == "confidential"
    statuses = {r["unique_id"]: r["status"] for r in client.get("/dbt/run_results.json").json()["results"]}
    assert set(statuses.values()) <= {"success", "pass"}
    client.post("/harness/clock", json={"days": 61})
    statuses = {r["unique_id"]: r["status"] for r in client.get("/dbt/run_results.json").json()["results"]}
    assert statuses["model.kestrel.finance_silver_supplier_invoices"] == "error"
    assert statuses["model.kestrel.finance_gold_on_time_payment"] == "skipped"
    assert statuses["model.kestrel.clinical_silver_encounters"] == "success"


# ------------------------------------------------------------------ scenarios


def test_scenarios_appear_as_the_story_unfolds(harness):
    client, _ = harness
    ids = lambda: [s["id"] for s in client.get("/harness/scenarios").json()["scenarios"]]  # noqa: E731
    first = ids()
    assert "agent-over-limit" in first and "breaking-change" not in first and "agent-principal-left" not in first
    client.post("/harness/clock", json={"days": 80})
    later = ids()
    assert {"breaking-change", "agent-principal-left", "reorganization", "vacant-owner"} <= set(later)


def test_the_agent_scenarios_follow_the_truth(harness):
    client, truth = harness
    sc = {s["id"]: s for s in client.get("/harness/scenarios").json()["scenarios"]}
    assert sc["agent-within-limit"]["expected"] == {"outcome": "allow"}
    over = sc["agent-over-limit"]["expected"]
    assert over["outcome"] == "consult" and over["consultRole"] == "APManager" and over["consultPeople"] == [truth.head_of("AP").id]
    client.post("/harness/clock", json={"days": 30})  # the post is vacant: it goes to a finance approver
    over = {s["id"]: s for s in client.get("/harness/scenarios").json()["scenarios"]}["agent-over-limit"]["expected"]
    assert over["consultRole"] == "FinanceApprover"
    client.post("/harness/clock", json={"days": 50})  # day 80: the principal left on day 75
    sc = {s["id"]: s for s in client.get("/harness/scenarios").json()["scenarios"]}
    assert sc["agent-within-limit"]["expected"]["outcome"] == "refuse"


def test_scoring(harness):
    client, _ = harness
    client.post("/harness/clock", json={"days": 61})
    sc = {s["id"]: s for s in client.get("/harness/scenarios").json()["scenarios"]}
    perfect = {"breaking-change": sc["breaking-change"]["expected"], "agent-within-limit": sc["agent-within-limit"]["expected"]}
    r = client.post("/harness/score", json=perfect).json()
    assert r["passed"] == 2 and r["of"] == len(sc)
    partial = {
        "breaking-change": {
            **sc["breaking-change"]["expected"],
            "affectedAssets": ["finance.silver.supplier_invoices", "clinical.silver.encounters"],
        }
    }
    res = next(x for x in client.post("/harness/score", json=partial).json()["results"] if x["id"] == "breaking-change")
    f = res["fields"]["affectedAssets"]
    assert not res["passed"] and f["precision"] == 0.5 and f["recall"] == 0.5
    assert f["missing"] == ["finance.gold.on_time_payment"] and f["extra"] == ["clinical.silver.encounters"]
