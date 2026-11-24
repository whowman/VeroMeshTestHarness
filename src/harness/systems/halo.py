"""HaloITSM, through the Halo REST API.

Many organizations run HaloITSM instead of ServiceNow, or beside it for a business unit
(it is also a CRM: customers, sites and contracts). The mock follows Halo's public API:

- Sign-in: OAuth 2.0 client credentials at ``POST /auth/token`` (``grant_type``,
  ``client_id``, ``client_secret``, ``scope=all``; the tenant as ``?tenant=`` for hosted
  instances), then ``Authorization: Bearer <token>`` on ``/api/...``.
- Resources (Halo's casing): ``/api/Users`` (end users), ``/api/Agent`` (IT staff who work
  tickets), ``/api/Team`` (agent teams), ``/api/Asset`` (configuration items; business
  applications have the asset type "Business Application"), ``/api/Tickets``. One record:
  ``/api/<Resource>/{id}``.
- Lists page with ``pageinate=true``, ``page_size`` and ``page_no`` (from 1), and answer
  ``{"record_count": n, "page_no": p, "page_size": s, "<resource>": [...]}``; without
  ``pageinate`` the whole list comes back. ``search`` filters by name.

Quirks, each recorded as a defect:
- agent-still-enabled: Halo agents are licensed seats maintained by hand; an IT leaver's
  agent stays enabled until someone frees the licence (here, 14 days);
- halo-user-stale: end users are synced from Entra ID nightly, so a leaver is still an
  active user for a day;
- asset-owner-free-text: a business application's owner is a free-text custom field,
  sometimes a name, sometimes an email address.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request

from .base import SourceSystem, defect

CLIENT_ID = "veromesh-connector"
CLIENT_SECRET = "harness-secret"  # noqa: S105 — the mock's OAuth client secret
TOKEN = "harness-halo-token"  # noqa: S105 — a test token for a mock
AGENT_LICENCE_DAYS = 14  # an IT leaver's agent seat is freed by hand
USER_SYNC_DAYS = 1  # end users from Entra ID, nightly
IT_UNITS = ("IT", "INFRA", "APPS", "SEC")
RESOURCES = {"Users": "users", "Agent": "agents", "Team": "teams", "Asset": "assets", "Tickets": "tickets"}


class Halo(SourceSystem):
    name = "halo"
    title = "HaloITSM (Halo REST API)"

    def project(self):
        t = self.truth
        defects: list[dict] = []
        users, agents, teams, assets = {}, {}, {}, {}
        seen = self.seen_as_of(USER_SYNC_DAYS)
        ids = {p.id: i + 1 for i, p in enumerate(sorted(t.people.values(), key=lambda p: p.id))}
        for p in t.people.values():
            if p.hired > t.today:
                continue
            last = p.at(min(t.today, p.left or t.today)) or p.history[-1]
            inactive = bool(p.left and p.left <= seen)
            if p.left and p.left <= t.today and not inactive:
                defects.append(defect("halo-user-stale", self.name, {"person": p.id}, "a leaver still an active Halo user"))
            users[ids[p.id]] = {
                "id": ids[p.id],
                "name": p.name,
                "emailaddress": p.email,
                "site_name": p.location,
                "department_name": t.units[last.unit].name,
                "jobtitle": last.title,
                "inactive": inactive,
            }
            if last.unit in IT_UNITS:
                left_long = p.left and p.left + timedelta(days=self.lag(AGENT_LICENCE_DAYS)) <= t.today
                disabled = bool(left_long)
                if p.left and p.left <= t.today and not disabled:
                    defects.append(
                        defect(
                            "agent-still-enabled",
                            self.name,
                            {"person": p.id, "left": p.left.isoformat()},
                            "an IT leaver's Halo agent seat is still enabled",
                        )
                    )
                agents[ids[p.id]] = {
                    "id": ids[p.id],
                    "name": p.name,
                    "email": p.email,
                    "team": t.units[last.unit].name,
                    "isdisabled": disabled,
                }
        for i, unit in enumerate(IT_UNITS, start=1):
            teams[i] = {"id": i, "name": t.units[unit].name, "inactive": False}
        for i, s in enumerate(t.systems.values(), start=1):
            owner = t.people.get(s.business_owner or "")
            free = None
            if owner:
                free = owner.email if self.quirk(0.4, "asset-owner-free-text", s.id) else owner.name
                if free == owner.email:
                    defects.append(
                        defect("asset-owner-free-text", self.name, {"system": s.id, "value": free}, "the owner is an email in a text field")
                    )
            assets[i] = {
                "id": i,
                "inventory_number": f"BA-{i:04d}",
                "key_field": s.name,
                "assettype_name": "Business Application",
                "inactive": s.lifecycle == "retired",
                "fields": [{"name": "Business Owner", "value": free or ""}, {"name": "Criticality", "value": s.criticality}],
            }
        return {"users": users, "agents": agents, "teams": teams, "assets": assets, "tickets": {}}, defects

    def router(self) -> APIRouter:
        r = APIRouter()

        def signed_in(authorization: str = Header("")):
            if authorization != f"Bearer {TOKEN}":
                raise HTTPException(401, detail="Unauthorized")

        @r.post("/auth/token")
        def token(
            grant_type: str = Form(...),
            client_id: str = Form(""),
            client_secret: str = Form(""),
            scope: str = Form("all"),
        ):
            if grant_type != "client_credentials" or (client_id, client_secret) != (CLIENT_ID, CLIENT_SECRET):
                raise HTTPException(400, detail={"error": "invalid_client"})
            return {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600, "scope": scope}

        def listing(resource: str):
            key = RESOURCES[resource]

            def endpoint(request: Request):
                q = request.query_params
                rows = list(self.records()[key].values())
                if q.get("search"):
                    s = q["search"].lower()
                    rows = [x for x in rows if s in str(x.get("name") or x.get("key_field") or "").lower()]
                total = len(rows)
                if q.get("pageinate", "").lower() == "true":
                    size, page = int(q.get("page_size", 50)), max(1, int(q.get("page_no", 1)))
                    rows = rows[(page - 1) * size : page * size]
                    return {"record_count": total, "page_no": page, "page_size": size, key: rows}
                return {"record_count": total, key: rows}

            return endpoint

        def one(resource: str):
            key = RESOURCES[resource]

            def endpoint(rid: int):
                row = self.records()[key].get(rid)
                if row is None:
                    raise HTTPException(404, detail="Not Found")
                return row

            return endpoint

        for resource in RESOURCES:
            r.add_api_route(f"/api/{resource}", listing(resource), methods=["GET"], dependencies=[Depends(signed_in)])
            r.add_api_route(f"/api/{resource}/{{rid}}", one(resource), methods=["GET"], dependencies=[Depends(signed_in)])
        return r
