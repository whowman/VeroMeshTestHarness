"""HaloITSM, as its public REST API serves it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harness import clock
from harness.app import create_app
from harness.truth import generate


@pytest.fixture
def halo():
    truth = generate("healthcare", 300, 7)
    clock.advance(truth, 80)
    return truth, TestClient(create_app(truth))


def token(client):
    r = client.post(
        "/halo/auth/token",
        data={"grant_type": "client_credentials", "client_id": "veromesh-connector", "client_secret": "harness-secret", "scope": "all"},
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_sign_in_and_paging(halo):
    _, client = halo
    assert client.get("/halo/api/Users").status_code == 401
    assert (
        client.post("/halo/auth/token", data={"grant_type": "client_credentials", "client_id": "x", "client_secret": "y"}).status_code
        == 400
    )
    h = token(client)
    everyone = client.get("/halo/api/Users", headers=h).json()
    page = client.get("/halo/api/Users", params={"pageinate": "true", "page_size": 25, "page_no": 2}, headers=h).json()
    assert page["record_count"] == everyone["record_count"] > 25 and page["page_no"] == 2 and len(page["users"]) == 25
    one = page["users"][0]
    assert client.get(f"/halo/api/Users/{one['id']}", headers=h).json() == one
    assert {"id", "name", "emailaddress", "inactive"} <= set(one)


def test_assets_agents_and_their_quirks(halo):
    truth, client = halo
    h = token(client)
    apps = client.get("/halo/api/Asset", headers=h).json()["assets"]
    assert {a["key_field"] for a in apps} == {s.name for s in truth.systems.values()}
    assert all(a["assettype_name"] == "Business Application" for a in apps)
    agents = client.get("/halo/api/Agent", headers=h).json()["agents"]
    assert agents and {"id", "name", "email", "team", "isdisabled"} <= set(agents[0])
    key = client.get("/harness/answer-key").json()["defects"]
    kinds = {d["kind"] for d in key if d["system"] == "halo"}
    assert "asset-owner-free-text" in kinds
