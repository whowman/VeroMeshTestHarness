"""SAP LeanIX, through its GraphQL API.

The mock follows LeanIX's public API:

- Sign-in: a technical user's API token, exchanged at ``POST /services/mtm/v1/oauth2/token``
  with basic authentication as ``apitoken:<token>`` and ``grant_type=client_credentials``,
  for a short-lived access token; then ``Authorization: Bearer <token>``.
- ``POST /services/pathfinder/v1/graphql``: ``allFactSheets(factSheetType:
  BusinessCapability, first:, after:)`` in Relay pages (``pageInfo { hasNextPage
  endCursor }``, ``edges { node }``). Each node: ``id``, ``name``, ``description``,
  ``status`` (ACTIVE or ARCHIVED), ``externalId { externalId }``, ``relToParent`` and
  ``subscriptions`` (``type``, ``user { email displayName }``, ``roles { name }``). The
  mock reads only the page size and cursor from a query, not the rest of it.

The capability map is the profile's EA_CAPABILITIES, the same as ServiceNow EA's. Each
capability's ``relBusinessCapabilityToApplication`` lists the Application fact sheets that
support it (the profile's EA_SUPPORT): ``id``, ``name`` and ``externalId { externalId }``, the
application's ServiceNow number, as when LeanIX is synced with the CMDB.

Quirks, each recorded as a defect:
- subscription-stale: subscriptions are maintained by hand and reviewed quarterly, so the
  Business Owner subscription keeps the person who filled the owner role at the last review;
- archived-capability: one capability retired from the map is still there, ARCHIVED;
- application-unsynced: one application was entered in LeanIX by hand under its product name,
  with no external ID, so it matches no system the CMDB knows by name or number.
"""

from __future__ import annotations

import base64
import importlib
import re
from datetime import timedelta

from fastapi import APIRouter, Form, Header, HTTPException, Request

from .base import SourceSystem, defect

API_TOKEN = "harness-leanix-api-token"  # noqa: S105 — a test token for a mock
ACCESS = "harness-leanix-access"  # noqa: S105 — a test token for a mock
REVIEW_DAYS = 90  # subscriptions are reviewed quarterly
ARCHIVED = ("ChequePrinting", "Cheque printing", "AccountsPayable")
UNSYNCED = ("HCM", "Workday")  # the application entered by hand, and the name it was given


class Leanix(SourceSystem):
    name = "leanix"
    title = "SAP LeanIX (GraphQL API)"

    def project(self):
        t = self.truth
        defects: list[dict] = []
        prof = importlib.import_module(f"harness.profiles.{t.profile}")
        caps = list(getattr(prof, "EA_CAPABILITIES", []))
        period = self.lag(REVIEW_DAYS)
        review = t.start + timedelta(days=((t.today - t.start).days // period) * period) if period else t.today
        ids = {c[0]: self.guid("fs", c[0]) for c in caps}
        sheets = {}
        systems = list(t.systems)
        support: dict[str, list[dict]] = {}
        for cap, sid in getattr(prof, "EA_SUPPORT", []):
            if sid not in t.systems:
                continue
            app = {
                "id": self.guid("app", sid),
                "name": t.systems[sid].name,
                "externalId": {"externalId": f"APM{1000 + systems.index(sid):07d}"},
            }
            if sid == UNSYNCED[0] and self.on("application-unsynced"):
                app = {"id": self.guid("app", sid), "name": UNSYNCED[1], "externalId": None}
                defects.append(
                    defect(
                        "application-unsynced",
                        self.name,
                        {"capability": cap, "system": sid},
                        "an application entered by hand, with no external ID and its product name",
                    )
                )
            support.setdefault(cap, []).append({"node": {"factSheet": app}})
        for cid, name, parent, role in caps:
            owner = t.holder(role, review) or t.holder(role, t.today)
            now = t.holder(role, t.today)
            if owner and now and owner.id != now.id:
                defects.append(
                    defect(
                        "subscription-stale",
                        self.name,
                        {"capability": cid, "shown": owner.id, "actual": now.id},
                        "the Business Owner subscription is from the last quarterly review",
                    )
                )
            subs = []
            if owner:
                subs.append(
                    {
                        "node": {
                            "type": "RESPONSIBLE",
                            "user": {"email": owner.email, "displayName": owner.name},
                            "roles": [{"name": "Business Owner"}],
                        }
                    }
                )
            sheets[ids[cid]] = self._sheet(ids[cid], cid, name, ids.get(parent or ""), "ACTIVE", subs, support.get(cid, []))
        if caps and self.on("archived-capability"):
            cid, name, parent = ARCHIVED
            fid = self.guid("fs", cid)
            sheets[fid] = self._sheet(fid, cid, name, ids.get(parent), "ARCHIVED", [])
            defects.append(defect("archived-capability", self.name, {"capability": cid}, "a retired capability kept as ARCHIVED"))
        return {"factsheets": sheets}, defects

    @staticmethod
    def _sheet(fid, cid, name, parent, status, subs, apps=()) -> dict:
        return {
            "id": fid,
            "name": name,
            "description": f"The {name.lower()} capability.",
            "status": status,
            "type": "BusinessCapability",
            "externalId": {"externalId": cid},
            "relToParent": {"edges": [{"node": {"factSheet": {"id": parent}}}] if parent else []},
            "subscriptions": {"edges": subs},
            "relBusinessCapabilityToApplication": {"edges": list(apps)},
        }

    def router(self) -> APIRouter:
        r = APIRouter()
        good = "Basic " + base64.b64encode(f"apitoken:{API_TOKEN}".encode()).decode()

        @r.post("/services/mtm/v1/oauth2/token")
        def token(grant_type: str = Form(...), authorization: str = Header("")):
            if authorization != good or grant_type != "client_credentials":
                raise HTTPException(401, detail={"error": "invalid_client"})
            return {"access_token": ACCESS, "token_type": "bearer", "expires_in": 3600}

        @r.post("/services/pathfinder/v1/graphql")
        async def graphql(request: Request, authorization: str = Header("")):
            if authorization != f"Bearer {ACCESS}":
                raise HTTPException(401, detail={"errors": [{"message": "unauthorized"}]})
            body = await request.json()
            query = body.get("query") or ""
            if "allFactSheets" not in query:
                return {"errors": [{"message": "the harness serves allFactSheets only"}]}
            kind = re.search(r"factSheetType:\s*(\w+)", query)
            size = int(m.group(1)) if (m := re.search(r"first:\s*(\d+)", query)) else 100
            nodes = [n for n in self.records()["factsheets"].values() if not kind or n["type"] == kind.group(1)]
            nodes.sort(key=lambda n: n["name"])
            start = int((body.get("variables") or {}).get("after") or 0)
            page = nodes[start : start + size]
            more = start + size < len(nodes)
            return {
                "data": {
                    "allFactSheets": {
                        "totalCount": len(nodes),
                        "pageInfo": {"hasNextPage": more, "endCursor": str(start + size) if more else None},
                        "edges": [{"node": {k: v for k, v in n.items() if k != "type"}} for n in page],
                    }
                }
            }

        return r
