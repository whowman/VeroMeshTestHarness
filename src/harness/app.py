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
    /harness/received the calls that reached the ERP's MCP server (/erp/mcp), for the gateway

A connector is pointed at a system's base URL (for example http://harness:8100/servicenow)
exactly as it would be at the real system's.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.routing import Route

from . import __version__, clock, scenarios
from .systems import REGISTRY
from .truth import Truth, chance, generate

CHAOS_KINDS = ("throttle", "server-error", "unavailable", "token-expired", "slow")
SIGN_IN_PATHS = ("/token", "/oauth_token.do")  # token endpoints: never "your token has expired"


class Advance(BaseModel):
    days: int


class Chaos(BaseModel):
    rate: float = 0.0  # share of system requests that misbehave
    kinds: list[str] = list(CHAOS_KINDS)
    slow_ms: int = 1500
    seed: int = 1


class Difficulty(BaseModel):
    scale: float = 1.0
    overrides: dict[str, float] = {}


def sp_site(d: dict) -> str:
    from .systems.sharepoint import SITES

    return SITES[d["site"]]


def answer_key(truth: Truth, systems: dict) -> dict:
    t = truth
    return {
        "today": t.today.isoformat(),
        "organization": t.org_name,
        "difficulty": t.difficulty,
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
        "documents": {f"{sp_site(d)}/{d['folder']}/{d['name']}": d["claims"] for d in systems["sharepoint"].documents()}
        if "sharepoint" in systems
        else {},
    }


def create_app(truth: Truth | None = None, systems: list[str] | None = None) -> FastAPI:
    truth = truth or generate()
    chosen = {n: REGISTRY[n](truth) for n in (systems or sorted(REGISTRY))}
    servers = []  # MCP servers in front of systems, whose session managers the lifespan runs

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with AsyncExitStack() as stack:
            for m in servers:
                await stack.enter_async_context(m.session_manager.run())
            yield

    app = FastAPI(
        title="VeroMesh test harness",
        version=__version__,
        docs_url="/harness/docs",
        openapi_url="/harness/openapi.json",
        lifespan=lifespan,
    )
    for name, system in chosen.items():
        app.include_router(system.router(), prefix=f"/{name}", tags=[system.title])
    if "erp" in chosen:
        from .systems import erp_mcp

        server, endpoint = erp_mcp.build(chosen["erp"])
        servers.append(server)
        app.router.routes.append(Route("/erp/mcp", endpoint=endpoint, methods=["GET", "POST", "DELETE"]))
    app.state.truth = truth
    app.state.systems = chosen
    app.state.chaos = {"rate": 0.0, "kinds": list(CHAOS_KINDS), "slow_ms": 1500, "seed": 1}
    app.state.chaos_seen = {"requests": 0, **{k: 0 for k in CHAOS_KINDS}}

    @app.middleware("http")
    async def misbehave(request: Request, call_next):
        """With chaos on, some system requests fail the way real APIs do: throttled (429 with
        Retry-After), a server error (500), unavailable (503), an expired token (401), or
        slow. Which ones is decided by the request's number and the seed, so a run can be
        repeated. /harness itself always answers."""
        c, seen = app.state.chaos, app.state.chaos_seen
        if request.url.path.startswith("/harness") or c["rate"] <= 0:
            return await call_next(request)
        seen["requests"] += 1
        n = seen["requests"]
        if chance(c["seed"], "chaos", n) >= c["rate"] or not c["kinds"]:
            return await call_next(request)
        kind = c["kinds"][int(chance(c["seed"], "chaos-kind", n) * len(c["kinds"]))]
        if kind == "token-expired" and request.url.path.endswith(SIGN_IN_PATHS):
            return await call_next(request)  # a sign-in request carries no token to expire
        seen[kind] += 1
        if kind == "slow":
            await asyncio.sleep(c["slow_ms"] / 1000)
            return await call_next(request)
        status, headers, body = {
            "throttle": (
                429,
                {"Retry-After": "1"},
                {"error": {"code": "TooManyRequests", "message": "Rate limit exceeded. Retry after 1 second."}},
            ),
            "server-error": (500, {}, {"error": {"code": "InternalServerError", "message": "An unexpected error occurred."}}),
            "unavailable": (
                503,
                {"Retry-After": "2"},
                {"error": {"code": "ServiceUnavailable", "message": "The service is temporarily unavailable."}},
            ),
            "token-expired": (
                401,
                {"WWW-Authenticate": 'Bearer error="invalid_token", error_description="The access token expired"'},
                {"error": {"code": "InvalidAuthenticationToken", "message": "Lifetime validation failed, the token is expired."}},
            ),
        }[kind]
        return JSONResponse(body, status_code=status, headers=headers)

    @app.get("/harness/chaos", tags=["harness"])
    def get_chaos() -> dict:
        return {**app.state.chaos, "seen": app.state.chaos_seen}

    @app.put("/harness/chaos", tags=["harness"])
    def set_chaos(body: Chaos):
        """Make the systems misbehave: rate 0.05 fails 5% of requests, of the kinds listed."""
        unknown = set(body.kinds) - set(CHAOS_KINDS)
        if unknown:
            return JSONResponse({"detail": f"unknown kinds {sorted(unknown)}; known: {list(CHAOS_KINDS)}"}, status_code=422)
        app.state.chaos = body.model_dump()
        app.state.chaos_seen = {"requests": 0, **{k: 0 for k in CHAOS_KINDS}}
        return get_chaos()

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
            "difficulty": truth.difficulty,
            "systems": {n: s.title for n, s in chosen.items()},
        }

    @app.post("/harness/clock", tags=["harness"])
    def advance(body: Advance) -> dict:
        done = clock.advance(truth, body.days)
        return {"today": truth.today.isoformat(), "applied": done}

    @app.get("/harness/answer-key", tags=["harness"])
    def key() -> dict:
        return answer_key(truth, chosen)

    @app.get("/harness/difficulty", tags=["harness"])
    def get_difficulty() -> dict:
        return truth.difficulty

    @app.put("/harness/difficulty", tags=["harness"])
    def set_difficulty(body: Difficulty) -> dict:
        """Change how hard the systems make it; every system re-projects."""
        truth.difficulty = {"scale": body.scale, "overrides": dict(body.overrides)}
        for s in chosen.values():
            s._cache = None
        return truth.difficulty

    @app.get("/harness/scenarios", tags=["harness"])
    def list_scenarios() -> dict:
        """The questions askable today, each with its expected answer."""
        return {"today": truth.today.isoformat(), "scenarios": scenarios.scenarios(truth, chosen)}

    @app.get("/harness/received", tags=["harness"])
    def received(after: int = 0) -> dict:
        """The calls that reached the ERP's MCP server (``/erp/mcp``), in order, after ``after``:
        how a scenario tells whether a call VeroMesh's gateway refused or held reached it."""
        erp = chosen.get("erp")
        calls = [c for c in (erp.received if erp else []) if c["seq"] > after]
        return {"system": "erp", "calls": calls}

    @app.post("/harness/score", tags=["harness"])
    def score(answers: dict) -> dict:
        """Score VeroMesh's answers ({scenario id: {field: value}}) against today's scenarios.
        People may be named by any identifier the systems use: employee ID, email, or the
        email's local part (as for an account with no employee ID)."""
        expected = scenarios.scenarios(truth, chosen)
        return {"today": truth.today.isoformat(), **scenarios.score(expected, answers, scenarios.aliases(truth))}

    return app
