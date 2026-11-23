"""The Clinician Copilot slice (its scenarios and the semantic layer) and the systems
misbehaving on purpose."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from harness.app import create_app
from harness.truth import generate

SD = {"Authorization": "Basic " + base64.b64encode(b"admin:harness").decode(), "Accept": "application/sparql-results+json"}
PREFIX = "PREFIX sem: <https://kestrelvalley.example/semantic#> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> PREFIX skos: <http://www.w3.org/2004/02/skos/core#> "


@pytest.fixture
def harness():
    truth = generate(people=2000, seed=7)
    return TestClient(create_app(truth)), truth


def copilot(client) -> dict:
    return {s["id"]: s for s in client.get("/harness/scenarios").json()["scenarios"] if s.get("slice") == "clinician-copilot"}


# ------------------------------------------------------------------ the Copilot scenarios


def test_eleven_copilot_scenarios_and_the_semantic_one(harness):
    client, _ = harness
    sc = copilot(client)
    assert len([s for s in sc if s.startswith("copilot-")]) == 11 and "semantic-context" in sc
    assert all(s["ask"]["agent"] == "ClinicianCopilot" for k, s in sc.items() if k.startswith("copilot-"))


def test_care_relationship_signing_and_review(harness):
    client, truth = harness
    sc = copilot(client)
    doctor = sc["copilot-history-treating"]["ask"]["requester"]
    assert "Prescriber" in truth.people[doctor].at(truth.today).roles
    assert sc["copilot-history-not-treating"]["expected"] == {"outcome": "refuse", "reason": "no-care-relationship"}
    order = sc["copilot-draft-order"]["expected"]
    assert order["outcome"] == "draft" and order["signers"] == [doctor] and order["reviewRoles"] == ["Pharmacist"]
    nurse = sc["copilot-draft-order-nurse"]
    assert nurse["expected"]["requesterMaySign"] is False and nurse["ask"]["requester"] not in nurse["expected"]["signers"]
    assert all("Prescriber" in truth.people[p].at(truth.today).roles for p in nurse["expected"]["signers"])
    assert sc["copilot-high-alert"]["expected"]["independentChecks"] == 2


def test_the_interaction_is_in_the_emr(harness):
    client, _ = harness
    sc = copilot(client)["copilot-interaction"]
    assert sc["expected"]["outcome"] == "consult" and ["warfarin 5 mg", "aspirin 81 mg"] in sc["expected"]["interactions"]
    tok = client.post(
        "/emr/oauth2/token", data={"grant_type": "client_credentials", "client_id": "veromesh-connector", "client_secret": "harness-secret"}
    ).json()["access_token"]
    meds = client.get(f"/emr/fhir/MedicationRequest?subject={sc['ask']['patient']}", headers={"Authorization": f"Bearer {tok}"}).json()[
        "entry"
    ]
    assert {"warfarin 5 mg", "aspirin 81 mg"} <= {m["resource"]["medicationCodeableConcept"]["text"] for m in meds}


def test_prior_authorization_thresholds(harness):
    client, truth = harness
    sc = copilot(client)
    reviewers = sorted(p.id for p in truth.active_people() if "UMReviewer" in p.at(truth.today).roles)
    assert sc["copilot-prior-auth-within"]["expected"]["consultPeople"] == reviewers
    director = [truth.head_of("UM").id]
    assert sc["copilot-prior-auth-above"]["expected"]["consultPeople"] == director
    assert sc["copilot-prior-auth-deny"]["expected"]["consultPeople"] == director


def test_a_vacant_role_goes_to_the_owning_units_head(harness):
    client, truth = harness
    for p in truth.active_people():  # everyone in Utilization Management leaves but its head
        if p.at(truth.today).unit == "UM" and not p.at(truth.today).head:
            p.left = truth.today
    for s in client.app.state.systems.values():
        s._cache = None
    sc = copilot(client)
    assert sc["copilot-prior-auth-within"]["expected"]["consultPeople"] == [truth.head_of("UM").id]


# ------------------------------------------------------------------ the semantic layer


def test_sparql_over_the_semantic_layer(harness):
    client, _ = harness
    assert client.get("/stardog/admin/databases").status_code == 401
    assert client.get("/stardog/admin/databases", headers=SD).json() == {"databases": ["semantic"]}
    q = PREFIX + "SELECT ?label ?def WHERE { ?t rdfs:label ?label ; skos:definition ?def ; sem:domain 'Claims/Coverage' } ORDER BY ?label"
    rows = client.post("/stardog/semantic/query", data={"query": q}, headers=SD).json()["results"]["bindings"]
    assert [r["label"]["value"] for r in rows] == ["Claim", "Coverage", "PriorAuthorization"]
    got = client.post("/stardog/semantic/query", content=q, headers={**SD, "Content-Type": "application/sparql-query"})
    assert got.status_code == 200 and client.get("/stardog/semantic/query", params={"query": q}, headers=SD).status_code == 200
    assert client.post("/stardog/semantic/query", data={"query": "DELETE WHERE { ?s ?p ?o }"}, headers=SD).status_code == 400


def test_the_semantic_context_and_the_breaking_change(harness):
    client, truth = harness
    sem = copilot(client)["semantic-context"]["expected"]
    assert sem["unmapped"] == ["CareGap"] and sem["staleMappings"] == []
    assert sem["owners"]["Claim"] == [truth.assets["claims.silver.claims"].owner]
    client.post("/harness/clock", json={"days": 61})
    sc = {s["id"]: s for s in client.get("/harness/scenarios").json()["scenarios"]}
    assert sc["semantic-context"]["expected"]["staleMappings"] == ["finance.bronze.erp_invoices.amount"]
    assert sc["breaking-change"]["expected"]["staleSemanticMappings"] == ["finance.bronze.erp_invoices.amount"]


# ------------------------------------------------------------------ chaos


def test_chaos_is_off_by_default_and_repeatable(harness):
    client, _ = harness
    sn = {"Authorization": "Basic " + base64.b64encode(b"admin:harness").decode()}
    url = "/servicenow/api/now/table/sys_user?sysparm_limit=1"
    assert all(client.get(url, headers=sn).status_code == 200 for _ in range(20))
    assert (
        client.put("/harness/chaos", json={"rate": 0.3, "kinds": ["throttle", "server-error", "unavailable", "token-expired"]}).status_code
        == 200
    )
    first = [client.get(url, headers=sn).status_code for _ in range(60)]
    assert {429, 500, 503, 401} <= set(first) and first.count(200) > 30
    client.put("/harness/chaos", json={"rate": 0.3, "kinds": ["throttle", "server-error", "unavailable", "token-expired"]})
    assert [client.get(url, headers=sn).status_code for _ in range(60)] == first  # the same seed fails the same requests
    seen = client.get("/harness/chaos").json()["seen"]
    assert seen["requests"] == 60 and seen["throttle"] > 0


def test_chaos_responses_look_like_the_real_ones(harness):
    client, _ = harness
    client.put("/harness/chaos", json={"rate": 1.0, "kinds": ["throttle"]})
    r = client.get("/databricks/api/2.1/unity-catalog/catalogs", headers={"Authorization": "Bearer dapi-harness-token"})
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"
    client.put("/harness/chaos", json={"rate": 1.0, "kinds": ["token-expired"]})
    r = client.get("/workday/ccx/api/v1/kestrelvalley/workers")
    assert r.status_code == 401 and "invalid_token" in r.headers["WWW-Authenticate"]
    assert client.get("/harness/status").status_code == 200  # the harness's own controls always answer
    assert client.put("/harness/chaos", json={"rate": 0.1, "kinds": ["meteor"]}).status_code == 422


def test_slow_responses(harness):
    import time

    client, _ = harness
    client.put("/harness/chaos", json={"rate": 1.0, "kinds": ["slow"], "slow_ms": 200})
    t0 = time.perf_counter()
    r = client.get("/dbt/manifest.json")
    assert r.status_code == 200 and time.perf_counter() - t0 >= 0.2


# ------------------------------------------------------------------ closer to the public APIs


def test_oracle_approvals_are_their_own_resource():
    truth = generate(people=600, seed=7)
    client = TestClient(create_app(truth))
    erp = {"Authorization": "Basic " + base64.b64encode(b"integration.user:harness").decode()}
    base = "/erp/fscmRestApi/resources/11.13.18.05"
    inv = client.get(f"{base}/invoices?limit=1", headers=erp).json()["items"][0]
    assert "ApproverPersonNumber" not in inv
    hist = client.get(f"{base}/invoiceApprovalsAndNotificationsHistory?q=InvoiceId={inv['InvoiceId']}", headers=erp).json()["items"]
    assert [h["Action"] for h in hist][:2] == ["Initiated", "Approved"] and hist[1]["WorkflowType"] == "Invoice approval"
    usernames = {p.email.split("@")[0].upper() for p in truth.people.values()}
    assert all(h["Approver"] in usernames for h in hist if h["Action"] == "Approved")


def test_lineage_from_the_system_table(harness):
    client, truth = harness
    r = client.post(
        "/databricks/api/2.0/sql/statements",
        headers={"Authorization": "Bearer dapi-harness-token"},
        json={"statement": "SELECT * FROM system.access.table_lineage"},
    ).json()
    cols = [c["name"] for c in r["manifest"]["schema"]["columns"]]
    rows = {(row[cols.index("source_table_full_name")], row[cols.index("target_table_full_name")]) for row in r["result"]["data_array"]}
    assert rows == set(truth.lineage)


def test_sharepoint_content_redirects_to_a_download_url(harness):
    client, _ = harness
    tok = client.post(
        "/entra/kestrelvalley.example/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": "veromesh-connector", "client_secret": "harness-secret"},
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    site = client.get("/sharepoint/v1.0/sites?search=Finance", headers=h).json()["value"][0]
    drive = client.get(f"/sharepoint/v1.0/sites/{site['id']}/drive", headers=h).json()["id"]
    folder = client.get(f"/sharepoint/v1.0/drives/{drive}/root/children", headers=h).json()["value"][0]
    item = client.get(f"/sharepoint/v1.0/drives/{drive}/items/{folder['id']}/children", headers=h).json()["value"][0]
    r = client.get(f"/sharepoint/v1.0/drives/{drive}/items/{item['id']}/content", headers=h, follow_redirects=False)
    assert r.status_code == 302 and "/sharepoint/download/" in r.headers["Location"]
    got = client.get(r.headers["Location"])  # no token, as with Graph's pre-authenticated URL
    assert got.status_code == 200 and got.text.startswith("# ")


def test_entra_delta_reports_deleted_users():
    client = TestClient(create_app(generate(people=1500, seed=7)))
    tok = client.post(
        "/entra/kestrelvalley.example/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": "veromesh-connector", "client_secret": "harness-secret"},
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    url = "/entra/v1.0/users/delta"
    while True:
        page = client.get(url, headers=h).json()
        if "@odata.deltaLink" in page:
            break
        url = page["@odata.nextLink"].split("testserver", 1)[1]
    delta = page["@odata.deltaLink"].split("testserver", 1)[1]
    client.post("/harness/clock", json={"days": 60})  # leavers in the first weeks are deleted after 30 days
    removed = []
    url = delta
    while url:
        page = client.get(url, headers=h).json()
        removed += [u for u in page["value"] if "@removed" in u]
        url = page.get("@odata.nextLink", "").split("testserver", 1)[-1] if page.get("@odata.nextLink") else None
    assert removed and all(u["@removed"] == {"reason": "changed"} for u in removed)
