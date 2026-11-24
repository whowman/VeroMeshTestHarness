"""The scenario runner, against a fake VeroMesh API and the real harness."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from harness import cli
from harness.app import create_app
from harness.runner import VeroMesh, run
from harness.scenarios import aliases, score
from harness.truth import generate

logging.disable(logging.INFO)


@pytest.fixture
def company():
    truth = generate("healthcare", 300, 7)
    return truth, TestClient(create_app(truth))


def fake_veromesh(truth, *, people=None, decision=None):
    """VeroMesh answering from the truth, in its own IDs (employee IDs; sign-in names for contractors)."""
    working = truth.active_people()

    def vid(p):
        return p.employee_id if p.worker_type == "employee" else p.email.split("@")[0]

    rows = (
        people
        if people is not None
        else [{"id": vid(p), "name": p.name, **({"employeeId": p.employee_id} if p.worker_type == "employee" else {})} for p in working]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/people":
            return httpx.Response(200, json=rows)
        if path == "/conflicts":
            return httpx.Response(200, json=[])
        if path == "/sources":
            return httpx.Response(200, json=[{"source": "workday"}, {"source": "entra"}])
        if path == "/decisions/check":
            body = json.loads(request.content)
            if decision is None:
                return httpx.Response(404, json={"detail": f"No action '{body['action']}' in the model."})
            return httpx.Response(201, json=decision(body))
        return httpx.Response(404, json={"detail": "not here"})

    return VeroMesh(httpx.Client(transport=httpx.MockTransport(handle)), "http://vm")


def test_the_scorer_accepts_the_systems_identifiers(company):
    truth, h = company
    names = aliases(truth)
    p = truth.active_people()[0]
    for alias in (p.id, p.employee_id, p.email, p.email.split("@")[0].upper()):
        assert names[alias.lower()] == p.id
    sc = [{"id": "x", "gate": 1, "expected": {"people": [p.id], "outcome": "allow"}}]
    got = score(sc, {"x": {"people": [p.employee_id], "outcome": "allow"}}, names)
    assert got["passed"] == 1


def test_the_runner_answers_what_it_can_and_names_what_is_missing(company):
    truth, h = company
    report = run(fake_veromesh(truth), h)
    results = {r["id"]: r for r in report.score["results"]}
    ident = results["identity-resolution"]["fields"]
    assert ident["people"]["passed"] and ident["noHrRecord"]["passed"]  # employee IDs and sign-in names, translated
    assert "identity-resolution" in report.partial
    assert report.not_yet["agent-within-limit"] == "No action 'ApproveSupplierInvoice' in the model."
    assert "ServiceNow" in report.not_yet["system-ownership"]
    assert "patient or requester" in report.not_yet["copilot-draft-order"]
    assert report.errors == {}
    lines = report.lines()
    assert lines[0].startswith("Scenarios on ") and any(line.startswith("Gate 2:") for line in lines)


def test_decisions_are_put_to_the_check_and_scored(company):
    truth, h = company
    manager = next(p for p in truth.active_people() if "APManager" in p.at(truth.today).roles)

    def decide(body):
        if body["amount"] <= 5000:
            return {"outcome": "allow", "consult": []}
        return {
            "outcome": "consult",
            "consult": [
                {"role": "APManager", "people": [{"id": manager.employee_id, "name": manager.name}]},
                {"role": "Controller", "people": [{"id": "someone-else", "name": "Someone Else"}]},
            ],
        }

    report = run(fake_veromesh(truth, decision=decide), h)
    results = {r["id"]: r for r in report.score["results"]}
    assert results["agent-within-limit"]["passed"]
    assert results["agent-over-limit"]["passed"]


def test_an_empty_veromesh_is_not_yet_rather_than_wrong(company):
    truth, h = company
    report = run(fake_veromesh(truth, people=[]), h)
    assert "load Workday and Entra ID first" in report.not_yet["identity-resolution"]


def test_the_command_writes_a_report(company, tmp_path, capsys):
    truth, h = company
    vm = fake_veromesh(truth)
    out = tmp_path / "report.json"
    args = cli.argparse.Namespace(veromesh="http://vm", token=None, harness="", out=str(out))
    assert cli.run_scenarios(args, veromesh_http=vm.http, harness_http=h) == 0
    saved = json.loads(out.read_text())
    assert set(saved) == {"today", "answers", "notYet", "partial", "errors", "score"}
    assert "Gate 1:" in capsys.readouterr().out


def test_without_the_model_the_agent_acts_on_permission(company):
    truth, h = company
    report = run(None, h, without_model=True)
    assert report.answers["agent-within-limit"] == {"outcome": "allow"}
    assert report.answers["agent-over-limit"] == {"outcome": "allow"}  # wrong: it should consult
    assert "no organizational context" in report.not_yet["identity-resolution"]
    passed = {r["id"] for r in report.score["results"] if r["passed"]}
    assert "agent-within-limit" in passed and "agent-over-limit" not in passed


def test_the_tier1_measures(company, tmp_path, capsys):
    truth, h = company
    vm = fake_veromesh(truth, decision=lambda body: {"outcome": "allow"})
    out = tmp_path / "measures.json"
    args = cli.argparse.Namespace(veromesh="http://vm", token=None, harness="", out=str(out))
    assert cli.run_measures(args, veromesh_http=vm.http, harness_http=h) == 0
    got = json.loads(out.read_text())
    assert got["M1"]["withModel"]["of"] == got["M1"]["withoutModel"]["of"] > 0
    assert got["M5"]["scenarios"] and "M2" not in got  # the fake has no model measures
    assert "M1 context question accuracy" in capsys.readouterr().out
