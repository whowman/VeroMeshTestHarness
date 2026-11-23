"""Oracle Fusion Cloud ERP: suppliers, purchase orders and supplier invoices, with who
approved each, and the ERP's own user accounts and job roles.

What a connector reads, through Oracle's REST API:
- /fscmRestApi/resources/11.13.18.05/suppliers, purchaseOrders and invoices;
- /hcmRestApi/resources/11.13.18.05/userAccounts (each with its roles).
Paging uses limit and offset, with hasMore (and totalResults=true for the count);
filters use q, Oracle's ``Field op value;Field op value`` form. Basic authentication.

Invoices and purchase orders are generated for every day from 90 days before the
simulation's start to today. Each is approved by someone who held the authority that day
(truth.authorities): an AP specialist up to 5,000, the AP Manager up to 50,000 (with a
second approver above 25,000), a finance approver above that. While the AP Manager's
post is vacant, the Controller approves what the AP Manager would have.

Quirks, each recorded as a defect:
- approved-over-limit: a few invoices were approved by someone above their limit;
- missing-co-approval: a few invoices over the co-approval threshold have one approver;
- self-approved-po: a few purchase orders were raised and approved by the same buyer
  (a segregation-of-duties clash);
- leaver-retains-role: ERP accounts are end-dated 10 days after HR, so a leaver keeps
  their job roles (and can still approve) for those days.
"""

from __future__ import annotations

import base64
import random
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .base import SourceSystem, defect

USERS = {"integration.user": "harness"}
FSCM = "/fscmRestApi/resources/11.13.18.05"
HCM = "/hcmRestApi/resources/11.13.18.05"
END_DATE_LAG = 10  # days between HR's termination and the ERP account's end date
HISTORY_DAYS = 90  # transactions before the simulation's start
JOB_ROLES = {
    "APClerk": "Accounts Payable Specialist",
    "APManager": "Accounts Payable Manager",
    "FinanceApprover": "Financial Approver",
    "Buyer": "Buyer",
    "ProcurementApprover": "Procurement Manager",
}
SUPPLIERS = [
    "MedSupply Co",
    "Northline Pharma",
    "Clearwater Linen",
    "Summit Imaging",
    "Bayview Lab Services",
    "Copperleaf Foods",
    "Granite IT Services",
    "Harbor Biomedical",
    "Ridgeway Staffing",
    "Tidewater Utilities",
    "Orchard Office Supply",
    "Pinecrest Surgical",
]


class Erp(SourceSystem):
    name = "erp"
    title = "Oracle Fusion Cloud ERP (REST API)"

    def project(self):
        t = self.truth
        rng = random.Random(f"{t.seed}:erp")
        defects: list[dict] = []
        limits = {a.role: a for a in t.authorities if a.action == "ApproveSupplierInvoice"}
        po_limits = {a.role: a for a in t.authorities if a.action == "RaisePurchaseOrder"}
        suppliers = {
            i + 300001: {
                "SupplierId": i + 300001,
                "Supplier": name,
                "SupplierNumber": f"{5000 + i}",
                "Status": "ACTIVE",
                "BusinessRelationship": "SPEND_AUTHORIZED",
            }
            for i, name in enumerate(SUPPLIERS)
        }

        # Who held each ERP role when: a span per position, running on after a leaver
        # until their ERP account was end-dated.
        spans: dict[str, list] = {r: [] for r in JOB_ROLES}
        for p in t.people.values():
            for i, h in enumerate(p.history):
                end = p.history[i + 1].start if i + 1 < len(p.history) else None
                if p.left and (end is None or p.left < end):
                    end = p.left + timedelta(days=END_DATE_LAG)
                for role in h.roles:
                    if role in spans:
                        spans[role].append((p, max(h.start, p.hired), end))
        seen: dict = {}

        def holders(role: str, day) -> list:
            """Who can act in ``role`` on ``day`` in the ERP."""
            key = (role, day)
            if key not in seen:
                seen[key] = sorted({p.id: p for p, s, e in spans[role] if s <= day and (e is None or day < e)}.values(), key=lambda p: p.id)
            return seen[key]

        invoices, pos_ = {}, {}
        first = t.start - timedelta(days=HISTORY_DAYS)
        day, n = first, 0
        while day <= t.today:
            for _ in range(rng.randint(14, 26)):
                n += 1
                amount = round(min(rng.lognormvariate(7.6, 1.3), 900_000), 2)
                role = (
                    "APClerk"
                    if amount <= limits["APClerk"].limit
                    else "APManager"
                    if amount <= limits["APManager"].limit
                    else "FinanceApprover"
                )
                people = holders(role, day) or (holders("FinanceApprover", day) if role == "APManager" else [])
                if not people:
                    continue
                approver = rng.choice(people)
                second = None
                over = limits[role].co_approval_over
                if over and amount > over:
                    others = [p for p in holders("FinanceApprover", day) if p.id != approver.id]
                    second = rng.choice(others) if others else None
                    if second and self.quirk(0.03, "no-co", n):
                        defects.append(
                            defect(
                                "missing-co-approval",
                                self.name,
                                {"invoice": f"INV-{n:06d}", "amount": amount, "approver": approver.id},
                                f"over {over:,} with one approver",
                            )
                        )
                        second = None
                if (
                    role == "APManager"
                    and limits["APClerk"].limit < amount < limits["APClerk"].limit * 1.6
                    and self.quirk(0.08, "over-limit", n)
                ):
                    clerks = holders("APClerk", day)
                    if clerks:
                        approver = rng.choice(clerks)
                        defects.append(
                            defect(
                                "approved-over-limit",
                                self.name,
                                {"invoice": f"INV-{n:06d}", "amount": amount, "approver": approver.id, "limit": limits["APClerk"].limit},
                                "approved above the approver's limit",
                            )
                        )
                if approver.left and approver.left <= day:
                    defects.append(
                        defect(
                            "leaver-retains-role",
                            self.name,
                            {"invoice": f"INV-{n:06d}", "approver": approver.id, "left": approver.left.isoformat()},
                            "approved by someone who had left, before their ERP account was end-dated",
                        )
                    )
                sid = 300001 + rng.randrange(len(SUPPLIERS))
                inv = {
                    "InvoiceId": 100000 + n,
                    "InvoiceNumber": f"INV-{n:06d}",
                    "SupplierId": sid,
                    "Supplier": suppliers[sid]["Supplier"],
                    "InvoiceAmount": amount,
                    "InvoiceCurrency": "USD",
                    "InvoiceDate": day.isoformat(),
                    "DueDate": (day + timedelta(days=30)).isoformat(),
                    "ApprovalStatus": "APPROVED",
                    "ApproverPersonNumber": approver.employee_id,
                    "SecondApproverPersonNumber": second.employee_id if second else None,
                    "PaidFlag": (day + timedelta(days=30)) <= t.today and rng.random() < 0.95,
                    "PaidOnTime": rng.random() < 0.87,
                    "BusinessUnit": "Kestrel Valley Health BU",
                }
                invoices[inv["InvoiceId"]] = inv
            for _ in range(rng.randint(3, 8)):
                n += 1
                amount = round(min(rng.lognormvariate(8.0, 1.2), 400_000), 2)
                buyers = holders("Buyer", day)
                if not buyers:
                    continue
                buyer = rng.choice(buyers)
                need_approval = amount > po_limits["Buyer"].limit
                approvers = [p for p in holders("ProcurementApprover", day) if p.id != buyer.id]
                approver = rng.choice(approvers) if need_approval and approvers else None
                if need_approval and "ProcurementApprover" in (buyer.at(day) or buyer.history[-1]).roles and self.quirk(0.3, "self-po", n):
                    approver = buyer
                    defects.append(
                        defect(
                            "self-approved-po",
                            self.name,
                            {"po": f"PO-{n:06d}", "person": buyer.id, "amount": amount},
                            "raised and approved by the same person",
                        )
                    )
                po = {
                    "POHeaderId": 200000 + n,
                    "OrderNumber": f"PO-{n:06d}",
                    "SupplierId": 300001 + rng.randrange(len(SUPPLIERS)),
                    "Total": amount,
                    "Currency": "USD",
                    "CreationDate": day.isoformat(),
                    "BuyerPersonNumber": buyer.employee_id,
                    "ApproverPersonNumber": approver.employee_id if approver else None,
                    "Status": "OPEN" if day > t.today - timedelta(days=20) else "CLOSED",
                }
                po["Supplier"] = suppliers[po["SupplierId"]]["Supplier"]
                pos_[po["POHeaderId"]] = po
            day += timedelta(days=1)
        accounts = {}
        for p in t.people.values():
            pos = p.at(t.today)
            ended = p.left and t.today >= p.left + timedelta(days=END_DATE_LAG)
            if pos is None and not (p.left and not ended):
                continue
            held = (pos or p.history[-1]).roles
            roles = [JOB_ROLES[r] for r in held if r in JOB_ROLES]
            if not roles:
                continue
            if pos is None:
                defects.append(
                    defect(
                        "leaver-retains-role",
                        self.name,
                        {"person": p.id, "left": p.left.isoformat(), "roles": roles},
                        "ERP account still active after the person left",
                    )
                )
            accounts[p.employee_id] = {
                "UserId": self.sys_id("erpuser", p.id)[:16],
                "Username": p.email.split("@")[0].upper(),
                "PersonNumber": p.employee_id,
                "SuspendedFlag": False,
                "roles": [{"RoleCode": "ORA_" + r.upper().replace(" ", "_") + "_JOB", "RoleName": r} for r in roles],
            }
        return {"suppliers": suppliers, "invoices": invoices, "purchaseOrders": pos_, "userAccounts": accounts}, defects

    # ------------------------------------------------------------ the API

    def router(self) -> APIRouter:
        r = APIRouter()

        def signed_in(authorization: str = Header("")):
            if authorization.startswith("Basic "):
                user, _, pw = base64.b64decode(authorization[6:]).decode().partition(":")
                if USERS.get(user) == pw:
                    return
            raise HTTPException(401, detail="Unauthorized")

        auth = [Depends(signed_in)]

        def collection(kind: str, request: Request) -> dict:
            q = request.query_params
            items = list(self.records()[kind].values())
            items = oracle_q(items, q.get("q", ""))
            limit, offset = min(int(q.get("limit", 25)), 500), int(q.get("offset", 0))
            chunk = items[offset : offset + limit]
            out = {"items": chunk, "count": len(chunk), "hasMore": offset + limit < len(items), "limit": limit, "offset": offset}
            if q.get("totalResults") == "true":
                out["totalResults"] = len(items)
            return out

        def endpoint(kind: str):
            def handler(request: Request) -> dict:
                return collection(kind, request)

            return handler

        for path, kind in (
            (f"{FSCM}/suppliers", "suppliers"),
            (f"{FSCM}/invoices", "invoices"),
            (f"{FSCM}/purchaseOrders", "purchaseOrders"),
            (f"{HCM}/userAccounts", "userAccounts"),
        ):
            r.add_api_route(path, endpoint(kind), methods=["GET"], dependencies=auth, name=kind)
        return r


_TERM = re.compile(r"^\s*(\w+)\s*(>=|<=|!=|=|>|<|LIKE)\s*'?(.*?)'?\s*$")


def oracle_q(items: list[dict], q: str) -> list[dict]:
    """Oracle's ``q``: ``Field op value`` terms joined by ``;`` (and)."""
    for term in filter(None, q.split(";")):
        m = _TERM.match(term)
        if not m:
            raise HTTPException(400, detail=f"Unsupported q term in the harness: {term}")
        field, op, want = m.groups()

        def ok(item, field=field, op=op, want=want):
            got = item.get(field)
            if isinstance(got, (int, float)) and not isinstance(got, bool):
                got, w = float(got), float(want)
            else:
                got, w = str(got), want
            if op == "LIKE":
                return re.fullmatch(want.replace("%", ".*").replace("*", ".*"), str(got), re.IGNORECASE) is not None
            return {"=": got == w, "!=": got != w, ">": got > w, "<": got < w, ">=": got >= w, "<=": got <= w}[op]

        items = [i for i in items if ok(i)]
    return items
