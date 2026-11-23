"""The harness: a reproducible company, a clock that tells the same story every time, and
systems that serve it the way their real APIs do, with every quirk in the answer key."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from harness import clock
from harness.app import create_app
from harness.truth import generate

TENANT = "kestrelvalley.example"


@pytest.fixture
def harness():
    truth = generate(people=600, seed=7)
    return TestClient(create_app(truth)), truth


def graph_token(client) -> dict:
    r = client.post(
        f"/entra/{TENANT}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "veromesh-connector",
            "client_secret": "harness-secret",
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def all_pages(client, url, headers) -> tuple[list[dict], dict]:
    out, last = [], {}
    while url:
        last = client.get(url, headers=headers).json()
        out += last["value"]
        url = last.get("@odata.nextLink")
    return out, last


SN = {"Authorization": "Basic " + base64.b64encode(b"admin:harness").decode()}


# ------------------------------------------------------------------ the company


def test_the_same_seed_gives_the_same_company_and_story():
    a, b = generate(people=600, seed=7), generate(people=600, seed=7)
    assert [p.email for p in a.people.values()] == [p.email for p in b.people.values()]
    assert clock.advance(a, 90) == clock.advance(b, 90)
    assert generate(people=600, seed=8).people["p00010"].email != a.people["p00010"].email


def test_every_unit_has_a_head_and_everyone_else_a_manager():
    t = generate(people=600, seed=7)
    for u in t.units:
        assert t.head_of(u) is not None
    for p in t.active_people():
        pos = p.at(t.today)
        assert pos.manager or (pos.head and t.units[pos.unit].parent is None)


def test_the_script_plays_out():
    t = generate(people=600, seed=7)
    ap_head = t.head_of("AP")
    done = clock.advance(t, 70)
    left = next(e for e in done if e["kind"] == "leave" and e["person"] == ap_head.id)
    assert {"kpi": "OnTimePaymentRate", "field": "owner"} in left["vacated"]
    hired = next(e for e in done if e.get("head") and e["unit"] == "AP")
    assert t.kpis["OnTimePaymentRate"].owner == hired["person"] != ap_head.id
    assert t.units["CINF"].parent == "DATA"
    change = next(e for e in done if e["kind"] == "schema-change")
    assert "invoice_amount" in [c[0] for c in t.assets[change["asset"]].columns]
    assert change["downstream"] == ["finance.silver.supplier_invoices"]


# ------------------------------------------------------------------ Entra ID


def test_graph_needs_a_token(harness):
    client, _ = harness
    assert client.get("/entra/v1.0/users").status_code == 401
    r = client.post(
        f"/entra/{TENANT}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": "veromesh-connector", "client_secret": "wrong"},
    )
    assert r.status_code == 401


def test_users_page_and_match_the_company(harness):
    client, truth = harness
    h = graph_token(client)
    users, _ = all_pages(client, "/entra/v1.0/users?$top=100&$select=displayName,mail,employeeId,accountEnabled", h)
    active = truth.active_people(truth.today)
    assert len(users) >= len(active) - 10  # yesterday's sync: today's hires are not in yet
    assert set(users[0]) <= {"id", "displayName", "mail", "employeeId", "accountEnabled"}
    one = next(p for p in active if p.worker_type == "employee")
    found = client.get(f"/entra/v1.0/users?$filter=employeeId eq '{one.employee_id}'", headers=h).json()["value"]
    assert len(found) == 1 and found[0]["surname"] == one.family


def test_a_manager_and_groups(harness):
    client, truth = harness
    h = graph_token(client)
    p = next(p for p in truth.active_people() if not p.at(truth.today).head)
    m = client.get(f"/entra/v1.0/users/{p.email}/manager", headers=h).json()
    assert m["userPrincipalName"] == truth.people[p.at(truth.today).manager].email
    groups = client.get("/entra/v1.0/groups?$filter=displayName eq 'SG-APManager'", headers=h).json()["value"]
    members, _ = all_pages(client, f"/entra/v1.0/groups/{groups[0]['id']}/members", h)
    assert {u["userPrincipalName"] for u in members} == {
        q.email for q in truth.active_people(truth.today) if "APManager" in q.at(truth.today).roles
    }


def test_agents_are_service_principals_owned_by_their_principal(harness):
    client, truth = harness
    h = graph_token(client)
    sps = client.get("/entra/v1.0/servicePrincipals", headers=h).json()["value"]
    copilot = next(s for s in sps if s["displayName"] == "Clinician Copilot")
    assert copilot["appId"] == truth.agents["ClinicianCopilot"].client_id
    owners = client.get(f"/entra/v1.0/servicePrincipals/{copilot['id']}/owners", headers=h).json()["value"]
    assert [o["userPrincipalName"] for o in owners] == [truth.people[truth.agents["ClinicianCopilot"].acts_for].email]


def test_delta_returns_only_what_changed(harness):
    client, _ = harness
    h = graph_token(client)
    everyone, last = all_pages(client, "/entra/v1.0/users/delta", h)
    delta = last["@odata.deltaLink"].split("/entra", 1)[1]
    client.post("/harness/clock", json={"days": 10})
    changed, _ = all_pages(client, "/entra" + delta, h)
    assert 0 < len(changed) < len(everyone) / 5


def test_entra_quirks_are_in_the_answer_key(harness):
    client, _ = harness
    client.post("/harness/clock", json={"days": 60})
    kinds = {d["kind"] for d in client.get("/harness/answer-key").json()["defects"] if d["system"] == "entra"}
    assert {"name-variant", "contractor-only-here", "leaver-account-enabled", "stale-manager"} <= kinds


# ------------------------------------------------------------------ ServiceNow


def test_the_table_api_needs_sign_in(harness):
    client, _ = harness
    assert client.get("/servicenow/api/now/table/sys_user").status_code == 401
    tok = client.post("/servicenow/oauth_token.do", data={"grant_type": "password", "username": "admin", "password": "harness"}).json()
    assert (
        client.get(
            "/servicenow/api/now/table/sys_user?sysparm_limit=1", headers={"Authorization": f"Bearer {tok['access_token']}"}
        ).status_code
        == 200
    )


def test_cmdb_applications_with_owners_by_reference(harness):
    client, truth = harness
    r = client.get("/servicenow/api/now/table/cmdb_ci_business_app?sysparm_query=name=Oracle Fusion ERP", headers=SN)
    erp = r.json()["result"]
    assert len(erp) == 1
    owner = client.get(f"/servicenow/api/now/table/sys_user/{erp[0]['owned_by']['value']}", headers=SN).json()["result"]
    assert owner["email"] == truth.people[truth.systems["ERP"].business_owner].email
    shown = client.get(
        "/servicenow/api/now/table/cmdb_ci_business_app?sysparm_query=name=Oracle Fusion ERP&sysparm_display_value=true&sysparm_fields=name,owned_by",
        headers=SN,
    ).json()["result"][0]
    assert shown == {"name": "Oracle Fusion ERP", "owned_by": truth.people[truth.systems["ERP"].business_owner].name}


def test_encoded_queries_paging_and_count(harness):
    client, _ = harness
    r = client.get(
        "/servicenow/api/now/table/sys_user?sysparm_query=active=true^titleSTARTSWITHRegistered^ORDERBYlast_name&sysparm_limit=5",
        headers=SN,
    )
    assert r.status_code == 200 and len(r.json()["result"]) == 5
    assert int(r.headers["X-Total-Count"]) > 5 and 'rel="next"' in r.headers["Link"]
    names = [u["last_name"] for u in r.json()["result"]]
    assert names == sorted(names) and all(u["title"].startswith("Registered") for u in r.json()["result"])
    rel = client.get("/servicenow/api/now/table/cmdb_rel_ci?sysparm_limit=1", headers=SN).json()["result"][0]
    assert rel["type"]["value"] == "Runs on::Runs"


def test_servicenow_quirks_and_the_clock(harness):
    client, truth = harness
    ap_head = truth.head_of("AP")

    def active() -> str:
        url = f"/servicenow/api/now/table/sys_user?sysparm_query=email={ap_head.email}"
        return client.get(url, headers=SN).json()["result"][0]["active"]

    client.post("/harness/clock", json={"days": 25})  # the AP Manager left on day 20
    assert active() == "true"  # the weekly HR sync has not run since
    key = client.get("/harness/answer-key").json()
    kinds = {d["kind"] for d in key["defects"] if d["system"] == "servicenow"}
    assert {"no-technical-owner", "duplicate-ci", "stale-user", "leaver-still-active"} <= kinds
    client.post("/harness/clock", json={"days": 3})
    assert active() == "false"
    sem = client.get("/servicenow/api/now/table/cmdb_ci_business_app?sysparm_query=name=Stardog Knowledge Graph", headers=SN).json()[
        "result"
    ][0]
    assert sem["managed_by"] == ""
    client.post("/harness/clock", json={"days": 37})  # day 65: the schema change happened on day 60
    changes = client.get("/servicenow/api/now/table/change_request?sysparm_query=short_descriptionLIKEinvoice_amount", headers=SN).json()[
        "result"
    ]
    assert len(changes) == 1


def test_status(harness):
    client, _ = harness
    s = client.get("/harness/status").json()
    assert s["organization"] == "Kestrel Valley Health" and set(s["systems"]) == {
        "databricks",
        "dbt",
        "emr",
        "entra",
        "erp",
        "servicenow",
        "sharepoint",
        "workday",
    }
