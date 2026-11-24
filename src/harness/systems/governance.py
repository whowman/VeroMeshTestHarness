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
AGENT_ACTIONS = {
    "InvoiceAssistant": ["ApproveSupplierInvoice"],
    "ProcurementAgent": ["RaisePurchaseOrder"],
    "PeopleAssistant": ["AdjustPay", "PromoteEmployee", "HireEmployee", "TerminateEmployee"],
}
#: The measures in the approval rules report, as VeroMesh limit kinds; its scopes, likewise.
HR_KINDS = {"Percent_Change": "percent", "Grade_Steps": "grade-steps", "Headcount": "headcount"}
HR_SCOPES = {"Supervisory Organization": "reporting-line", "Organization": "department", "Company": "any", "": "any"}


def hr_parents(prof, t, principal, as_of) -> dict[str, dict]:
    """For a principal who manages people: per action, the workbook columns of an agent
    authority bounded by the Manager initiator rule of its business process (the parent is
    the authority the Workday approvals connector writes: {role}-{action})."""
    rules = getattr(prof, "HR_RULES", [])
    has_reports = any(p.at(as_of) and p.at(as_of).manager == principal.id for p in t.people.values())
    if not rules or not has_reports:
        return {}
    role = prof.HR_GROUP_ROLES["Manager"]
    out = {}
    for bp, kind, group, measure, limit, scope, _ in rules:
        if kind != "Initiator" or group != "Manager" or bp not in prof.HR_ACTIONS:
            continue
        action = prof.HR_ACTIONS[bp]
        row = {"Parent authority": f"{role}-{action}", "Scope": HR_SCOPES.get(scope, "any")}
        if limit is not None and measure in HR_KINDS:
            row |= {"Limit kind": HR_KINDS[measure], "Limit value": limit}
        out[action] = row
    return out


# The process each agent action belongs to, and its owner role.
PROCESSES = {
    "InvoiceApproval": ("Supplier invoice approval", "APManager"),
    "Purchasing": ("Purchasing", "ProcurementApprover"),
}
ACTION_PROCESS = {"ApproveSupplierInvoice": "InvoiceApproval", "RaisePurchaseOrder": "Purchasing"}
# Impact map: action -> (KPI it moves, materiality threshold, unit). Below the threshold the
# agent acts and informs the KPI's owner; above it, it consults them first.
IMPACTS = {"ApproveSupplierInvoice": ("OnTimePaymentRate", 200, "invoices per day")}
# More impacts of the same action: (KPI, threshold, unit). Paying an invoice also uses cash.
MORE_IMPACTS = [("ApproveSupplierInvoice", "DaysPayableOutstanding", 4000, "USD per invoice")]
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
    "AdjustPay": "Adjust pay",
    "PromoteEmployee": "Promote an employee",
    "HireEmployee": "Hire an employee",
    "TerminateEmployee": "Terminate an employee",
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
        objectives = getattr(prof, "OBJECTIVES", [])
        capabilities = getattr(prof, "CAPABILITIES", [])
        health = {c[5][0]: (c[0], c[5][1], c[5][2]) for c in capabilities if c[5]}
        wanted = (
            {kpi for kpi, _, _ in IMPACTS.values()}
            | {kpi for _, kpi, _, _ in MORE_IMPACTS}
            | {k for o in objectives for k in o[6]}
            | set(health)
        )
        day = (as_of - t.start).days
        sheets["KPIs"] = []
        for k in sorted(t.kpis.values(), key=lambda k: k.id):
            if k.id not in wanted:
                continue
            row = {"ID": k.id, "Name": k.name, "Unit": k.measure, **({"Owner role": r} if (r := owner_role(k.owner)) else {})}
            if k.id in health:
                cap, thr, better = health[k.id]
                row |= {"Health of capabilities": cap, "Health threshold": thr, "Better when": better}
                if k.id == "OnTimePaymentRate" and hasattr(prof, "on_time_payment_rate"):
                    # The monthly scorecard: the value as of the workbook's last update.
                    row |= {"Current value": prof.on_time_payment_rate(day), "Measured on": as_of.isoformat()}
            sheets["KPIs"].append(row)
        sheets["Objectives"] = [
            {
                "ID": oid,
                "Name": name,
                "Level": level,
                "Owner role": owner,
                **({"Parent objective": parent} if parent else {}),
                **({"Priority": prio} if prio is not None else {}),
                **({"Measured by KPIs": ";".join(k for k in kpis if k in t.kpis)} if kpis else {}),
                **({"Conflicts with objectives": ";".join(conf)} if conf else {}),
            }
            for oid, name, level, parent, owner, prio, kpis, conf in objectives
        ]
        sheets["Capabilities"] = [
            {"ID": cid, "Name": name, "Owner role": owner, "Realized by processes": ";".join(procs), "Serves objectives": ";".join(serves)}
            for cid, name, owner, procs, serves, _ in capabilities
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
            for a, kpi, thr, unit in sorted([(a, *v) for a, v in IMPACTS.items()] + MORE_IMPACTS)
            if a in actions and kpi in t.kpis
        ] + [
            {"Action": a, "Impact on": cap, "Materiality threshold": thr, "Threshold unit": unit}
            for a, cap, thr, unit in getattr(prof, "HR_IMPACTS", [])
        ]
        for row in sheets["Actions"]:
            more = [kpi for a, kpi, _, _ in MORE_IMPACTS if a == row["ID"] and kpi in t.kpis]
            if more:
                row["Affects"] = ";".join([*([row["Affects"]] if row.get("Affects") else []), *more])
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
            hr = hr_parents(prof, t, principal, as_of)
            for action in performs:
                if action in hr:  # a decision about people: bounded by the Workday business process
                    auths.append(
                        {
                            "ID": f"{ag.id}-{action}",
                            "Name": f"{ag.name}: {ACTION_NAMES.get(action, action).lower()}",
                            "Holder": ag.id,
                            "Actions covered": action,
                            "Delegated by": principal.employee_id,
                            **hr[action],
                        }
                    )
                    continue
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
