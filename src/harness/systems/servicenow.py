"""ServiceNow, through the Table API (/api/now/table/{table}).

What a connector reads: users (sys_user) and departments synced from HR, assignment
groups and their members, the CMDB (business applications, servers and databases, and
the relationships between them), and incidents and change requests. Sign-in is basic
authentication or an OAuth token from /oauth_token.do. Queries use ServiceNow's
encoded query (sysparm_query); paging uses sysparm_limit and sysparm_offset, with
X-Total-Count and a Link header, as the real API does.

Quirks, each recorded as a defect:
- stale-user: HR syncs into ServiceNow weekly, so movers show their old title and
  department for up to a week;
- leaver-still-active: for the same reason, a leaver stays active for up to a week;
- no-technical-owner: one business application has no managed_by;
- duplicate-ci: one application is in the CMDB twice under different names;
- owner-is-leaver: the CMDB's owners are updated monthly, so an owner who left still
  owns the application (or capability) for a while;
- capability-owner-vacant: a capability whose owner role no one fills has no owned_by.

Enterprise Architecture's business capability map is cmdb_ci_business_capability (name,
parent, owned_by), from the profile's EA_CAPABILITIES.
"""

from __future__ import annotations

import base64
import importlib
import re
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, Response

from .base import SourceSystem, defect

USERS = {"admin": "harness"}  # basic auth for the mock
CLIENT_SECRET = "harness-secret"  # noqa: S105 — the mock's OAuth client secret
TOKEN = "harness-servicenow-token"  # noqa: S105 — a test token for a mock
USER_SYNC_DAYS = 7  # HR -> sys_user
CMDB_OWNER_SYNC_DAYS = 30  # owners in the CMDB are reviewed monthly
NO_TECH_OWNER = "SEM"  # the application that lost its managed_by
DUPLICATE = ("EHR", "Epic")  # an application entered twice, under this second name
INSTALLED, RETIRED = "1", "7"
CRITICALITY = {
    "1 - most critical": "1 - most critical",
    "2 - somewhat critical": "2 - somewhat critical",
    "3 - less critical": "3 - less critical",
}


def ts(d: date, hour: int = 2) -> str:
    return datetime.combine(d, time(hour)).strftime("%Y-%m-%d %H:%M:%S")


class ServiceNow(SourceSystem):
    name = "servicenow"
    title = "ServiceNow (Table API)"

    def ref(self, table: str, sid: str | None) -> dict | str:
        return {"link": f"/api/now/table/{table}/{sid}", "value": sid} if sid else ""

    def project(self):
        t = self.truth
        day = self.seen_as_of(USER_SYNC_DAYS)
        defects: list[dict] = []
        tables: dict[str, dict[str, dict]] = {
            name: {}
            for name in (
                "sys_user",
                "cmn_department",
                "sys_user_group",
                "sys_user_grmember",
                "cmdb_ci_business_app",
                "cmdb_ci_business_capability",
                "cmdb_ci_server",
                "cmdb_ci_db_instance",
                "cmdb_rel_ci",
                "incident",
                "change_request",
            )
        }
        user_id = lambda pid: self.sys_id("user", pid)  # noqa: E731
        for u in t.units.values():
            sid = self.sys_id("dept", u.id)
            tables["cmn_department"][sid] = {
                "sys_id": sid,
                "name": u.name,
                "id": u.id,
                "cost_center": u.cost_center,
                "parent": self.ref("cmn_department", self.sys_id("dept", u.parent) if u.parent else None),
                "sys_updated_on": ts(t.start),
            }
        for p in t.people.values():
            pos = p.at(day)
            if pos is None and not (p.left and p.left <= day):
                continue  # not synced yet
            last = pos or p.history[-1]
            now = p.at(t.today)
            if pos and p.left and p.left <= t.today:
                defects.append(
                    defect(
                        "leaver-still-active",
                        self.name,
                        {"person": p.id, "left": p.left.isoformat()},
                        "left, but still active until the weekly HR sync",
                    )
                )
            if pos and now and (now.title, now.unit) != (pos.title, pos.unit):
                defects.append(
                    defect(
                        "stale-user",
                        self.name,
                        {"person": p.id, "shown": [pos.unit, pos.title], "actual": [now.unit, now.title]},
                        "title or department not yet synced from HR",
                    )
                )
            sid = user_id(p.id)
            tables["sys_user"][sid] = {
                "sys_id": sid,
                "user_name": p.email.split("@")[0],
                "first_name": p.given,
                "last_name": p.family,
                "name": p.name,
                "email": p.email,
                "employee_number": p.employee_id,
                "title": last.title,
                "department": self.ref("cmn_department", self.sys_id("dept", last.unit)),
                "manager": self.ref("sys_user", user_id(last.manager) if last.manager else None),
                "location": p.location,
                "active": "false" if p.left and p.left <= day else "true",
                "sys_updated_on": ts(min(self.changed_on(p) + timedelta(days=self.lag(USER_SYNC_DAYS)), t.today)),
            }
        # Assignment groups: one per technical unit, its members the unit's people.
        for unit in sorted({s.technical_unit for s in t.systems.values()}):
            gid = self.sys_id("group", unit)
            tables["sys_user_group"][gid] = {
                "sys_id": gid,
                "name": f"{t.units[unit].name} Support",
                "manager": self.ref("sys_user", user_id(h.id) if (h := t.head_of(unit, day)) else None),
                "active": "true",
                "sys_updated_on": ts(t.start),
            }
            for p in t.active_people(day):
                if p.at(day).unit == unit:
                    mid = self.sys_id("grmember", unit, p.id)
                    tables["sys_user_grmember"][mid] = {
                        "sys_id": mid,
                        "group": self.ref("sys_user_group", gid),
                        "user": self.ref("sys_user", user_id(p.id)),
                    }
        # The CMDB. Owners come from a monthly review: the owner as of the last review,
        # unless that person has since left (then the leaver stays named until the next).
        period = self.lag(CMDB_OWNER_SYNC_DAYS)
        review = t.start + timedelta(days=((t.today - t.start).days // period) * period) if period else t.today
        owners_then = self._owners_at(review)
        for s in t.systems.values():
            aid = self.sys_id("app", s.id)
            business, technical = owners_then.get(s.id, (s.business_owner, s.technical_owner))
            for field, who in (("owned_by", business), ("managed_by", technical)):
                if who and t.people[who].left and t.people[who].left <= t.today:
                    defects.append(
                        defect(
                            "owner-is-leaver",
                            self.name,
                            {"system": s.id, "field": field, "person": who},
                            "the CMDB names an owner who has left",
                        )
                    )
            if s.id == NO_TECH_OWNER and self.on("no-technical-owner"):
                technical = None
                defects.append(defect("no-technical-owner", self.name, {"system": s.id}, "business application with no managed_by"))
            row = {
                "sys_id": aid,
                "name": s.name,
                "number": f"APM{1000 + list(t.systems).index(s.id):07d}",
                "vendor": s.vendor,
                "u_kind": s.kind,
                "owned_by": self.ref("sys_user", user_id(business) if business else None),
                "managed_by": self.ref("sys_user", user_id(technical) if technical else None),
                "business_criticality": CRITICALITY[s.criticality],
                "install_status": RETIRED if s.lifecycle == "retired" else INSTALLED,
                "u_regulatory_scope": ",".join(s.regulatory),
                "support_group": self.ref("sys_user_group", self.sys_id("group", s.technical_unit)),
                "sys_updated_on": ts(review),
            }
            tables["cmdb_ci_business_app"][aid] = row
            for i, kind in enumerate(("app", "app", "db")):
                table = "cmdb_ci_db_instance" if kind == "db" else "cmdb_ci_server"
                cid = self.sys_id("ci", s.id, i)
                tables[table][cid] = {
                    "sys_id": cid,
                    "name": f"{s.id.lower()}-{kind}{i + 1:02d}",
                    "u_application": s.id,
                    "install_status": row["install_status"],
                    "sys_updated_on": ts(t.start),
                }
                rid = self.sys_id("rel", s.id, i)
                tables["cmdb_rel_ci"][rid] = {
                    "sys_id": rid,
                    "parent": self.ref("cmdb_ci_business_app", aid),
                    "child": self.ref(table, cid),
                    "type": {"value": "Runs on::Runs", "display_value": "Runs on::Runs"},
                }
        # Enterprise Architecture: the business capability map, its owners from the same
        # monthly review (a vacant owner role leaves owned_by empty).
        prof = importlib.import_module(f"harness.profiles.{t.profile}")
        for i, (cid_, name, parent, role) in enumerate(getattr(prof, "EA_CAPABILITIES", [])):
            owner = t.holder(role, review) or t.holder(role, t.today)
            if owner is None:
                defects.append(
                    defect("capability-owner-vacant", self.name, {"capability": cid_, "role": role}, "no one fills the owner role")
                )
            elif owner.left and owner.left <= t.today:
                defects.append(
                    defect(
                        "owner-is-leaver",
                        self.name,
                        {"capability": cid_, "person": owner.id},
                        "the capability map names an owner who has left",
                    )
                )
            sid = self.sys_id("cap", cid_)
            tables["cmdb_ci_business_capability"][sid] = {
                "sys_id": sid,
                "name": name,
                "number": f"BCAP{1000 + i:07d}",
                "short_description": f"The {name.lower()} capability.",
                "parent": self.ref("cmdb_ci_business_capability", self.sys_id("cap", parent) if parent else None),
                "owned_by": self.ref("sys_user", user_id(owner.id) if owner else None),
                "install_status": INSTALLED,
                "sys_updated_on": ts(review),
            }
        if self.on("duplicate-ci"):
            dup_of, dup_name = DUPLICATE
            did = self.sys_id("app-duplicate", dup_of)
            tables["cmdb_ci_business_app"][did] = {
                **tables["cmdb_ci_business_app"][self.sys_id("app", dup_of)],
                "sys_id": did,
                "name": dup_name,
                "number": "APM0009999",
            }
            defects.append(
                defect(
                    "duplicate-ci",
                    self.name,
                    {"system": dup_of, "names": [t.systems[dup_of].name, dup_name]},
                    "the same application twice in the CMDB",
                )
            )
        # Change requests for the changes the clock applied, and a steady trickle of incidents.
        for i, e in enumerate(a for a in t.applied if a["kind"] in ("schema-change", "retire", "reorg")):
            cid = self.sys_id("chg", i)
            what = {
                "schema-change": f"Rename column {e.get('renamed', ['', ''])[0]} to {e.get('renamed', ['', ''])[1]} in {e.get('asset')}",
                "retire": f"Retire {t.systems[e['system']].name}" if e.get("system") else "",
                "reorg": f"Move {e.get('unit')} under {e.get('to')}",
            }[e["kind"]]
            system = "LAKE" if e["kind"] == "schema-change" else e.get("system", "ITSM")
            tables["change_request"][cid] = {
                "sys_id": cid,
                "number": f"CHG{30000 + i:07d}",
                "short_description": what,
                "cmdb_ci": self.ref("cmdb_ci_business_app", self.sys_id("app", system)),
                "state": "3",
                "type": "normal",
                "opened_at": ts(date.fromisoformat(e["date"]) - timedelta(days=7)),
                "closed_at": ts(date.fromisoformat(e["date"])),
                "sys_updated_on": ts(date.fromisoformat(e["date"])),
            }
        for n in range(max(0, (t.today - t.start).days) // 2 + 20):
            s = sorted(t.systems)[n % len(t.systems)]
            opened = t.start + timedelta(days=n // 2 - 10)
            iid = self.sys_id("inc", n)
            tables["incident"][iid] = {
                "sys_id": iid,
                "number": f"INC{10000 + n:07d}",
                "short_description": f"{t.systems[s].name}: slow response",
                "cmdb_ci": self.ref("cmdb_ci_business_app", self.sys_id("app", s)),
                "assignment_group": self.ref("sys_user_group", self.sys_id("group", t.systems[s].technical_unit)),
                "priority": str(1 + n % 4),
                "state": "7" if opened < t.today - timedelta(days=3) else "2",
                "opened_at": ts(opened, 9),
                "sys_updated_on": ts(opened, 9),
            }
        return tables, defects

    def _owners_at(self, day: date) -> dict:
        """Who owned each system on ``day``: the truth's owners, unless the clock's events
        after ``day`` changed them (then as they were before)."""
        out = {}
        for s in self.truth.systems.values():
            business, technical = s.business_owner, s.technical_owner
            for e in reversed(self.truth.applied):
                if date.fromisoformat(e["date"]) <= day:
                    break
                for v in e.get("vacated", []):
                    if v.get("system") == s.id:
                        if v["field"] == "business_owner":
                            business = e["person"]
                        else:
                            technical = e["person"]
                for v in e.get("owns", []):
                    if v.get("system") == s.id:
                        if v["field"] == "business_owner":
                            business = None
                        else:
                            technical = None
            out[s.id] = (business, technical)
        return out

    # ------------------------------------------------------------ the API

    def router(self) -> APIRouter:
        r = APIRouter()

        def signed_in(authorization: str = Header("")):
            if authorization == f"Bearer {TOKEN}":
                return
            if authorization.startswith("Basic "):
                user, _, pw = base64.b64decode(authorization[6:]).decode().partition(":")
                if USERS.get(user) == pw:
                    return
            raise HTTPException(
                401,
                detail={
                    "error": {"message": "User Not Authenticated", "detail": "Required to provide Auth information"},
                    "status": "failure",
                },
            )

        auth = [Depends(signed_in)]

        @r.post("/oauth_token.do")
        def oauth(
            grant_type: str = Form(...),
            client_id: str = Form(""),
            client_secret: str = Form(""),
            username: str = Form(""),
            password: str = Form(""),
        ):
            ok = (grant_type == "password" and USERS.get(username) == password) or (
                grant_type == "client_credentials" and client_secret == CLIENT_SECRET
            )
            if not ok:
                raise HTTPException(401, detail={"error": "access_denied", "error_description": "access_denied"})
            return {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 1799, "scope": "useraccount"}

        @r.get("/api/now/table/{table}", dependencies=auth)
        def table(table: str, request: Request, response: Response):
            rows = self._table(table)
            q = request.query_params
            rows = query(rows, q.get("sysparm_query", ""))
            total = len(rows)
            limit = int(q.get("sysparm_limit", 10000))
            offset = int(q.get("sysparm_offset", 0))
            chunk = rows[offset : offset + limit]
            response.headers["X-Total-Count"] = str(total)
            if offset + limit < total:
                nxt = request.url.include_query_params(sysparm_offset=offset + limit, sysparm_limit=limit)
                response.headers["Link"] = f'<{nxt}>;rel="next"'
            return {"result": [shape(x, q.get("sysparm_fields"), q.get("sysparm_display_value"), self) for x in chunk]}

        @r.get("/api/now/table/{table}/{sys_id}", dependencies=auth)
        def record(table: str, sys_id: str, request: Request):
            rows = self.records().get(table)
            if rows is None or sys_id not in rows:
                raise HTTPException(
                    404,
                    detail={
                        "error": {"message": "No Record found", "detail": "Record doesn't exist or ACL restricts the record retrieval"},
                        "status": "failure",
                    },
                )
            q = request.query_params
            return {"result": shape(rows[sys_id], q.get("sysparm_fields"), q.get("sysparm_display_value"), self)}

        return r

    def _table(self, table: str) -> list[dict]:
        rows = self.records().get(table)
        if rows is None:
            raise HTTPException(400, detail={"error": {"message": "Invalid table " + table, "detail": None}, "status": "failure"})
        return sorted(rows.values(), key=lambda x: x["sys_id"])

    def display(self, ref: dict) -> str:
        table = ref["link"].split("/api/now/table/")[1].split("/")[0]
        row = self.records().get(table, {}).get(ref["value"], {})
        return row.get("name") or row.get("number") or ref["value"]


def shape(row: dict, fields: str | None, display: str | None, sn: ServiceNow) -> dict:
    out = dict(row)
    if fields:
        keep = [f.strip() for f in fields.split(",")]
        out = {k: v for k, v in out.items() if k in keep}
    if display in ("true", "all"):
        for k, v in list(out.items()):
            if isinstance(v, dict) and "link" in v:
                out[k] = sn.display(v) if display == "true" else {**v, "display_value": sn.display(v)}
    return out


_TERM = re.compile(r"^(?P<f>[a-z_.]+)(?P<op>ISNOTEMPTY|ISEMPTY|STARTSWITH|LIKE|!=|>=|<=|=|>|<)(?P<v>.*)$")


def _value(row: dict, field: str):
    v = row.get(field.split(".")[0], "")
    return v["value"] if isinstance(v, dict) else v


def query(rows: list[dict], q: str) -> list[dict]:
    """ServiceNow's encoded query, the parts connectors use: terms joined by ^ (and) and
    ^OR (or), the operators = != > >= < <= LIKE STARTSWITH ISEMPTY ISNOTEMPTY, and
    ORDERBY / ORDERBYDESC."""
    if not q:
        return rows
    order = []
    groups: list[list[str]] = []
    for part in q.split("^"):
        if part.startswith("ORDERBYDESC"):
            order.append((part[11:], True))
        elif part.startswith("ORDERBY"):
            order.append((part[7:], False))
        elif part.startswith("OR") and groups:
            groups[-1].append(part[2:])
        elif part:
            groups.append([part])

    def holds(row: dict, term: str) -> bool:
        m = _TERM.match(term)
        if not m:
            raise HTTPException(400, detail={"error": {"message": f"Unsupported query term in the harness: {term}"}, "status": "failure"})
        got, op, want = str(_value(row, m["f"])), m["op"], m["v"]
        return {
            "=": got == want,
            "!=": got != want,
            ">": got > want,
            ">=": got >= want,
            "<": got < want,
            "<=": got <= want,
            "LIKE": want.lower() in got.lower(),
            "STARTSWITH": got.lower().startswith(want.lower()),
            "ISEMPTY": got == "",
            "ISNOTEMPTY": got != "",
        }[op]

    out = [r for r in rows if all(any(holds(r, t) for t in g) for g in groups)]
    for field, desc in reversed(order):
        out.sort(key=lambda r: str(_value(r, field)), reverse=desc)
    return out
