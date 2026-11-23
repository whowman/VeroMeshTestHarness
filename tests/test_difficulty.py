"""Difficulty: no quirks and no lag at 0 (every system agrees with the truth), more at 2,
and any one quirk set by its defect kind."""

from __future__ import annotations

from collections import Counter

from fastapi.testclient import TestClient

from harness import clock
from harness.app import answer_key, create_app
from harness.systems import REGISTRY
from harness.truth import generate

# Facts about the company, not quirks of a system: they stay at difficulty 0.
FACTS = {
    "contractor-only-here",
    "vacant-supervisory-org",
    "schema-drift",
    "model-failing",
    "document-names-leaver",
    "document-outdated",
    "stale-mapping",
}


def kinds(difficulty: dict, days: int = 70) -> Counter:
    t = generate(people=1500, seed=7, difficulty=difficulty)
    clock.advance(t, days)
    return Counter(d["kind"] for d in answer_key(t, {n: c(t) for n, c in REGISTRY.items()})["defects"])


def test_at_zero_only_facts_remain():
    assert set(kinds({"scale": 0})) <= FACTS


def test_at_zero_systems_agree_with_the_truth_at_once():
    t = generate(people=800, seed=7, difficulty={"scale": 0})
    client = TestClient(create_app(t))
    ap_head = t.head_of("AP")
    client.post("/harness/clock", json={"days": 21})  # she left on day 20
    import base64

    sn = {"Authorization": "Basic " + base64.b64encode(b"admin:harness").decode()}
    user = client.get(f"/servicenow/api/now/table/sys_user?sysparm_query=email={ap_head.email}", headers=sn).json()["result"][0]
    assert user["active"] == "false"


def test_a_higher_scale_makes_more():
    one, two = kinds({"scale": 1}), kinds({"scale": 2})
    assert two["name-variant"] > one["name-variant"] and two["missing-mail"] > one["missing-mail"]


def test_one_quirk_by_its_kind():
    k = kinds({"scale": 1, "overrides": {"name-variant": 0, "duplicate-ci": 0, "missing-mail": 0.5}})
    assert k["name-variant"] == 0 and k["duplicate-ci"] == 0 and k["missing-mail"] > kinds({"scale": 1})["missing-mail"]
    assert k["stale-manager"] > 0


def test_difficulty_can_change_while_running():
    client = TestClient(create_app(generate(people=600, seed=7)))
    client.post("/harness/clock", json={"days": 30})
    before = {d["kind"] for d in client.get("/harness/answer-key").json()["defects"]}
    assert client.put("/harness/difficulty", json={"scale": 0}).json() == {"scale": 0.0, "overrides": {}}
    after = {d["kind"] for d in client.get("/harness/answer-key").json()["defects"]}
    assert after <= FACTS < before | FACTS and client.get("/harness/status").json()["difficulty"]["scale"] == 0
