"""SharePoint, through Microsoft Graph: the documents where much of an organization's
context lives (policies, procedures, charters, runbooks, registers, minutes, org charts).

What a connector reads: /sharepoint/v1.0/sites (with ?search=), a site's drive, a
folder's children, an item, its content (markdown here; as in Graph, a 302 to a
pre-authenticated download URL), and the drive's delta. It uses
the same Graph token as Entra ID (/entra/{tenant}/oauth2/v2.0/token).

Every document makes claims, each checked against the truth every day:
- holds: a person holds a title or a role;
- reports-to: a unit reports into another;
- limit: a role's limit for an action;
- owns: a person owns a system or a data asset (as business or technical owner, owner
  or steward).
Most documents are written before the simulation starts and never updated, so their
claims go stale as the clock moves; the data governance council's minutes are written
each month, true when written. A claim that is false today is a defect:
- document-names-leaver: it names someone who has left;
- document-outdated: it was true and no longer is;
- policy-system-mismatch: the policy states a limit the ERP does not enforce (a quirk);
- register-conflicts-with-catalog: the data owner register names the wrong owner for
  one table (a quirk).
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from .base import SourceSystem, defect
from .entra import TOKEN as GRAPH_TOKEN

SITES = {
    "finance": "Finance",
    "clinical": "Clinical Operations",
    "it": "Information Technology",
    "datagov": "Data Governance",
    "people": "People",
}
POLICY_MISMATCH = ("APClerk", "ApproveSupplierInvoice", 7_500)  # the policy says 7,500; the ERP enforces 5,000
REGISTER_WRONG = ("clinical.silver.encounters", "CLIN")  # the register names the CMO as owner


class SharePoint(SourceSystem):
    name = "sharepoint"
    title = "SharePoint (Microsoft Graph: sites, drives, documents)"

    # ------------------------------------------------------------ documents and their claims

    def documents(self) -> list[dict]:
        """Every document: site, folder, name, written (date), author, body, claims."""
        t = self.truth
        start = t.start
        name = lambda pid: t.people[pid].name if pid else "(vacant)"  # noqa: E731
        head = lambda unit, day=start: t.head_of(unit, day)  # noqa: E731
        docs = []

        def doc(site, folder, title, written, author, body, claims):
            docs.append(
                {
                    "site": site,
                    "folder": folder,
                    "name": f"{title}.md",
                    "written": written,
                    "author": author,
                    "body": body,
                    "claims": claims,
                }
            )

        # Finance: the delegation of authority policy and the invoice exception procedure.
        cfo = head("FIN")
        rows, claims = [], []
        for a in t.authorities:
            limit = a.limit
            if (a.role, a.action) == POLICY_MISMATCH[:2] and self.on("policy-system-mismatch"):
                limit = POLICY_MISMATCH[2]
            shown = f"{limit:,} USD" if limit else "no amount limit"
            co = f"; a second approver above {a.co_approval_over:,} USD" if a.co_approval_over else ""
            rows.append(f"| {a.role} | {a.action} | {shown}{co} |")
            if a.limit is not None:
                claims.append({"type": "limit", "role": a.role, "action": a.action, "value": limit})
        doc(
            "finance",
            "Policies",
            "Delegation of Authority Policy",
            start - timedelta(days=200),
            cfo,
            f"# Delegation of Authority Policy\n\nOwner: {cfo.name}, Chief Financial Officer. Effective {start - timedelta(days=200)}.\n\n"
            "Each role below may approve up to the limit shown. Above it, the request goes to the next role up.\n\n"
            "| Role | Action | Limit |\n| --- | --- | --- |\n" + "\n".join(rows) + "\n",
            claims,
        )
        ap = head("AP")
        doc(
            "finance",
            "Procedures",
            "Invoice exception escalation",
            start - timedelta(days=120),
            ap,
            f"# Invoice exception escalation\n\nWhen an invoice fails matching or is above your limit, escalate to the Accounts Payable Manager, "
            f"{ap.name} ({ap.email}). If the Accounts Payable Manager is unavailable, escalate to the Controller.\n",
            [{"type": "holds", "person": ap.id, "title": "Accounts Payable Manager"}],
        )
        # Clinical: the medication verification SOP and the Clinical Informatics charter.
        pharm = head("PHAR")
        doc(
            "clinical",
            "SOPs",
            "Medication order verification",
            start - timedelta(days=300),
            pharm,
            f"# Medication order verification\n\nOwner: {pharm.name}, Director of Pharmacy.\n\nEvery medication order is verified by a pharmacist "
            "before it is dispensed. Target: the median order is verified within 30 minutes (KPI: MedicationOrderVerificationTime). "
            "Only clinicians with the Prescriber role may sign orders; an AI copilot may draft, never sign.\n",
            [{"type": "holds", "person": pharm.id, "title": "Director of Pharmacy"}],
        )
        cmio = head("CINF")
        doc(
            "clinical",
            "Charters",
            "Clinical Informatics charter",
            start - timedelta(days=400),
            cmio,
            f"# Clinical Informatics charter\n\nClinical Informatics is led by {cmio.name}, Chief Medical Information Officer, and reports to "
            f"{t.units[self.parent_on('CINF', start)].name}. It owns the clinical data in the lakehouse and the Clinician Copilot's clinical content.\n",
            [
                {"type": "reports-to", "unit": "CINF", "parent": self.parent_on("CINF", start)},
                {"type": "holds", "person": cmio.id, "title": "Chief Medical Information Officer"},
            ],
        )
        # IT: a runbook per system, naming its owners as they were when it was written.
        for s in t.systems.values():
            bo, to = self.owner_on("system", s.id, "business_owner", start), self.owner_on("system", s.id, "technical_owner", start)
            doc(
                "it",
                "Runbooks",
                f"{s.name} runbook",
                start - timedelta(days=90),
                t.people[to] if to else head("IT"),
                f"# {s.name} runbook\n\nBusiness owner: {name(bo)}. Technical owner: {name(to)}. "
                f"Support: {t.units[s.technical_unit].name} Support. Criticality: {s.criticality}.\n\nFor an outage, page the technical owner, then the support group.\n",
                [
                    {"type": "owns", "of": "system", "id": s.id, "field": "business_owner", "person": bo},
                    {"type": "owns", "of": "system", "id": s.id, "field": "technical_owner", "person": to},
                ],
            )
        # Data governance: the data owner register, and the council's monthly minutes.
        rows, claims = [], []
        for a in t.assets.values():
            then = self.owner_on("asset", a.name, "owner", start)
            steward = self.owner_on("asset", a.name, "steward", start)
            owner = then
            if (a.name, REGISTER_WRONG[1]) == REGISTER_WRONG and self.on("register-conflicts-with-catalog"):
                owner = head(REGISTER_WRONG[1]).id
            rows.append(f"| {a.name} | {name(owner)} | {name(steward)} |")
            claims.append({"type": "owns", "of": "asset", "id": a.name, "field": "owner", "person": owner, "quirk": owner != then})
            claims.append({"type": "owns", "of": "asset", "id": a.name, "field": "steward", "person": steward})
        cdo = head("DATA")
        doc(
            "datagov",
            "Registers",
            "Data owner register",
            start - timedelta(days=60),
            cdo,
            "# Data owner register\n\n| Table | Owner | Steward |\n| --- | --- | --- |\n" + "\n".join(rows) + "\n",
            claims,
        )
        month = date(start.year, start.month, 1)
        while month <= t.today:
            when = month + timedelta(days=14)
            if when <= t.today:
                chair = t.head_of("DATA", when)
                kpi_lines = "\n".join(f"- {k.name}: owner {name(self.owner_on('kpi', k.id, 'owner', when))}." for k in t.kpis.values())
                doc(
                    "datagov",
                    "Minutes",
                    f"Data governance council {when:%Y-%m}",
                    when,
                    chair or cdo,
                    f"# Data governance council, {when:%d %B %Y}\n\nChair: {name(chair.id if chair else None)}.\n\nKPI owners confirmed:\n{kpi_lines}\n",
                    [
                        {
                            "type": "owns",
                            "of": "kpi",
                            "id": k.id,
                            "field": "owner",
                            "person": self.owner_on("kpi", k.id, "owner", when),
                            "asOf": when.isoformat(),
                        }
                        for k in t.kpis.values()
                    ],
                )
            month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        # People: an org chart per unit, as it was at the start.
        for u in t.units.values():
            h = head(u.id)
            leads = sorted(
                {
                    q.at(start).manager
                    for q in t.active_people(start)
                    if q.at(start).unit == u.id
                    and q.at(start).manager
                    and q.at(start).manager != (h.id if h else None)
                    and t.people[q.at(start).manager].at(start)
                    and t.people[q.at(start).manager].at(start).unit == u.id
                }
            )
            lines = "\n".join(f"  - {t.people[x].name}, {t.people[x].at(start).title}" for x in leads)
            parent_then = self.parent_on(u.id, start)
            parent = t.units[parent_then].name if parent_then else "the board"
            doc(
                "people",
                "Org charts",
                f"{u.name} org chart",
                start - timedelta(days=30),
                head("HR"),
                f"# {u.name}\n\nReports to: {parent}.\n\n- {name(h.id if h else None)}, head of {u.name}\n{lines}\n",
                [{"type": "reports-to", "unit": u.id, "parent": parent_then}]
                + ([{"type": "holds", "person": h.id, "title": h.at(start).title}] if h else [])
                + [{"type": "holds", "person": x, "title": t.people[x].at(start).title} for x in leads],
            )
        return docs

    def check(self, claim: dict) -> tuple[bool, str | None]:
        """Whether a claim holds today, and if not, the person it names who has left."""
        t = self.truth
        typ = claim["type"]
        if typ == "holds":
            p = t.people[claim["person"]]
            pos = p.at(t.today)
            return (pos is not None and pos.title == claim["title"]), (p.id if not p.active(t.today) else None)
        if typ == "reports-to":
            return t.units[claim["unit"]].parent == claim["parent"], None
        if typ == "limit":
            a = next(a for a in t.authorities if (a.role, a.action) == (claim["role"], claim["action"]))
            return a.limit == claim["value"], None
        if typ == "owns":
            target = {"system": t.systems, "asset": t.assets, "kpi": t.kpis}[claim["of"]][claim["id"]]
            actual = getattr(target, claim["field"])
            who = claim["person"]
            left = who if who and not t.people[who].active(t.today) else None
            return actual == who, left
        raise ValueError(typ)

    # ------------------------------------------------------------ the projection

    def project(self):
        t = self.truth
        defects = []
        sites, items = {}, {}
        for key, title in SITES.items():
            sid = f"kestrelvalley.sharepoint.example,{self.guid('site', key)},{self.guid('web', key)}"
            sites[key] = {
                "id": sid,
                "name": key,
                "displayName": title,
                "webUrl": f"https://kestrelvalley.sharepoint.example/sites/{key}",
                "_drive": self.guid("drive", key),
            }
        folders: dict[tuple[str, str], dict] = {}
        for d in self.documents():
            site = sites[d["site"]]
            fkey = (d["site"], d["folder"])
            if fkey not in folders:
                fid = self.guid("folder", *fkey)
                folders[fkey] = {
                    "id": fid,
                    "name": d["folder"],
                    "folder": {"childCount": 0},
                    "parentReference": {"driveId": site["_drive"], "path": "/drive/root:"},
                    "createdDateTime": f"{t.start - timedelta(days=400)}T09:00:00Z",
                    "lastModifiedDateTime": f"{t.start - timedelta(days=400)}T09:00:00Z",
                    "webUrl": f"{site['webUrl']}/Shared%20Documents/{d['folder'].replace(' ', '%20')}",
                    "_drive": site["_drive"],
                    "_parent": "root",
                }
                items[fid] = folders[fkey]
            folder = folders[fkey]
            folder["folder"]["childCount"] += 1
            iid = self.guid("item", d["site"], d["folder"], d["name"])
            body = d["body"].encode()
            author = d["author"]
            items[iid] = {
                "id": iid,
                "name": d["name"],
                "size": len(body),
                "file": {"mimeType": "text/markdown", "hashes": {"sha256Hash": hashlib.sha256(body).hexdigest().upper()}},
                "createdDateTime": f"{d['written']}T10:00:00Z",
                "lastModifiedDateTime": f"{d['written']}T10:00:00Z",
                "createdBy": {"user": {"displayName": author.name, "email": author.email}} if author else {},
                "parentReference": {"driveId": site["_drive"], "id": folder["id"], "path": f"/drive/root:/{d['folder']}"},
                "webUrl": f"{folder['webUrl']}/{d['name'].replace(' ', '%20')}",
                "_drive": site["_drive"],
                "_parent": folder["id"],
                "_body": body,
                "_claims": d["claims"],
            }
            for i, c in enumerate(d["claims"]):
                ok, left = self.check(c)
                if ok:
                    continue
                if c.get("type") == "limit":
                    kind = "policy-system-mismatch"
                elif c.get("quirk"):
                    kind = "register-conflicts-with-catalog"
                elif left:
                    kind = "document-names-leaver"
                else:
                    kind = "document-outdated"
                defects.append(
                    defect(
                        kind,
                        self.name,
                        {"document": f"{SITES[d['site']]}/{d['folder']}/{d['name']}", "claim": i, **c},
                        "a statement in the document no longer matches the organization",
                    )
                )
        return {"sites": sites, "items": items}, defects

    # ------------------------------------------------------------ the API

    def router(self) -> APIRouter:
        r = APIRouter()

        def bearer(authorization: str = Header("")):
            if authorization != f"Bearer {GRAPH_TOKEN}":
                raise HTTPException(
                    401, detail={"error": {"code": "InvalidAuthenticationToken", "message": "Access token is empty or invalid."}}
                )

        auth = [Depends(bearer)]

        def public(i: dict) -> dict:
            return {k: v for k, v in i.items() if not k.startswith("_")}

        def drive_items(drive: str) -> list[dict]:
            return [i for i in self.records()["items"].values() if i["_drive"] == drive]

        @r.get("/v1.0/sites", dependencies=auth)
        def sites(request: Request):
            q = (request.query_params.get("search") or "").strip("*").lower()
            return {"value": [public(s) for s in self.records()["sites"].values() if q in s["displayName"].lower() or q in s["name"]]}

        @r.get("/v1.0/sites/{site_id}", dependencies=auth)
        def site(site_id: str):
            return public(self._site(site_id))

        @r.get("/v1.0/sites/{site_id}/drive", dependencies=auth)
        def site_drive(site_id: str):
            s = self._site(site_id)
            return {"id": s["_drive"], "name": "Documents", "driveType": "documentLibrary", "webUrl": f"{s['webUrl']}/Shared%20Documents"}

        @r.get("/v1.0/drives/{drive}/root/children", dependencies=auth)
        def root_children(drive: str):
            return {"value": [public(i) for i in sorted(drive_items(drive), key=lambda i: i["name"]) if i["_parent"] == "root"]}

        @r.get("/v1.0/drives/{drive}/items/{item}/children", dependencies=auth)
        def children(drive: str, item: str):
            return {"value": [public(i) for i in sorted(drive_items(drive), key=lambda i: i["name"]) if i["_parent"] == item]}

        @r.get("/v1.0/drives/{drive}/items/{item}", dependencies=auth)
        def one(drive: str, item: str):
            return public(self._item(drive, item))

        @r.get("/v1.0/drives/{drive}/items/{item}/content", dependencies=auth)
        def content(drive: str, item: str, request: Request):
            """As Graph does: 302 Found to a pre-authenticated download URL (no token needed)."""
            i = self._item(drive, item)
            if "_body" not in i:
                raise HTTPException(400, detail={"error": {"code": "invalidRequest", "message": "A folder has no content."}})
            url = str(request.url_for("download", key=self.download_key(drive, item)))
            return Response(status_code=302, headers={"Location": url})

        @r.get("/download/{key}", name="download")
        def download(key: str):
            for i in self.records()["items"].values():
                if "_body" in i and self.download_key(i["_drive"], i["id"]) == key:
                    return Response(i["_body"], media_type="text/markdown")
            raise HTTPException(404, detail={"error": {"code": "itemNotFound", "message": "The download link has expired."}})

        @r.get("/v1.0/drives/{drive}/root/delta", dependencies=auth)
        def delta(drive: str, request: Request, token: str | None = None):
            since = token or "0000"
            items = [public(i) for i in drive_items(drive) if i["lastModifiedDateTime"] > since]
            now = f"{self.truth.today}T23:59:59Z"
            return {"value": items, "@odata.deltaLink": str(request.url.remove_query_params(["token"]).include_query_params(token=now))}

        return r

    def download_key(self, drive: str, item: str) -> str:
        """The pre-authenticated part of a download URL: good for the current day only."""
        return hashlib.sha256(f"{self.truth.seed}:{drive}:{item}:{self.truth.today}".encode()).hexdigest()[:40]

    def _site(self, site_id: str) -> dict:
        for s in self.records()["sites"].values():
            if site_id in (s["id"], s["name"]):
                return s
        raise HTTPException(404, detail={"error": {"code": "itemNotFound", "message": "The site could not be found."}})

    def _item(self, drive: str, item: str) -> dict:
        i = self.records()["items"].get(item)
        if i is None or i["_drive"] != drive:
            raise HTTPException(404, detail={"error": {"code": "itemNotFound", "message": "The resource could not be found."}})
        return i
