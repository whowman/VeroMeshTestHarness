"""Workday HCM: the system of record for employees, their positions and their managers.

What a connector reads, in the two ways integrations read Workday:
- the REST API: /ccx/api/v1/{tenant}/workers (with limit and offset) and
  /ccx/api/v1/{tenant}/supervisoryOrganizations;
- Reports as a Service (RaaS), the usual route for bulk HR feeds:
  /ccx/service/customreport2/{tenant}/{owner}/VeroMesh_Workers?format=json, and
  VeroMesh_Worker_Changes with Updated_From and Updated_Through for what changed, and
  VeroMesh_Approval_Rules: the business process definitions' initiators and approval
  steps (promotions, pay changes, hires, terminations), from the profile's HR_RULES.
Sign-in is OAuth at /ccx/oauth2/{tenant}/token (refresh token or client credentials).

Workday holds employees only: contingent workers (contractors) are not in it, so they
appear in Entra ID with no HR record (recorded there as contractor-only-here).

Quirks, each recorded as a defect:
- retro-termination: some terminations are entered a few days after they took effect,
  so for those days the leaver still shows as active;
- vacant-supervisory-org: a supervisory organization whose manager has left has no
  manager until the post is filled;
- legal-and-preferred-name: people who go by a short name carry both; the systems
  downstream use one or the other.
"""

from __future__ import annotations

import importlib
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request

from .base import SourceSystem, defect

CLIENTS = {"veromesh-connector": "harness-secret"}
REFRESH_TOKEN = "harness-workday-refresh"  # noqa: S105 — the mock's integration refresh token
TOKEN = "harness-workday-token"  # noqa: S105
RETRO_DAYS = 4  # how late a retro termination is entered


class Workday(SourceSystem):
    name = "workday"
    title = "Workday HCM (REST API and Reports as a Service)"

    def wid(self, *keys) -> str:
        return self.sys_id(*keys)  # Workday IDs are 32 hexadecimal characters

    def project(self):
        t = self.truth
        prof = importlib.import_module(f"harness.profiles.{t.profile}")
        defects: list[dict] = []
        workers, orgs, changes = {}, {}, []
        managers = {pos.manager for q in t.active_people() if (pos := q.at(t.today)) and pos.manager}
        for u in t.units.values():
            head = t.head_of(u.id)
            orgs[u.id] = {
                "id": self.wid("org", u.id),
                "descriptor": u.name,
                "code": u.id,
                "superiorOrganization": {"id": self.wid("org", u.parent), "descriptor": t.units[u.parent].name} if u.parent else None,
                "manager": {"id": self.wid("worker", head.id), "descriptor": head.name} if head else None,
                "costCenter": u.cost_center,
            }
            if head is None:
                defects.append(defect("vacant-supervisory-org", self.name, {"unit": u.id}, "supervisory organization with no manager"))
        for p in t.people.values():
            if p.worker_type != "employee" or p.hired > t.today:
                continue
            late = bool(p.left) and self.quirk(0.2, "retro-termination", p.id)
            known_left = p.left if p.left and (not late or t.today >= p.left + timedelta(days=RETRO_DAYS)) else None
            if p.left and p.left <= t.today and not known_left:
                defects.append(
                    defect(
                        "retro-termination",
                        self.name,
                        {"person": p.id, "left": p.left.isoformat()},
                        "termination not yet entered; the worker still shows as active",
                    )
                )
            day = min(t.today, (known_left - timedelta(days=1)) if known_left else t.today)
            pos = p.at(day) or p.history[-1]
            preferred = prof.NICKNAMES.get(p.given) if self.quirk(0.5, "legal-and-preferred-name", p.id) else None
            if preferred and not (known_left and known_left <= t.today):
                defects.append(
                    defect(
                        "legal-and-preferred-name",
                        self.name,
                        {"person": p.id, "legal": p.given, "preferred": preferred},
                        "goes by a preferred name",
                    )
                )
            mgr = t.people.get(pos.manager) if pos.manager else None
            workers[p.id] = {
                "id": self.wid("worker", p.id),
                "descriptor": f"{preferred or p.given} {p.family}",
                "workerId": p.employee_id,
                "legalName": {"firstName": p.given, "lastName": p.family},
                "preferredName": {"firstName": preferred or p.given, "lastName": p.family},
                "primaryWorkEmail": p.email,
                "businessTitle": pos.title,
                "isManager": pos.head or p.id in managers,
                "primarySupervisoryOrganization": {"id": orgs[pos.unit]["id"], "descriptor": t.units[pos.unit].name},
                "manager": {"id": self.wid("worker", mgr.id), "descriptor": mgr.name, "workerId": mgr.employee_id} if mgr else None,
                "location": {"descriptor": p.location},
                "positionId": f"P-{p.employee_id[1:]}",
                "hireDate": p.hired.isoformat(),
                "terminationDate": known_left.isoformat() if known_left else None,
                "active": not (known_left and known_left <= t.today),
                "workerType": "Employee",
                "costCenter": t.units[pos.unit].cost_center,
            }
            # Business process events, entered when they happened (a retro termination later).
            for i, h in enumerate(p.history):
                kind = "Hire" if i == 0 else ("Change Organization Assignments" if h.unit != p.history[i - 1].unit else "Change Job")
                changes.append(
                    {
                        "Employee_ID": p.employee_id,
                        "Business_Process": kind,
                        "Effective_Date": h.start.isoformat(),
                        "Entered_On": h.start.isoformat(),
                    }
                )
            if known_left:
                entered = known_left + timedelta(days=RETRO_DAYS if late else 0)
                changes.append(
                    {
                        "Employee_ID": p.employee_id,
                        "Business_Process": "Termination",
                        "Effective_Date": known_left.isoformat(),
                        "Entered_On": entered.isoformat(),
                    }
                )
        return {"workers": workers, "orgs": orgs, "changes": changes}, defects

    # ------------------------------------------------------------ the API

    def router(self) -> APIRouter:
        r = APIRouter()

        def bearer(authorization: str = Header("")):
            if authorization != f"Bearer {TOKEN}":
                raise HTTPException(401, detail={"error": "invalid authentication", "errors": [{"error": "Invalid credentials"}]})

        auth = [Depends(bearer)]

        @r.post("/ccx/oauth2/{tenant}/token")
        def token(
            tenant: str,
            grant_type: str = Form(...),
            client_id: str = Form(""),
            client_secret: str = Form(""),
            refresh_token: str = Form(""),
        ):
            ok = CLIENTS.get(client_id) == client_secret and (
                (grant_type == "refresh_token" and refresh_token == REFRESH_TOKEN) or grant_type == "client_credentials"
            )
            if not ok:
                raise HTTPException(401, detail={"error": "invalid_client"})
            return {"access_token": TOKEN, "token_type": "Bearer", "refresh_token": REFRESH_TOKEN}

        def paged(request: Request, items: list[dict]) -> dict:
            q = request.query_params
            limit, offset = min(int(q.get("limit", 20)), 100), int(q.get("offset", 0))
            return {"total": len(items), "data": items[offset : offset + limit]}

        @r.get("/ccx/api/v1/{tenant}/workers", dependencies=auth)
        def workers(tenant: str, request: Request):
            items = sorted(self.records()["workers"].values(), key=lambda w: w["workerId"])
            search = request.query_params.get("search")
            if search:
                items = [
                    w for w in items if search.lower() in (w["descriptor"] + " " + w["primaryWorkEmail"] + " " + w["workerId"]).lower()
                ]
            return paged(request, items)

        @r.get("/ccx/api/v1/{tenant}/workers/{wid}", dependencies=auth)
        def worker(tenant: str, wid: str):
            for w in self.records()["workers"].values():
                if w["id"] == wid or w["workerId"] == wid:
                    return w
            raise HTTPException(404, detail={"error": "Resource not found"})

        @r.get("/ccx/api/v1/{tenant}/supervisoryOrganizations", dependencies=auth)
        def orgs(tenant: str, request: Request):
            return paged(request, sorted(self.records()["orgs"].values(), key=lambda o: o["descriptor"]))

        @r.get("/ccx/service/customreport2/{tenant}/{owner}/{report}", dependencies=auth)
        def raas(tenant: str, owner: str, report: str, request: Request):
            q = request.query_params
            if q.get("format", "json") != "json":
                raise HTTPException(400, detail={"error": "the harness serves format=json"})
            if report == "VeroMesh_Workers":
                return {"Report_Entry": [report_row(w) for w in sorted(self.records()["workers"].values(), key=lambda w: w["workerId"])]}
            if report == "VeroMesh_Worker_Changes":
                start = date.fromisoformat(q.get("Updated_From", "1900-01-01")[:10])
                end = date.fromisoformat(q.get("Updated_Through", self.truth.today.isoformat())[:10])
                rows = [
                    c
                    for c in self.records()["changes"]
                    if start <= date.fromisoformat(c["Entered_On"]) <= end and date.fromisoformat(c["Entered_On"]) <= self.truth.today
                ]
                return {"Report_Entry": sorted(rows, key=lambda c: (c["Entered_On"], c["Employee_ID"]))}
            if report == "VeroMesh_Approval_Rules":
                prof = importlib.import_module(f"harness.profiles.{self.truth.profile}")
                return {"Report_Entry": approval_rows(getattr(prof, "HR_RULES", []))}
            if report == "VeroMesh_Goals":
                return {"Report_Entry": goal_rows(self.truth)}
            raise HTTPException(404, detail={"error": f"No report {report} for {owner}"})

        return r


def report_row(w: dict) -> dict:
    """A worker as the VeroMesh_Workers custom report lays it out (Workday's RaaS style)."""
    return {
        "Employee_ID": w["workerId"],
        "Worker_WID": w["id"],
        "Legal_Name": f"{w['legalName']['firstName']} {w['legalName']['lastName']}",
        "Preferred_Name": f"{w['preferredName']['firstName']} {w['preferredName']['lastName']}",
        "Email_Primary_Work": w["primaryWorkEmail"],
        "Business_Title": w["businessTitle"],
        "Supervisory_Organization": w["primarySupervisoryOrganization"]["descriptor"],
        "Manager_Employee_ID": (w["manager"] or {}).get("workerId", ""),
        "Cost_Center": w["costCenter"],
        "Location": w["location"]["descriptor"],
        "Position_ID": w["positionId"],
        "Hire_Date": w["hireDate"],
        "Termination_Date": w["terminationDate"] or "",
        "Active_Status": "1" if w["active"] else "0",
        "Worker_Type": w["workerType"],
        # Role-based security groups assigned to the worker (an HR Partner on their organization).
        "Organization_Roles": "HR Partner" if w["businessTitle"] == "HR Business Partner" else "",
    }


def approval_rows(rules) -> list[dict]:
    """Business process rules as the VeroMesh_Approval_Rules custom report lays them out
    (one row per initiator or approval step of each business process definition)."""
    out = []
    for bp, kind, group, measure, limit, scope, step in rules:
        row = {"Business_Process_Type": bp, "Rule_Type": kind, "Security_Group": group}
        if measure:
            row["Condition_Measure"] = measure
        if limit is not None:
            row["Condition_Above"] = str(limit)
        if scope:
            row["Organization_Scope"] = scope
        if step is not None:
            row["Step_Order"] = str(step)
        out.append(row)
    return out


def goal_rows(t) -> list[dict]:
    """Organization goals (Workday Talent) from the profile's OBJECTIVES: each owned by the
    person filling its owner role (while it is vacant, whoever filled it when it was set,
    as Workday keeps the goal with its original owner), aligned to its parent goal."""
    prof = importlib.import_module(f"harness.profiles.{t.profile}")
    rows = []
    for oid, name, level, parent, role, _priority, kpis, _conflicts in getattr(prof, "OBJECTIVES", []):
        owner = t.holder(role, t.today) or t.holder(role, t.start)
        pos = owner.at(t.today) or owner.history[-1] if owner else None
        rows.append(
            {
                "Goal_ID": oid,
                "Goal_Name": name,
                "Description": name + ".",
                "Owner_Employee_ID": owner.employee_id if owner else "",
                "Owner_Business_Title": pos.title if pos else "",
                "Parent_Goal_ID": parent or "",
                "Goal_Level": level.title(),
                "Status": "In Progress",
                "KPI_IDs": ";".join(kpis),
            }
        )
    return rows
