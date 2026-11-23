"""Workday (REST and Reports as a Service) and Databricks (Unity Catalog, lineage, SQL),
served from the same truth, with their quirks in the answer key."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harness.app import create_app
from harness.truth import generate

TENANT = "kestrelvalley"
WD = f"/workday/ccx/api/v1/{TENANT}"
RAAS = f"/workday/ccx/service/customreport2/{TENANT}/ISU_VeroMesh"
DB = {"Authorization": "Bearer dapi-harness-token"}


@pytest.fixture
def harness():
    truth = generate(people=600, seed=7)
    return TestClient(create_app(truth)), truth


def wd_token(client) -> dict:
    r = client.post(
        f"/workday/ccx/oauth2/{TENANT}/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "veromesh-connector",
            "client_secret": "harness-secret",
            "refresh_token": "harness-workday-refresh",
        },
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ------------------------------------------------------------------ Workday


def test_workday_holds_employees_not_contractors(harness):
    client, truth = harness
    h = wd_token(client)
    assert client.get(f"{WD}/workers", headers={}).status_code == 401
    seen, offset = [], 0
    while True:
        page = client.get(f"{WD}/workers?limit=100&offset={offset}", headers=h).json()
        seen += page["data"]
        offset += 100
        if offset >= page["total"]:
            break
    ids = {w["workerId"] for w in seen}
    assert ids == {p.employee_id for p in truth.people.values() if p.worker_type == "employee"}
    assert not any(i.startswith("C") for i in ids)


def test_a_worker_has_manager_and_supervisory_org(harness):
    client, truth = harness
    h = wd_token(client)
    p = next(p for p in truth.active_people() if p.worker_type == "employee" and not p.at(truth.today).head)
    w = client.get(f"{WD}/workers/{p.employee_id}", headers=h).json()
    pos = p.at(truth.today)
    assert w["manager"]["workerId"] == truth.people[pos.manager].employee_id
    assert w["primarySupervisoryOrganization"]["descriptor"] == truth.units[pos.unit].name
    orgs = client.get(f"{WD}/supervisoryOrganizations?limit=100", headers=h).json()["data"]
    ap = next(o for o in orgs if o["code"] == "AP")
    assert ap["manager"]["descriptor"] == truth.head_of("AP").name


def test_the_workers_report_and_changes(harness):
    client, truth = harness
    h = wd_token(client)
    rows = client.get(f"{RAAS}/VeroMesh_Workers?format=json", headers=h).json()["Report_Entry"]
    assert {"Employee_ID", "Legal_Name", "Preferred_Name", "Manager_Employee_ID", "Termination_Date"} <= set(rows[0])
    start = truth.today.isoformat()
    client.post("/harness/clock", json={"days": 30})
    changes = client.get(f"{RAAS}/VeroMesh_Worker_Changes?format=json&Updated_From={start}", headers=h).json()["Report_Entry"]
    kinds = {c["Business_Process"] for c in changes}
    assert {"Hire", "Termination"} <= kinds and all(c["Entered_On"] >= start for c in changes)


def test_the_ap_manager_post_is_vacant_in_workday(harness):
    client, _ = harness
    h = wd_token(client)
    client.post("/harness/clock", json={"days": 30})  # left on day 20, replaced on day 50
    orgs = client.get(f"{WD}/supervisoryOrganizations?limit=100", headers=h).json()["data"]
    assert next(o for o in orgs if o["code"] == "AP")["manager"] is None
    kinds = {d["kind"] for d in client.get("/harness/answer-key").json()["defects"] if d["system"] == "workday"}
    assert {"vacant-supervisory-org", "legal-and-preferred-name"} <= kinds
    client.post("/harness/clock", json={"days": 25})
    orgs = client.get(f"{WD}/supervisoryOrganizations?limit=100", headers=h).json()["data"]
    assert next(o for o in orgs if o["code"] == "AP")["manager"] is not None


def test_a_retro_termination_shows_active_until_entered():
    truth = generate(people=2000, seed=7, difficulty={"scale": 1, "overrides": {"retro-termination": 1.0}})
    client = TestClient(create_app(truth))
    client.post("/harness/clock", json={"days": 90})
    retro = [d for d in client.get("/harness/answer-key").json()["defects"] if d["kind"] == "retro-termination"]
    assert retro, "some recent leavers are entered late"
    h = wd_token(client)
    pid = retro[0]["about"]["person"]
    w = client.get(f"{WD}/workers/{truth.people[pid].employee_id}", headers=h).json()
    assert w["active"] is True and w["terminationDate"] is None


# ------------------------------------------------------------------ Databricks


def test_unity_catalog_tables_with_columns_and_owners(harness):
    client, truth = harness
    assert client.get("/databricks/api/2.1/unity-catalog/catalogs").status_code == 401
    cats = [c["name"] for c in client.get("/databricks/api/2.1/unity-catalog/catalogs", headers=DB).json()["catalogs"]]
    assert cats == ["claims", "clinical", "finance", "people"]
    schemas = [
        s["name"] for s in client.get("/databricks/api/2.1/unity-catalog/schemas?catalog_name=finance", headers=DB).json()["schemas"]
    ]
    assert schemas == ["bronze", "gold", "silver"]
    silver = client.get("/databricks/api/2.1/unity-catalog/tables?catalog_name=finance&schema_name=silver", headers=DB).json()["tables"]
    t = silver[0]
    assert t["full_name"] == "finance.silver.supplier_invoices" and t["owner"] == truth.people[truth.assets[t["full_name"]].owner].email
    assert t["properties"]["steward"] and {c["name"] for c in t["columns"]} >= {"invoice_id", "amount_usd"}
    bronze = client.get("/databricks/api/2.1/unity-catalog/tables/finance.bronze.erp_invoices", headers=DB).json()
    assert bronze["owner"] == "data-engineering"


def test_lineage_and_tags(harness):
    client, _ = harness
    lin = client.get("/databricks/api/2.0/lineage-tracking/table-lineage?table_name=clinical.gold.longitudinal_patient", headers=DB).json()
    assert {u["tableInfo"]["name"] for u in lin["upstreams"]} == {"encounters", "medication_orders", "claims"} and lin["downstreams"] == []
    r = client.post(
        "/databricks/api/2.0/sql/statements",
        headers=DB,
        json={"statement": "SELECT * FROM system.information_schema.column_tags", "warehouse_id": "w1"},
    ).json()
    tags = {(row[0], row[1], row[2], row[3]): row[5] for row in r["result"]["data_array"]}
    assert tags[("clinical", "bronze", "ehr_encounters", "patient_mrn")] == "phi"
    assert ("clinical", "silver", "encounters", "admit_date") not in tags  # the untagged PHI column


def test_rows_are_simulated_records_consistent_with_the_truth(harness):
    client, truth = harness
    r = client.post(
        "/databricks/api/2.0/sql/statements", headers=DB, json={"statement": "SELECT * FROM people.silver.workforce LIMIT 20"}
    ).json()
    cols = [c["name"] for c in r["manifest"]["schema"]["columns"]]
    rows = [dict(zip(cols, row, strict=True)) for row in r["result"]["data_array"]]
    ids = {p.employee_id: p for p in truth.people.values()}
    assert len(rows) == 20 and all(
        row["person_id"] in ids and row["job_title"] == ids[row["person_id"]].at(truth.today).title for row in rows
    )
    r = client.post(
        "/databricks/api/2.0/sql/statements", headers=DB, json={"statement": "SELECT * FROM clinical.silver.medication_orders LIMIT 5"}
    ).json()
    npis = {p.employee_id for p in truth.people.values() if p.npi}
    prescriber = [c["name"] for c in r["manifest"]["schema"]["columns"]].index("prescriber_id")
    assert all(row[prescriber] in npis for row in r["result"]["data_array"])
    assert client.post("/databricks/api/2.0/sql/statements", headers=DB, json={"statement": "DROP TABLE x"}).status_code == 400


def test_the_breaking_change_shows_in_the_lakehouse(harness):
    client, _ = harness
    client.post("/harness/clock", json={"days": 65})
    cols = [
        c["name"] for c in client.get("/databricks/api/2.1/unity-catalog/tables/finance.bronze.erp_invoices", headers=DB).json()["columns"]
    ]
    assert "invoice_amount" in cols and "amount" not in cols
    drift = [d for d in client.get("/harness/answer-key").json()["defects"] if d["kind"] == "schema-drift"]
    assert drift and drift[0]["about"]["downstream"] == "finance.silver.supplier_invoices"
    kinds = {d["kind"] for d in client.get("/harness/answer-key").json()["defects"] if d["system"] == "databricks"}
    assert {"owner-is-group", "untagged-sensitive-column"} <= kinds
