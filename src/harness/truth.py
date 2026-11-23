"""The simulated company: one reproducible record of the truth, with history.

Every source system is a projection of this record (systems/), so what VeroMesh
should conclude is known: the truth is the answer key. People keep a history of
positions, so a system can lag behind the truth (a stale manager, an old title), and
the clock (clock.py) moves the company forward: hires, moves, leavers, reorganizations,
owner changes, a breaking schema change.

Generated from a profile (profiles/) and a seed; the same profile, size and seed always
give the same company.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta


def chance(seed: int, *keys) -> float:
    """A stable number in [0, 1) for these keys: the same quirk falls on the same entity
    every time a system is projected, whatever else changed."""
    h = hashlib.sha256(json.dumps([seed, *keys], default=str).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


@dataclass
class Position:
    """Where a person is from a date: unit, title, manager, roles."""

    start: date
    unit: str
    title: str
    manager: str | None
    roles: list[str]
    head: bool = False  # heads its unit


@dataclass
class Person:
    id: str  # the truth's own ID
    employee_id: str  # the HR system's worker ID
    given: str
    family: str
    email: str
    location: str
    worker_type: str  # employee | contractor
    hired: date
    history: list[Position]
    left: date | None = None
    npi: str | None = None  # clinicians' national provider ID

    def at(self, day: date) -> Position | None:
        """The position held on ``day`` (None before hire or after leaving)."""
        if day < self.hired or (self.left and day >= self.left):
            return None
        held = [p for p in self.history if p.start <= day]
        return held[-1] if held else None

    def active(self, day: date) -> bool:
        return self.at(day) is not None

    @property
    def name(self) -> str:
        return f"{self.given} {self.family}"


@dataclass
class Unit:
    id: str
    name: str
    parent: str | None
    cost_center: str


@dataclass
class System:
    id: str
    name: str
    kind: str
    vendor: str
    business_unit: str
    technical_unit: str
    criticality: str
    regulatory: list[str]
    business_owner: str | None
    technical_owner: str | None
    lifecycle: str = "operational"


@dataclass
class Asset:
    name: str  # catalog.schema.table
    layer: str
    source_system: str
    unit: str
    columns: list[tuple[str, str, str]]  # name, type, classification
    owner: str | None
    steward: str | None


@dataclass
class Kpi:
    id: str
    name: str
    unit: str
    measure: str
    asset: str
    owner: str | None


@dataclass
class Agent:
    id: str
    name: str
    acts_for: str
    client_id: str
    purpose: str


@dataclass
class Authority:
    id: str
    role: str
    action: str
    limit: int | None
    co_approval_over: int | None


@dataclass
class Truth:
    profile: str
    seed: int
    org_name: str
    domain: str
    start: date  # the simulation's day 0
    today: date  # the clock: what "now" is in the simulated company
    units: dict[str, Unit]
    people: dict[str, Person]
    roles: dict[str, str]
    authorities: list[Authority]
    systems: dict[str, System]
    assets: dict[str, Asset]
    lineage: list[tuple[str, str]]
    kpis: dict[str, Kpi]
    agents: dict[str, Agent]
    # How hard the systems make it: scale multiplies every quirk's rate (0: none, and no
    # sync lag, so every system agrees with the truth; 1: as described; 2: twice as many);
    # overrides set one quirk's rate by its defect kind (e.g. {"name-variant": 0.9}).
    difficulty: dict = field(default_factory=lambda: {"scale": 1.0, "overrides": {}})
    events: list[dict] = field(default_factory=list)  # scheduled, applied by the clock
    applied: list[dict] = field(default_factory=list)  # applied so far, in order

    # ------------------------------------------------------------------ views

    def active_people(self, day: date | None = None) -> list[Person]:
        day = day or self.today
        return [p for p in self.people.values() if p.active(day)]

    def head_of(self, unit: str, day: date | None = None) -> Person | None:
        """The person holding the unit's head title (its first) on ``day``."""
        day = day or self.today
        heads = [p for p in self.active_people(day) if (pos := p.at(day)) and pos.unit == unit and pos.head]
        return heads[0] if heads else None

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, indent=1)


def generate(
    profile: str = "healthcare", people: int = 2000, seed: int = 7, today: date | None = None, difficulty: dict | None = None
) -> Truth:
    """The company at ``today`` (default 1 December 2026), about ``people`` strong, with
    the clock's events for the 180 days after it scheduled."""
    prof = importlib.import_module(f"harness.profiles.{profile}")
    rng = random.Random(seed)
    today = today or date(2026, 12, 1)
    units = {u[0]: Unit(u[0], u[1], u[2], u[3]) for u in prof.UNITS}
    out: dict[str, Person] = {}
    emails: set[str] = set()
    counter = [0]

    def new_person(unit: str, title: str, manager: str | None, head: bool = False, hired: date | None = None) -> Person:
        counter[0] += 1
        n = counter[0]
        given, family = rng.choice(prof.GIVEN), rng.choice(prof.FAMILY)
        local = f"{given}.{family}".lower()
        email, k = f"{local}@{prof.DOMAIN}", 1
        while email in emails:
            k += 1
            email = f"{local}{k}@{prof.DOMAIN}"
        emails.add(email)
        hired = hired or today - timedelta(days=rng.randint(30, 3650))
        contractor = not head and rng.random() < 0.07
        roles = list(prof.TITLE_ROLES.get(title, []))
        p = Person(
            id=f"p{n:05d}",
            employee_id=f"{'C' if contractor else 'W'}{100000 + n}",
            given=given,
            family=family,
            email=email,
            location=rng.choice(prof.LOCATIONS),
            worker_type="contractor" if contractor else "employee",
            hired=hired,
            history=[Position(hired, unit, title, manager, roles, head)],
            npi=f"1{rng.randint(100000000, 999999999)}" if {"Clinician", "Prescriber", "Pharmacist"} & set(roles) else None,
        )
        out[p.id] = p
        return p

    # Heads first, top down, so each head reports to its parent unit's head.
    heads: dict[str, Person] = {}
    for uid, _name, parent, _cc, _share, titles in prof.UNITS:
        heads[uid] = new_person(uid, titles[0], heads[parent].id if parent else None, head=True)
    # Then each unit's staff, in teams of up to eight under a lead who reports to the head.
    for uid, _name, _parent, _cc, share, titles in prof.UNITS:
        staff_titles = titles[1:] or titles
        n = max(len(staff_titles), round(share * people) - 1)
        lead = None
        for i in range(n):
            title = staff_titles[i % len(staff_titles)] if i < len(staff_titles) else rng.choice(staff_titles)
            if i % 8 == 0:
                lead = new_person(uid, title, heads[uid].id)
            else:
                new_person(uid, title, lead.id)

    def with_role(role: str, unit: str | None = None) -> list[Person]:
        return [p for p in out.values() if role in p.history[-1].roles and (unit is None or p.history[-1].unit == unit)]

    systems = {}
    for sid, name, kind, vendor, bu, tu, crit, reg in prof.SYSTEMS:
        tech = with_role("TechnicalOwner", tu) or with_role("TechnicalOwner") or [heads[tu]]
        systems[sid] = System(sid, name, kind, vendor, bu, tu, crit, reg, heads[bu].id, rng.choice(tech).id)
    stewards = with_role("DataSteward") or [heads["DATA"]]
    assets = {a[0]: Asset(a[0], a[1], a[2], a[3], [tuple(c) for c in a[4]], heads[a[3]].id, rng.choice(stewards).id) for a in prof.ASSETS}
    kpis = {k[0]: Kpi(k[0], k[1], k[2], k[3], k[4], heads[k[2]].id) for k in prof.KPIS}
    agents = {}
    for aid, name, title, purpose in prof.AGENTS:
        principals = [p for p in out.values() if p.history[-1].title == title] or list(heads.values())
        agents[aid] = Agent(aid, name, rng.choice(principals).id, f"{chance(seed, 'agent', aid):.16f}"[2:18], purpose)
    authorities = [Authority(f"{role}-{action}", role, action, limit, co) for role, action, limit, co in prof.AUTHORITIES]
    truth = Truth(
        profile=profile,
        seed=seed,
        org_name=prof.NAME,
        domain=prof.DOMAIN,
        start=today,
        today=today,
        units=units,
        people=out,
        roles=dict(prof.ROLES),
        authorities=authorities,
        systems=systems,
        assets=assets,
        lineage=[tuple(e) for e in prof.LINEAGE],
        kpis=kpis,
        agents=agents,
    )
    if difficulty:
        truth.difficulty = {"scale": float(difficulty.get("scale", 1.0)), "overrides": dict(difficulty.get("overrides", {}))}
    from .clock import schedule

    truth.events = schedule(truth, rng, days=180)
    return truth
