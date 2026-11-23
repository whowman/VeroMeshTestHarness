"""The clock: moves the simulated company forward in time.

Events are scheduled when the company is generated: the everyday churn (hires, moves,
leavers) and a script of the changes the gates must catch (a unit head leaving and the
post staying vacant, a reorganization, a breaking schema change, a system retired).
Everyday events name no person: each picks one when it is applied, from the company as
it is that day, by a stable number, so the same seed always gives the same story.

Each source system sees the change when it re-projects the truth (with its own lag and
quirks); VeroMesh has to notice it from there.
"""

from __future__ import annotations

import random
from datetime import timedelta

from .truth import Person, Position, Truth, chance

# The script: (day, event). Days count from the simulation's start.
SCRIPT = [
    (20, {"kind": "head-leaves", "unit": "AP"}),  # the Accounts Payable Manager leaves
    (45, {"kind": "reorg", "unit": "CINF", "parent": "DATA"}),  # Clinical Informatics moves under Data
    (50, {"kind": "head-hired", "unit": "AP"}),  # a new AP Manager, 30 days later
    (60, {"kind": "schema-change", "asset": "finance.bronze.erp_invoices", "rename": ["amount", "invoice_amount"]}),
    (120, {"kind": "retire", "system": "RCMS"}),
]
MONTHLY = {"hire": 0.010, "move": 0.010, "leave": 0.010}  # of the headcount, a month
REASSIGN_AFTER = 14  # days a post's ownership stays vacant after its holder leaves


def schedule(truth: Truth, rng: random.Random, days: int = 180) -> list[dict]:
    headcount = len(truth.people)
    events = [{"day": d, **e} for d, e in SCRIPT if d <= days]
    for kind, rate in MONTHLY.items():
        per_day = headcount * rate / 30
        for day in range(1, days + 1):
            n = int(per_day) + (1 if rng.random() < per_day - int(per_day) else 0)
            events += [{"day": day, "kind": kind, "pick": rng.random()} for _ in range(n)]
    events.sort(key=lambda e: (e["day"], e["kind"]))
    return events


def advance(truth: Truth, days: int) -> list[dict]:
    """Move the clock ``days`` forward, applying every event due; returns them as applied
    (with the people they picked)."""
    until = truth.today + timedelta(days=days)
    done = []
    while True:
        due = [e for e in truth.events if truth.start + timedelta(days=e["day"]) <= until]
        if not due:
            break
        e = due[0]
        truth.events.remove(e)
        day = truth.start + timedelta(days=e["day"])
        applied = _apply(truth, e, day)
        if applied is not None:
            applied = {"date": day.isoformat(), **applied}
            truth.applied.append(applied)
            done.append(applied)
    truth.today = until
    return done


def _staff(truth: Truth, day, unit: str | None = None) -> list[Person]:
    return sorted(
        (p for p in truth.active_people(day) if not p.at(day).head and (unit is None or p.at(day).unit == unit)),
        key=lambda p: p.id,
    )


def _pick(items: list, x: float):
    return items[min(len(items) - 1, int(x * len(items)))] if items else None


def _move(p: Person, day, **changes) -> None:
    cur = p.at(day)
    p.history.append(
        Position(
            day,
            changes.get("unit", cur.unit),
            changes.get("title", cur.title),
            changes.get("manager", cur.manager),
            changes.get("roles", cur.roles),
            changes.get("head", cur.head),
        )
    )


def _leave(truth: Truth, p: Person, day) -> dict:
    """``p`` leaves: reports move to p's manager; what p owned waits for a new owner."""
    pos = p.at(day)
    p.left = day
    for q in truth.active_people(day):
        if q.at(day).manager == p.id:
            _move(q, day, manager=pos.manager)
    vacated = []
    for s in truth.systems.values():
        for attr in ("business_owner", "technical_owner"):
            if getattr(s, attr) == p.id:
                setattr(s, attr, None)
                vacated.append({"system": s.id, "field": attr})
    for a in truth.assets.values():
        for attr in ("owner", "steward"):
            if getattr(a, attr) == p.id:
                setattr(a, attr, None)
                vacated.append({"asset": a.name, "field": attr})
    for k in truth.kpis.values():
        if k.owner == p.id:
            k.owner = None
            vacated.append({"kpi": k.id, "field": "owner"})
    if vacated and not pos.head:  # a head's posts wait for the new head (the script)
        truth.events.append({"day": (day - truth.start).days + REASSIGN_AFTER, "kind": "reassign", "unit": pos.unit, "vacated": vacated})
    orphaned = [a.id for a in truth.agents.values() if a.acts_for == p.id]
    return {"person": p.id, "unit": pos.unit, "title": pos.title, "vacated": vacated, "agentsOrphaned": orphaned}


def _apply(truth: Truth, e: dict, day) -> dict | None:
    kind = e["kind"]
    if kind == "leave":
        p = _pick(_staff(truth, day), e["pick"])
        return p and {"kind": "leave", **_leave(truth, p, day)}
    if kind == "move":
        p = _pick(_staff(truth, day), e["pick"])
        if p is None:
            return None
        units = sorted(u for u in truth.units if u != p.at(day).unit)
        to = _pick(units, chance(truth.seed, "move-to", p.id, day))
        head = truth.head_of(to, day)
        _move(p, day, unit=to, manager=head.id if head else None)
        return {"kind": "move", "person": p.id, "from": p.history[-2].unit, "to": to}
    if kind == "hire":
        unit = _pick(sorted(truth.units), e["pick"])
        mates = _staff(truth, day, unit)
        template = _pick(mates, chance(truth.seed, "hire-as", unit, day)) if mates else None
        head = truth.head_of(unit, day)
        title = template.at(day).title if template else "Analyst"
        p = _new_person(truth, unit, title, head.id if head else None, day, template.at(day).roles if template else [])
        return {"kind": "hire", "person": p.id, "unit": unit, "title": title}
    if kind == "head-leaves":
        head = truth.head_of(e["unit"], day)
        return head and {"kind": "leave", **_leave(truth, head, day)}
    if kind == "head-hired":
        unit = truth.units[e["unit"]]
        parent_head = truth.head_of(unit.parent, day) if unit.parent else None
        title, roles = _head_title(truth, e["unit"])
        p = _new_person(truth, e["unit"], title, parent_head.id if parent_head else None, day, roles, head=True)
        staff_leads = [
            q
            for q in truth.active_people(day)
            if q.at(day).unit == e["unit"]
            and q.at(day).manager in (None, parent_head and parent_head.id)
            and q.id != p.id
            and not q.at(day).head
        ]
        for q in staff_leads:
            _move(q, day, manager=p.id)
        taken = _take_vacancies(truth, e["unit"], p)
        return {"kind": "hire", "person": p.id, "unit": e["unit"], "title": title, "head": True, "owns": taken}
    if kind == "reassign":
        head = truth.head_of(e["unit"], day)
        if head is None:
            return None
        taken = _take_vacancies(truth, e["unit"], head, only=e["vacated"])
        return {"kind": "reassign", "to": head.id, "owns": taken}
    if kind == "reorg":
        unit = truth.units[e["unit"]]
        old = unit.parent
        unit.parent = e["parent"]
        head, new_parent_head = truth.head_of(e["unit"], day), truth.head_of(e["parent"], day)
        if head and new_parent_head:
            _move(head, day, manager=new_parent_head.id)
        return {"kind": "reorg", "unit": e["unit"], "from": old, "to": e["parent"]}
    if kind == "schema-change":
        a = truth.assets[e["asset"]]
        old, new = e["rename"]
        a.columns = [(new if c[0] == old else c[0], c[1], c[2]) for c in a.columns]
        downstream = sorted({t for s, t in truth.lineage if s == a.name})
        return {"kind": "schema-change", "asset": a.name, "renamed": e["rename"], "downstream": downstream}
    if kind == "retire":
        truth.systems[e["system"]].lifecycle = "retired"
        return {"kind": "retire", "system": e["system"]}
    raise ValueError(f"unknown event kind {kind}")


def _head_title(truth: Truth, unit: str) -> tuple[str, list[str]]:
    import importlib

    prof = importlib.import_module(f"harness.profiles.{truth.profile}")
    title = next(u[5][0] for u in prof.UNITS if u[0] == unit)
    return title, list(prof.TITLE_ROLES.get(title, []))


def _take_vacancies(truth: Truth, unit: str, p: Person, only: list[dict] | None = None) -> list[dict]:
    taken = []
    for s in truth.systems.values():
        for attr, u in (("business_owner", s.business_unit), ("technical_owner", s.technical_unit)):
            if getattr(s, attr) is None and u == unit and (only is None or {"system": s.id, "field": attr} in only):
                setattr(s, attr, p.id)
                taken.append({"system": s.id, "field": attr})
    for a in truth.assets.values():
        for attr in ("owner", "steward"):
            if getattr(a, attr) is None and a.unit == unit and (only is None or {"asset": a.name, "field": attr} in only):
                setattr(a, attr, p.id)
                taken.append({"asset": a.name, "field": attr})
    for k in truth.kpis.values():
        if k.owner is None and k.unit == unit:
            k.owner = p.id
            taken.append({"kpi": k.id, "field": "owner"})
    return taken


def _new_person(truth: Truth, unit, title, manager, day, roles, head=False) -> Person:
    import importlib

    prof = importlib.import_module(f"harness.profiles.{truth.profile}")
    n = len(truth.people) + 1
    given = _pick(prof.GIVEN, chance(truth.seed, "given", n))
    family = _pick(prof.FAMILY, chance(truth.seed, "family", n))
    local = f"{given}.{family}".lower()
    taken = {q.email for q in truth.people.values()}
    email, k = f"{local}@{truth.domain}", 1
    while email in taken:
        k += 1
        email = f"{local}{k}@{truth.domain}"
    p = Person(
        id=f"p{n:05d}",
        employee_id=f"W{100000 + n}",
        given=given,
        family=family,
        email=email,
        location=_pick(prof.LOCATIONS, chance(truth.seed, "loc", n)),
        worker_type="employee",
        hired=day,
        history=[Position(day, unit, title, manager, list(roles), head)],
    )
    truth.people[p.id] = p
    return p
