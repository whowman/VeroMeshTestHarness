"""Microsoft Entra ID, through the Microsoft Graph API (v1.0).

What a connector reads: users (with manager), groups and their members, service
principals for AI agents (with their owners), and user changes through delta queries.
Sign-in is OAuth client credentials at /entra/{tenant}/oauth2/v2.0/token, as with the
real login endpoint. Paging follows @odata.nextLink; changes follow @odata.deltaLink.

Quirks, each recorded as a defect:
- name-variant: some people appear by the short name they go by (Bob for Robert);
- stale-manager: a few people still show their previous manager after a move;
- missing-mail: a few accounts have no mail address;
- leaver-account-enabled: some leavers' accounts stay enabled, still in their groups;
- contractor-only-here: contractors are in Entra ID and not in the HR system.
Provisioning from HR runs nightly, so Entra ID sees the company as of yesterday.
"""

from __future__ import annotations

import base64
import importlib
import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request

from .base import SourceSystem, defect

CLIENTS = {"veromesh-connector": "harness-secret"}  # client ID -> secret for the token endpoint
TOKEN = "harness-entra-token"  # noqa: S105 — a test token for a mock
DISABLED_FOR = 30  # days a leaver's account stays (disabled) before it is deleted


class Entra(SourceSystem):
    name = "entra"
    title = "Microsoft Entra ID (Microsoft Graph v1.0)"

    def project(self):
        t = self.truth
        prof = importlib.import_module(f"harness.profiles.{t.profile}")
        day = self.seen_as_of(1)
        users, groups, defects = {}, {}, []
        members: dict[str, set[str]] = {}
        for p in t.people.values():
            pos = p.at(day)
            gone = p.left and p.left <= day
            stays_enabled = bool(gone) and self.quirk(0.15, "leaver-enabled", p.id)
            if gone and (day - p.left).days > DISABLED_FOR and not stays_enabled:
                continue  # deleted
            if pos is None and not gone:
                continue  # not hired yet as of the last sync
            last = pos or p.history[-1]
            uid = self.guid("user", p.id)
            given = p.given
            if p.given in prof.NICKNAMES and self.quirk(0.5, "nickname", p.id):
                given = prof.NICKNAMES[p.given]
                defects.append(defect("name-variant", self.name, {"person": p.id}, f"{p.given} appears as {given}"))
            manager = last.manager
            prev = self.previous(p, day)
            if pos and prev and prev.manager != pos.manager and self.quirk(0.3, "stale-manager", p.id):
                manager = prev.manager
                defects.append(
                    defect(
                        "stale-manager",
                        self.name,
                        {"person": p.id, "shown": manager, "actual": pos.manager},
                        "manager not updated after a move",
                    )
                )
            mail = p.email
            if self.quirk(0.02, "no-mail", p.id):
                mail = None
                defects.append(defect("missing-mail", self.name, {"person": p.id}, "account has no mail address"))
            enabled = not gone or stays_enabled
            if gone and stays_enabled:
                defects.append(
                    defect(
                        "leaver-account-enabled",
                        self.name,
                        {"person": p.id, "left": p.left.isoformat()},
                        "account still enabled after the person left",
                    )
                )
            if p.worker_type == "contractor" and not gone:
                defects.append(defect("contractor-only-here", self.name, {"person": p.id}, "contractor in Entra ID, not in the HR system"))
            users[uid] = {
                "id": uid,
                "userPrincipalName": p.email,
                "mail": mail,
                "displayName": f"{given} {p.family}",
                "givenName": given,
                "surname": p.family,
                "jobTitle": last.title,
                "department": t.units[last.unit].name,
                "officeLocation": p.location,
                "employeeId": p.employee_id if p.worker_type == "employee" else None,
                "employeeType": "Contractor" if p.worker_type == "contractor" else "Employee",
                "companyName": t.org_name,
                "accountEnabled": enabled,
                "_person": p.id,
                "_manager": self.guid("user", manager) if manager else None,
                "_changed": (self.changed_on(p) + timedelta(days=1)).isoformat(),
                "_roles": last.roles if enabled else [],
            }
            if enabled:
                for r in last.roles:
                    members.setdefault(f"SG-{r}", set()).add(uid)
                members.setdefault(f"All {t.units[last.unit].name}", set()).add(uid)
        for gname, ids in sorted(members.items()):
            gid = self.guid("group", gname)
            security = gname.startswith("SG-")
            groups[gid] = {
                "id": gid,
                "displayName": gname,
                "description": t.roles.get(gname[3:], "") if security else f"Everyone in {gname[4:]}",
                "mailEnabled": not security,
                "securityEnabled": security,
                "groupTypes": [] if security else ["Unified"],
                "_members": sorted(ids),
            }
        sps = {}
        for a in t.agents.values():
            sid = self.guid("sp", a.id)
            sps[sid] = {
                "id": sid,
                "appId": a.client_id,
                "displayName": a.name,
                "servicePrincipalType": "Application",
                "notes": a.purpose,
                "tags": ["AIAgent"],
                "_owners": [self.guid("user", a.acts_for)],
            }
        return {"users": users, "groups": groups, "servicePrincipals": sps}, defects

    # ------------------------------------------------------------ the API

    def router(self) -> APIRouter:
        r = APIRouter()

        def bearer(authorization: str = Header("")):
            if authorization != f"Bearer {TOKEN}":
                raise HTTPException(
                    401, detail={"error": {"code": "InvalidAuthenticationToken", "message": "Access token is empty or invalid."}}
                )

        auth = [Depends(bearer)]

        @r.post("/{tenant}/oauth2/v2.0/token")
        def token(
            tenant: str, grant_type: str = Form(...), client_id: str = Form(...), client_secret: str = Form(""), scope: str = Form("")
        ):
            if grant_type != "client_credentials" or CLIENTS.get(client_id) != client_secret:
                raise HTTPException(
                    401, detail={"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."}
                )
            return {"token_type": "Bearer", "expires_in": 3599, "access_token": TOKEN}

        def page(request: Request, items: list[dict], top: int | None, skip: str | None, select: str | None) -> dict:
            top = min(top or 100, 999)
            start = int(skip or 0)
            chunk = items[start : start + top]
            out = {"@odata.context": f"{request.url_for('graph_root')}$metadata", "value": [public(i, select) for i in chunk]}
            if start + top < len(items):
                q = dict(request.query_params)
                q["$skiptoken"] = str(start + top)
                out["@odata.nextLink"] = str(request.url.replace_query_params(**q))
            return out

        @r.get("/v1.0/", name="graph_root")
        def graph_root():
            return {"@odata.context": "https://graph.microsoft.com/v1.0/$metadata"}

        @r.get("/v1.0/users", dependencies=auth)
        def users(request: Request):
            q = request.query_params
            items = sorted(self.records()["users"].values(), key=lambda u: u["userPrincipalName"])
            items = [u for u in items if matches(u, q.get("$filter"))]
            return page(request, items, int(q.get("$top", 0)) or None, q.get("$skiptoken"), q.get("$select"))

        @r.get("/v1.0/users/delta", dependencies=auth)
        def users_delta(request: Request):
            q = request.query_params
            since = None
            if q.get("$deltatoken"):
                since = date.fromisoformat(json.loads(base64.urlsafe_b64decode(q["$deltatoken"]))["since"])
            items = sorted(self.records()["users"].values(), key=lambda u: u["userPrincipalName"])
            if since:
                items = [u for u in items if date.fromisoformat(u["_changed"]) > since]
            out = page(request, items, int(q.get("$top", 0)) or None, q.get("$skiptoken"), q.get("$select"))
            if "@odata.nextLink" not in out:
                token = base64.urlsafe_b64encode(json.dumps({"since": self.seen_as_of(0).isoformat()}).encode()).decode()
                out["@odata.deltaLink"] = str(
                    request.url.remove_query_params(["$skiptoken"]).include_query_params(**{"$deltatoken": token})
                )
            return out

        @r.get("/v1.0/users/{uid}", dependencies=auth)
        def user(uid: str, request: Request):
            u = self.find("users", uid, key="userPrincipalName")
            return public(u, request.query_params.get("$select"))

        @r.get("/v1.0/users/{uid}/manager", dependencies=auth)
        def manager(uid: str):
            u = self.find("users", uid, key="userPrincipalName")
            m = self.records()["users"].get(u["_manager"] or "")
            if m is None:
                raise HTTPException(
                    404,
                    detail={
                        "error": {
                            "code": "Request_ResourceNotFound",
                            "message": "Resource 'manager' does not exist or one of its queried reference-property objects are not present.",
                        }
                    },
                )
            return {"@odata.type": "#microsoft.graph.user", **public(m, None)}

        @r.get("/v1.0/users/{uid}/memberOf", dependencies=auth)
        def member_of(uid: str):
            u = self.find("users", uid, key="userPrincipalName")
            return {
                "value": [
                    {"@odata.type": "#microsoft.graph.group", **public(g, None)}
                    for g in self.records()["groups"].values()
                    if u["id"] in g["_members"]
                ]
            }

        @r.get("/v1.0/groups", dependencies=auth)
        def groups(request: Request):
            q = request.query_params
            items = sorted(self.records()["groups"].values(), key=lambda g: g["displayName"])
            items = [g for g in items if matches(g, q.get("$filter"))]
            return page(request, items, int(q.get("$top", 0)) or None, q.get("$skiptoken"), q.get("$select"))

        @r.get("/v1.0/groups/{gid}/members", dependencies=auth)
        def group_members(gid: str, request: Request):
            g = self.find("groups", gid)
            us = self.records()["users"]
            items = [{"@odata.type": "#microsoft.graph.user", **us[m]} for m in g["_members"] if m in us]
            q = request.query_params
            return page(request, items, int(q.get("$top", 0)) or None, q.get("$skiptoken"), q.get("$select"))

        @r.get("/v1.0/servicePrincipals", dependencies=auth)
        def sps(request: Request):
            q = request.query_params
            items = sorted(self.records()["servicePrincipals"].values(), key=lambda s: s["displayName"])
            items = [s for s in items if matches(s, q.get("$filter"))]
            return page(request, items, int(q.get("$top", 0)) or None, q.get("$skiptoken"), q.get("$select"))

        @r.get("/v1.0/servicePrincipals/{sid}/owners", dependencies=auth)
        def sp_owners(sid: str):
            s = self.find("servicePrincipals", sid)
            us = self.records()["users"]
            return {"value": [{"@odata.type": "#microsoft.graph.user", **public(us[o], None)} for o in s["_owners"] if o in us]}

        return r

    def find(self, kind: str, ident: str, key: str | None = None) -> dict:
        items = self.records()[kind]
        if ident in items:
            return items[ident]
        if key:
            for i in items.values():
                if (i.get(key) or "").lower() == ident.lower():
                    return i
        raise HTTPException(
            404,
            detail={
                "error": {
                    "code": "Request_ResourceNotFound",
                    "message": f"Resource '{ident}' does not exist or one of its queried reference-property objects are not present.",
                }
            },
        )


def public(item: dict, select: str | None) -> dict:
    """The item as Graph returns it: no harness-only fields, and only $select's if given."""
    out = {k: v for k, v in item.items() if not k.startswith("_")}
    if select:
        keep = {s.strip() for s in select.split(",")} | {"id"}
        out = {k: v for k, v in out.items() if k in keep}
    return out


def matches(item: dict, flt: str | None) -> bool:
    """The $filter forms a connector uses: ``field eq 'value'`` joined by ``and``, and
    ``startswith(field,'value')``."""
    if not flt:
        return True
    for term in flt.split(" and "):
        term = term.strip()
        if term.startswith("startswith("):
            field, value = term[len("startswith(") : -1].split(",", 1)
            if not str(item.get(field.strip()) or "").lower().startswith(value.strip().strip("'").lower()):
                return False
            continue
        field, op, value = term.split(" ", 2)
        value = value.strip("'")
        got = item.get(field)
        want = {"true": True, "false": False}.get(value, value)
        if op != "eq":
            raise HTTPException(400, detail={"error": {"code": "BadRequest", "message": f"Unsupported operator {op} in the harness"}})
        if got != want and str(got).lower() != str(want).lower():
            return False
    return True
