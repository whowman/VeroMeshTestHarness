"""The harness as one web service: each simulated system under its own path, and the
harness's own controls under /harness.

    /entra/...        Microsoft Graph (token endpoint at /entra/{tenant}/oauth2/v2.0/token)
    /servicenow/...   ServiceNow Table API (/servicenow/api/now/table/{table})
    /harness/status   the simulated date, headcount and systems
    /harness/clock    POST {"days": n}: move the company forward; returns the events applied
    /harness/answer-key  what VeroMesh should find: every system's defects, the events so far,
                      and the truth's owners, roles and authorities
    /harness/scenarios   the questions askable today, each with its expected answer
    /harness/score    POST VeroMesh's answers; returns what passed, with precision and recall

A connector is pointed at a system's base URL (for example http://harness:8100/servicenow)
exactly as it would be at the real system's.
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from . import __version__, clock, scenarios
from .systems import REGISTRY
from .truth import Truth, generate


class Advance(BaseModel):
    days: int


def answer_key(truth: Truth, systems: dict) -> dict:
    t = truth
    return {
        "today": t.today.isoformat(),
        "organization": t.org_name,
        "defects": [d for s in systems.values() for d in s.defects()],
        "events": t.applied,
        "people": {
            p.id: {
                "name": p.name,
                "email": p.email,
                "employeeId": p.employee_id,
                "workerType": p.worker_type,
                "unit": pos.unit,
                "title": pos.title,
                "manager": pos.manager,
                "roles": pos.roles,
            }
            for p in t.active_people()
            if (pos := p.at(t.today))
        },
        "leavers": {p.id: p.left.isoformat() for p in t.people.values() if p.left and p.left <= t.today},
        "systems": {
            s.id: {"name": s.name, "businessOwner": s.business_owner, "technicalOwner": s.technical_owner, "lifecycle": s.lifecycle}
            for s in t.systems.values()
        },
        "assets": {a.name: {"owner": a.owner, "steward": a.steward, "columns": [c[0] for c in a.columns]} for a in t.assets.values()},
        "kpis": {k.id: {"owner": k.owner, "asset": k.asset} for k in t.kpis.values()},
        "agents": {
            a.id: {"actsFor": a.acts_for, "clientId": a.client_id, "principalActive": t.people[a.acts_for].active(t.today)}
            for a in t.agents.values()
        },
        "authorities": [vars(a) for a in t.authorities],
    }


def create_app(truth: Truth | None = None, systems: list[str] | None = None) -> FastAPI:
    truth = truth or generate()
    chosen = {n: REGISTRY[n](truth) for n in (systems or sorted(REGISTRY))}
    app = FastAPI(title="VeroMesh test harness", version=__version__, docs_url="/harness/docs", openapi_url="/harness/openapi.json")
    for name, system in chosen.items():
        app.include_router(system.router(), prefix=f"/{name}", tags=[system.title])
    app.state.truth = truth
    app.state.systems = chosen

    @app.get("/harness/status", tags=["harness"])
    def status() -> dict:
        return {
            "organization": truth.org_name,
            "profile": truth.profile,
            "seed": truth.seed,
            "start": truth.start.isoformat(),
            "today": truth.today.isoformat(),
            "people": len(truth.active_people()),
            "eventsApplied": len(truth.applied),
            "eventsScheduled": len(truth.events),
            "systems": {n: s.title for n, s in chosen.items()},
        }

    @app.post("/harness/clock", tags=["harness"])
    def advance(body: Advance) -> dict:
        done = clock.advance(truth, body.days)
        return {"today": truth.today.isoformat(), "applied": done}

    @app.get("/harness/answer-key", tags=["harness"])
    def key() -> dict:
        return answer_key(truth, chosen)

    @app.get("/harness/scenarios", tags=["harness"])
    def list_scenarios() -> dict:
        """The questions askable today, each with its expected answer."""
        return {"today": truth.today.isoformat(), "scenarios": scenarios.scenarios(truth, chosen)}

    @app.post("/harness/score", tags=["harness"])
    def score(answers: dict) -> dict:
        """Score VeroMesh's answers ({scenario id: {field: value}}) against today's scenarios."""
        return {"today": truth.today.isoformat(), **scenarios.score(scenarios.scenarios(truth, chosen), answers)}

    return app
