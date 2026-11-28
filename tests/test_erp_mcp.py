"""The ERP's MCP server (what VeroMesh's gateway sits in front of) and the gateway scenarios,
run against a fake gateway that behaves as VeroMesh's: allowed calls go through, calls
needing an owner wait for the approval, and tools it does not offer never arrive."""

from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.app import create_app
from harness.runner import VeroMesh, gateway_allowed, gateway_held, gateway_not_offered
from harness.scenarios import score
from harness.systems.erp_mcp import TOKEN
from harness.truth import generate

logging.disable(logging.INFO)
MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def harness():
    truth = generate("healthcare", 300, 7)
    with TestClient(create_app(truth)) as h:  # runs the MCP server's session manager
        yield truth, h


def rpc(h, method, params=None, headers=MCP):
    return h.post("/erp/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_the_erp_mcp_server_offers_its_tools_and_records_every_call(harness):
    _, h = harness
    tools = {t["name"]: t for t in rpc(h, "tools/list").json()["result"]["tools"]}
    assert set(tools) == {"get_invoice", "approve_invoice", "change_supplier_bank"}
    assert set(tools["approve_invoice"]["inputSchema"]["properties"]) == {"invoice_number", "amount", "currency"}
    inv = h.get("/erp/fscmRestApi/resources/11.13.18.05/invoices", params={"limit": 1}, auth=("integration.user", "harness")).json()
    number = inv["items"][0]["InvoiceNumber"]
    got = rpc(h, "tools/call", {"name": "get_invoice", "arguments": {"invoice_number": number}}).json()["result"]
    assert not got.get("isError") and number in got["content"][0]["text"]
    rpc(h, "tools/call", {"name": "approve_invoice", "arguments": {"invoice_number": "INV-X", "amount": 800}})
    calls = h.get("/harness/received").json()["calls"]
    assert [(c["tool"], c["seq"]) for c in calls] == [("get_invoice", 1), ("approve_invoice", 2)]
    assert h.get("/harness/received", params={"after": 1}).json()["calls"][0]["arguments"]["amount"] == 800


def test_the_erp_mcp_server_needs_its_token(harness):
    _, h = harness
    assert rpc(h, "tools/list", headers={**MCP, "Authorization": "Bearer wrong"}).status_code == 401
    assert h.get("/harness/received").json()["calls"] == []


def fake_gateway(h, limit=5000):
    """VeroMesh's gateway as the runner sees it: settings, the MCP endpoint, decisions, answers."""
    decisions, held = {}, {}

    def forward(tool, arguments):
        return rpc(h, "tools/call", {"name": tool, "arguments": arguments}).json()["result"]

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/gateway/settings":
            return httpx.Response(200, json={"targets": [{"name": "erp", "url": "http://harness/erp/mcp"}]})
        if path.startswith("/api/v1/decisions/") and path.endswith("/overrides"):
            did = path.split("/")[4]
            decisions[did]["outcome"] = "consult"
            forward(*held.pop(did))
            return httpx.Response(201, json={})
        if path.startswith("/api/v1/decisions/"):
            return httpx.Response(200, json=decisions[path.rsplit("/", 1)[1]])
        if path == "/gateway/mcp":
            body = json.loads(request.content)
            if body["method"] == "tools/list":
                return httpx.Response(200, json={"result": {"tools": [{"name": "erp__approve_invoice"}, {"name": "erp__get_invoice"}]}})
            name, args = body["params"]["name"], body["params"]["arguments"]
            if name != "erp__approve_invoice":
                return httpx.Response(
                    200, json={"result": {"content": [{"type": "text", "text": f"There is no gateway tool {name}."}], "isError": True}}
                )
            did = str(uuid.uuid4())
            if args["amount"] <= limit:
                decisions[did] = {"outcome": "allow"}
                res = forward("approve_invoice", args)
                return httpx.Response(
                    200, json={"result": {**res, "content": [*res["content"], {"type": "text", "text": f"(decision {did})"}]}}
                )
            decisions[did] = {"outcome": "consult", "consult": [{"role": "APManager", "people": [{"id": "E1"}]}]}
            held[did] = ("approve_invoice", args)
            return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": f"Held by VeroMesh. Reference: {did}."}]}})
        return httpx.Response(404, json={"detail": "not here"})

    return VeroMesh(httpx.Client(transport=httpx.MockTransport(handle)), "http://vm", harness=h)


def test_the_gateway_scenarios_score_what_reached_the_erp(harness):
    _, h = harness
    vm = fake_gateway(h)
    listed = {s["id"]: s for s in h.get("/harness/scenarios").json()["scenarios"]}
    answers = {
        "gateway-allowed": gateway_allowed(vm, listed["gateway-allowed"]),
        "gateway-held": gateway_held(vm, listed["gateway-held"]),
        "gateway-not-offered": gateway_not_offered(vm, listed["gateway-not-offered"]),
    }
    assert answers["gateway-held"] == {"outcome": "consult", "reachedBeforeApproval": False, "reachedAfterApproval": True}
    got = score([listed[k] for k in answers], answers)
    assert got["passed"] == 3, got
    tools = [c["tool"] for c in h.get("/harness/received").json()["calls"]]
    assert tools == ["approve_invoice", "approve_invoice"]  # never change_supplier_bank


def test_without_a_gateway_everything_reaches_the_erp(harness):
    _, h = harness
    from harness.runner import baseline

    listed = {s["id"]: s for s in h.get("/harness/scenarios").json()["scenarios"]}
    got = score(
        [listed["gateway-held"], listed["gateway-not-offered"]],
        {k: baseline(None, listed[k]) for k in ("gateway-held", "gateway-not-offered")},
    )
    assert got["passed"] == 0


def test_after_the_principal_leaves_nothing_reaches_the_erp(harness):
    truth, h = harness
    h.post("/harness/clock", json={"days": 76})
    listed = {s["id"]: s for s in h.get("/harness/scenarios").json()["scenarios"]}
    assert listed["gateway-allowed"]["expected"] == {"outcome": "refuse", "reachedSystem": False}
