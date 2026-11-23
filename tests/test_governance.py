"""The governance workbook: VeroMesh's standard model workbook, as the governance team keeps it."""

from __future__ import annotations

import io
import logging

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from harness.app import create_app
from harness.systems.governance import schema
from harness.truth import generate

logging.disable(logging.INFO)


def sheets(client) -> dict[str, list[dict]]:
    wb = load_workbook(io.BytesIO(client.get("/governance/workbook.xlsx").content), read_only=True)
    out = {}
    for ws in wb.worksheets[1:]:
        rows = list(ws.iter_rows(values_only=True))
        out[ws.title] = [dict(zip(rows[0], r, strict=False)) for r in rows[1:]]
    return out


def test_the_workbook_is_in_veromesh_s_format():
    truth = generate("healthcare", 300, 7)
    client = TestClient(create_app(truth))
    fmt = {t["label"]: t for t in schema()}
    got = sheets(client)
    assert {"Roles", "Policies", "Actions", "Authorities", "Co-approvals", "Agents", "AI use cases"} <= set(got)
    for label, rows in got.items():
        required = {f["label"] for f in fmt[label]["fields"] if f["required"] and (fmt[label]["keyed"] or f["key"] != "id")}
        for row in rows:
            assert set(row) <= {f["label"] for f in fmt[label]["fields"]}, label
            assert all(row[c] not in (None, "") for c in required), (label, row)


def test_agents_hold_a_bounded_copy_of_their_principal_s_authority():
    truth = generate("healthcare", 300, 7)
    got = sheets(TestClient(create_app(truth)))
    agent = next(a for a in got["Agents"] if a["ID"] == "InvoiceAssistant")
    principal = truth.people[truth.agents["InvoiceAssistant"].acts_for]
    assert agent["Acts for"] == principal.employee_id and agent["Performs actions"] == "ApproveSupplierInvoice"
    auth = next(a for a in got["Authorities"] if a["Holder"] == "InvoiceAssistant")
    parent = next(a for a in got["Authorities"] if a["ID"] == auth["Parent authority"])
    assert auth["Limit amount"] == parent["Limit amount"] and parent["Holder"] in principal.at(truth.today).roles
    use = next(u for u in got["AI use cases"] if u["Covers agents"] == "InvoiceAssistant")
    assert use["Status"] == "approved" and use["Approved by"] == "AIReviewBoard"


def test_the_workbook_lags_until_the_first_of_the_month():
    truth = generate("healthcare", 300, 7)
    client = TestClient(create_app(truth))
    client.post("/harness/clock", json={"days": 75})  # the Invoice Assistant's principal leaves today
    assert any(a["ID"] == "InvoiceAssistant" for a in sheets(client)["Agents"])
    stale = [d for d in client.get("/harness/answer-key").json()["defects"] if d["kind"] == "governance-workbook-stale"]
    assert [d["about"]["agent"] for d in stale] == ["InvoiceAssistant"]
    about = client.get("/governance/workbook").json()
    assert about["updatedOn"].endswith("-01") and about["sheets"]["Agents"] == 4


def test_at_difficulty_zero_the_workbook_is_current():
    truth = generate("healthcare", 300, 7, difficulty={"scale": 0, "overrides": {}})
    client = TestClient(create_app(truth))
    client.post("/harness/clock", json={"days": 75})
    assert not any(a["ID"] == "InvoiceAssistant" for a in sheets(client)["Agents"])
