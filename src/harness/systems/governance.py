"""The governance workbook: what no system of record holds, kept by the governance team.

Who may decide what and up to what limit, the second sign-offs above an amount, the
AI agents and the actions each is built for, and which AI uses the AI review board has
approved. Organizations keep these in a delegation of authority policy and a board
register, not in a system; VeroMesh takes them as its **model workbook**, the standard
format documented in VeroMesh's user guide and workbook reference. This system
publishes the simulated company's workbook in that format, as the governance team
would share it:

    GET /governance/workbook.xlsx          the workbook (Excel), as of its last update
    GET /governance/workbook               when it was last updated, and what it holds

The format is VeroMesh's schema, copied into this package (formats/veromesh-schema.json)
from VeroMesh's contract snapshot: sheet names, column headers and the IDs rows refer to
by. People are named by their employee ID, as a governance team would name them.

Quirks, each recorded as a defect:
- governance-workbook-stale: the team updates the workbook on the first of each month,
  so an agent whose principal has left since still names them. VeroMesh refuses to load
  a workbook that refers to someone no longer in the model, until it is corrected.
"""

from __future__ import annotations

import importlib
import io
import json
from datetime import date
from importlib import resources

from fastapi import APIRouter
from fastapi.responses import Response

from .base import SourceSystem, defect

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BOARD = "AIReviewBoard"
POLICY = "DelegationOfAuthorityPolicy"
# Second sign-off above an authority's co-approval amount: the next authority up.
CO_APPROVER = {"APManager": "FinanceApprover", "FinanceApprover": "Executive", "ProcurementApprover": "FinanceApprover"}
# What each agent is built to do (its registration in the workbook).
AGENT_ACTIONS = {"InvoiceAssistant": ["ApproveSupplierInvoice"], "ProcurementAgent": ["RaisePurchaseOrder"]}
# The process each agent action belongs to, and its owner role.
PROCESSES = {
    "InvoiceApproval": ("Supplier invoice approval", "APManager"),
    "Purchasing": ("Purchasing", "ProcurementApprover"),
}
ACTION_PROCESS = {"ApproveSupplierInvoice": "InvoiceApproval", "RaisePurchaseOrder": "Purchasing"}
# Impact map: action -> (KPI it moves, materiality threshold, unit). Below the threshold the
# agent acts and informs the KPI's owner; above it, it consults them first.
IMPACTS = {"ApproveSupplierInvoice": ("OnTimePaymentRate", 200, "invoices per day")}
ACTION_NAMES = {
    "ApproveSupplierInvoice": "Approve a supplier invoice",
    "RaisePurchaseOrder": "Raise a purchase order",
    "SignMedicationOrder": "Sign a medication order",
    "VerifyMedicationOrder": "Verify a medication order",
    "ApproveCareProtocolChange": "Approve a change to a care protocol",
    "ApprovePatientDataRelease": "Release patient data",
    "ApproveSystemChange": "Approve a change to a system",
    "ApprovePriorAuthorization": "Approve a prior authorization",
    "DenyPriorAuthorization": "Deny a prior authorization",
}


def schema() -> list[dict]:
    """VeroMesh's workbook format: every sheet and column it accepts."""
    return json.loads(resources.files("harness.formats").joinpath("veromesh-schema.json").read_text())


class Governance(SourceSystem):
    name = "governance"
    title = "Governance workbook (VeroMesh's model workbook format)"

    def updated_on(self) -> date:
        """The first of the month: when the team last brought the workbook up to date."""
        t = self.truth
        return t.today if self.lag(1) == 0 else t.today.replace(day=1)

    def project(self):
        t = self.truth
        prof = importlib.import_module(f"harness.profiles.{t.profile}")
        as_of = self.updated_on()
        defects: list[dict] = []
        sheets: dict[str, list[dict]] = {}
        sheets["Roles"] = [{"ID": r, "Name": r, "Definition": d} for r, d in sorted(t.roles.items())] + [
            {"ID": BOARD, "Name": BOARD, "Definition": "Approves the uses of AI the organization allows"}
        ]
        sheets["Policies"] = [
            {
                "ID": POLICY,
                "Name": "Delegation of Authority Policy",
                "Type": "Policy",
                "Owner role": "FinanceApprover",
                "Link": "https://sharepoint.example/Finance/Policies/Delegation of Authority Policy.md",
            }
        ]
        actions = sorted({a.action for a in t.authorities})

        def owner_role(person_id: str | None) -> str | None:
            p = t.people.get(person_id) if person_id else None
            pos = p.at(as_of) if p else None
            return next((r for r in (pos.roles if pos else []) if r in t.roles), None)

        # The measures the agents' actions move, each owned by its unit head's role, and the
        # impact map with its materiality thresholds: so the decision check knows whom to
        # inform or consult (an action with no mapped impact is consulted on).
        sheets["KPIs"] = [
            {"ID": k.id, "Name": k.name, "Unit": k.measure, **({"Owner role": r} if (r := owner_role(k.owner)) else {})}
            for k in sorted(t.kpis.values(), key=lambda k: k.id)
            if k.id in {kpi for kpi, _, _ in IMPACTS.values()}
        ]
        sheets["Processes"] = [{"ID": pid, "Name": name, "Owner role": role} for pid, (name, role) in sorted(PROCESSES.items())]
        sheets["Actions"] = [
            {
                "ID": a,
                "Name": ACTION_NAMES.get(a, a),
                **({"Part of process": ACTION_PROCESS[a]} if a in ACTION_PROCESS else {}),
                **({"Affects": IMPACTS[a][0]} if a in IMPACTS and IMPACTS[a][0] in t.kpis else {}),
            }
            for a in actions
        ]
        sheets["Impacts"] = [
            {"Action": a, "Impact on": kpi, "Materiality threshold": thr, "Threshold unit": unit}
            for a, (kpi, thr, unit) in sorted(IMPACTS.items())
            if a in actions and kpi in t.kpis
        ]
        auths, co = [], []
        for a in t.authorities:
            row = {
                "ID": a.id,
                "Name": f"{a.role}: {ACTION_NAMES.get(a.action, a.action).lower()}",
                "Holder": a.role,
                "Actions covered": a.action,
                "Delegated by": POLICY,
            }
            if a.limit is not None:
                row |= {"Limit amount": a.limit, "Currency": prof.CURRENCY}
            auths.append(row)
            if a.co_approval_over is not None and a.role in CO_APPROVER:
                co.append({"Authority": a.id, "Approver role": CO_APPROVER[a.role], "Above amount": a.co_approval_over})
        agents, uses = [], []
        for ag in sorted(t.agents.values(), key=lambda x: x.id):
            principal = t.people[ag.acts_for]
            if principal.left and principal.left <= as_of:
                continue  # the team removed it at the last update
            if principal.left and principal.left <= t.today:
                defects.append(
                    defect(
                        "governance-workbook-stale",
                        self.name,
                        {"agent": ag.id, "principal": principal.id, "left": principal.left.isoformat()},
                        "the workbook still names a principal who has left",
                    )
                )
            performs = AGENT_ACTIONS.get(ag.id, [])
            agents.append(
                {
                    "ID": ag.id,
                    "Name": ag.name,
                    "Acts for": principal.employee_id,
                    "Sign-in client ID": ag.client_id,
                    "Performs actions": ";".join(performs),
                }
            )
            pos = principal.at(min(as_of, principal.left or as_of)) or principal.history[-1]
            for action in performs:
                parent = next((a for a in t.authorities if a.action == action and a.role in pos.roles), None)
                if parent is None:
                    continue  # the principal holds no authority for it: the agent gets none either
                row = {
                    "ID": f"{ag.id}-{action}",
                    "Name": f"{ag.name}: {ACTION_NAMES.get(action, action).lower()}",
                    "Holder": ag.id,
                    "Actions covered": action,
                    "Delegated by": principal.employee_id,
                    "Parent authority": parent.id,
                }
                if parent.limit is not None:
                    row |= {"Limit amount": parent.limit, "Currency": prof.CURRENCY}
                auths.append(row)
            uses.append(
                {
                    "ID": f"Use-{ag.id}",
                    "Name": ag.purpose,
                    "Approved by": BOARD,
                    "Status": "approved",
                    "Covers agents": ag.id,
                    "Allows actions": ";".join(performs),
                    "Approved on": t.start.isoformat(),
                }
            )
        sheets["Authorities"] = auths
        sheets["Co-approvals"] = co
        sheets["Agents"] = agents
        sheets["AI use cases"] = uses
        return {"sheets": sheets, "updatedOn": as_of.isoformat()}, defects

    def workbook(self) -> bytes:
        from openpyxl import Workbook
        from openpyxl.styles import Font

        fmt = {t["label"]: t for t in schema()}
        wb = Workbook()
        about = wb.active
        about.title = "How to use"
        about.append(["VeroMesh model workbook"])
        about.append([f"{self.truth.org_name}: governance, as of {self.records()['updatedOn']}."])
        about.append(["Load it with:  veromesh-connect spreadsheet import <file> --source governance"])
        about.cell(1, 1).font = Font(bold=True, size=14)
        for label, rows in self.records()["sheets"].items():
            cols = [f["label"] for f in fmt[label]["fields"] if fmt[label]["keyed"] or f["key"] != "id"]
            unknown = {k for r in rows for k in r} - set(cols)
            if unknown:
                raise ValueError(f"{label}: columns {sorted(unknown)} are not in VeroMesh's format")
            ws = wb.create_sheet(label)
            ws.append(cols)
            for c in range(1, len(cols) + 1):
                ws.cell(1, c).font = Font(bold=True)
            for r in rows:
                ws.append([r.get(c, "") for c in cols])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def router(self) -> APIRouter:
        r = APIRouter()

        @r.get("/workbook.xlsx")
        def workbook():
            return Response(
                self.workbook(),
                media_type=XLSX,
                headers={"Content-Disposition": 'attachment; filename="governance.xlsx"'},
            )

        @r.get("/workbook")
        def about():
            rec = self.records()
            return {"updatedOn": rec["updatedOn"], "sheets": {k: len(v) for k, v in rec["sheets"].items()}}

        return r
