"""The scenario runner: put today's scenarios to VeroMesh, score the answers.

    harness run --veromesh http://veromesh:8700 [--harness http://harness:8100] [--token ...]

For each scenario the harness lists (``GET /harness/scenarios``) the runner asks
VeroMesh through its public REST API, as an agent or a steward would, and sends the
answers to ``POST /harness/score``. It knows nothing of the truth: only the question.

Each answerer either returns an answer or raises ``NotYet`` naming what VeroMesh
lacks (usually a connector for the system that holds the facts), so the report
separates "answered wrong" from "cannot answer yet". People are answered in
VeroMesh's own IDs (employee IDs, or sign-in names); the scorer translates them.

What VeroMesh must hold first is loaded by its connectors, for example:

    veromesh-connect workday import --url http://harness:8100/workday --tenant kvh
    veromesh-connect entra import --tenant kvh --graph http://harness:8100/entra \\
        --login http://harness:8100/entra --role-group-prefix SG-
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx


class NotYet(Exception):
    """VeroMesh cannot answer this yet; the message says what is missing."""


#: An answer may carry this key: what it cannot cover yet. Reported, not scored.
PARTIAL = "_partial"


@dataclass
class VeroMesh:
    """VeroMesh's REST API (``/api/v1``)."""

    http: httpx.Client
    base: str = ""
    token: str | None = None
    calls: int = 0

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.calls += 1
        return self.http.request(method, f"{self.base}/api/v1{path}", headers=headers, **kw)

    def get(self, path: str, **params) -> dict | list:
        r = self.call("GET", path, params=params or None)
        if r.status_code == 404:
            raise NotYet(_detail(r))
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        r = self.call("POST", path, json=body)
        if r.status_code in (404, 422):
            raise NotYet(_detail(r))
        r.raise_for_status()
        return r.json()


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
    except (ValueError, AttributeError):
        d = None
    return str(d or r.text[:200] or r.status_code)


# ---------------------------------------------------------------------------- answerers


def identity_resolution(vm: VeroMesh, sc: dict) -> dict:
    """Everyone VeroMesh holds as a person; those with no employee ID (no HR record); and the
    people whose sources named them differently (a name conflict, merged into one person)."""
    people = vm.get("/people")
    if not people:
        raise NotYet("no people in VeroMesh: load Workday and Entra ID first")
    # People only the "ended" source keeps (dropped while another source still refers to
    # them) have left: they are not people working there.
    ended = {p["id"] for p in vm.get("/people", source="ended")} if _has_source(vm, "ended") else set()
    people = [p for p in people if p["id"] not in ended or _sources(vm, p["id"]) - {"ended"}]
    # People HR no longer lists whom an account or ERP role still holds have left too.
    left = {f["subject"]["id"] for f in _findings(vm, "access-without-employment") or []}
    people = [p for p in people if p["id"] not in left]
    conflicts = vm.get("/conflicts", type="people")
    return {
        "people": sorted(p["id"] for p in people),
        "noHrRecord": sorted(p["id"] for p in people if not p.get("employeeId")),
        "nameVariantsMerged": sorted({c["id"] for c in conflicts if c["field"] == "name"}),
        PARTIAL: "name variants only where the loaded sources disagree; ServiceNow and the EMR need their connectors",
    }


def _has_source(vm: VeroMesh, name: str) -> bool:
    return any(s.get("source") == name for s in vm.get("/sources"))


def _sources(vm: VeroMesh, pid: str) -> set[str]:
    """The sources that state a person, from VeroMesh's provenance of their name."""
    try:
        prov = vm.get(f"/people/{pid}/provenance")
    except NotYet:
        return set()
    return {c["source"] for c in prov.get("claims", [])}


def _findings(vm: VeroMesh, kind: str) -> list[dict] | None:
    """VeroMesh's findings of one kind, or None when this VeroMesh has no findings API."""
    try:
        return vm.get("/findings", kind=kind)["findings"]
    except NotYet:
        return None


def leaver_access(vm: VeroMesh, sc: dict) -> dict:
    """People who can still act somewhere although HR no longer lists them."""
    found = _findings(vm, "access-without-employment")
    if found is not None:
        if not _has_source(vm, "workday"):
            raise NotYet("no Workday source in VeroMesh")
        systems = sorted({f["source"] for f in found})
        return {
            "people": sorted({f["subject"]["id"] for f in found}),
            PARTIAL: f"accounts and ERP roles ({', '.join(systems) or 'none found'}); active practitioner "
            "records need the EMR connector, and approvals made after leaving need dated ERP history",
        }
    hr = {p["id"] for p in vm.get("/people", source="workday")}
    if not hr:
        raise NotYet("no Workday source in VeroMesh")
    iam = vm.get("/people", source="entra")
    if not iam:
        raise NotYet("no Entra ID source in VeroMesh")
    # Only people with an employee ID: those without one never had an HR record (contractors).
    return {
        "people": sorted(p["id"] for p in iam if p.get("employeeId") and p["id"] not in hr),
        PARTIAL: "accounts only: active practitioner records and ERP roles need the EMR and ERP connectors",
    }


def segregation_of_duties(vm: VeroMesh, sc: dict) -> dict:
    """Purchase orders their own buyer approved, as the ERP connector reports them."""
    found = _findings(vm, "self-approved-po")
    if found is None or not _has_source(vm, "erp"):
        raise NotYet("needs the ERP connector: purchase orders, buyers and approvers")
    return {"orders": sorted({f["about"]["po"] for f in found})}


def decision(vm: VeroMesh, sc: dict) -> dict:
    """An agent's request, put to the decision check as the agent would."""
    ask = sc["ask"]
    if {"patient", "requester"} & set(ask):
        raise NotYet("the decision check has no patient or requester context yet (Clinician Copilot slice)")
    body = {k: ask[k] for k in ("agent", "action", "amount", "currency") if k in ask}
    got = vm.post("/decisions/check", body)
    consult = got.get("consult") or []
    out = {"outcome": got.get("outcome")}
    if consult:
        # The first consulted role is the decision's own (above a limit: the next authority up);
        # owners consulted for impact follow it. VeroMesh names people as {"id", "name"}.
        out["consultRole"] = consult[0]["role"]
        out["consultPeople"] = sorted(p["id"] if isinstance(p, dict) else p for p in consult[0].get("people", []))
    if got.get("outcome") == "refuse":
        reasons = " ".join(got.get("reasons") or []).lower()
        if "principal" in reasons or "acts for" in reasons or "left" in reasons:
            out["reason"] = "principal-inactive"
    return out


def tradeoff(vm: VeroMesh, sc: dict) -> dict:
    """An action that moves two objectives that pull against each other."""
    ask = sc["ask"]
    got = vm.post("/decisions/check", {k: ask[k] for k in ("agent", "action", "amount", "currency")})
    out = {"outcome": got.get("outcome")}
    if got.get("outcome") == "refuse":
        reasons = " ".join(got.get("reasons") or []).lower()
        if "principal" in reasons or "acts for" in reasons or "left" in reasons:
            out["reason"] = "principal-inactive"
        return out
    trade = (got.get("basis") or {}).get("tradeoffs") or []
    if not trade:
        raise NotYet("the decision check reports no trade-off between objectives")
    out["consultRoles"] = sorted({c["role"] for c in got.get("consult") or []})
    out["priority"] = trade[0].get("priority")
    return out


def capability_health(vm: VeroMesh, sc: dict) -> dict:
    """Whether a capability is degraded, from VeroMesh's findings."""
    found = _findings(vm, "capability-degraded")
    if found is None:
        raise NotYet("needs the findings API")
    cap = sc["ask"]["capability"]
    hit = next((f for f in found if f["subject"].get("id") == cap), None)
    return {"capability": cap, "degraded": hit is not None, "owner": hit.get("ownerRole") if hit else None}


def missing(what: str) -> Callable[[VeroMesh, dict], dict]:
    def answer(vm: VeroMesh, sc: dict) -> dict:
        raise NotYet(what)

    return answer


ANSWERERS: dict[str, Callable[[VeroMesh, dict], dict]] = {
    "identity-resolution": identity_resolution,
    "leaver-access": leaver_access,
    "agent-within-limit": decision,
    "agent-over-limit": decision,
    "agent-principal-left": decision,
    "prescribing-privilege": missing("needs the EMR (FHIR) connector: practitioner roles and privileges"),
    "sensitive-data": missing("needs the Databricks Unity Catalog connector: columns and their tags"),
    "segregation-of-duties": segregation_of_duties,
    "objective-tradeoff": tradeoff,
    "capability-degraded": capability_health,
    "document-claims": missing("needs the SharePoint document reader"),
    "system-ownership": missing("needs the ServiceNow CMDB connector: systems and their owners"),
    "reorganization": missing("needs organization units (Workday supervisory organizations) in the model"),
    "vacant-owner": missing("needs KPIs and their owners (the governance workbook or catalog) in the model"),
    "breaking-change": missing("needs lineage (Databricks, dbt) and the semantic layer in the model"),
    "semantic-context": missing("needs the semantic layer (Stardog) and catalog connectors"),
}
for _sid in (
    "copilot-history-treating",
    "copilot-history-not-treating",
    "copilot-care-gap",
    "copilot-draft-order",
    "copilot-draft-order-nurse",
    "copilot-high-alert",
    "copilot-interaction",
    "copilot-prior-auth-within",
    "copilot-prior-auth-above",
    "copilot-prior-auth-deny",
    "copilot-record-release",
):
    ANSWERERS[_sid] = decision


# ---------------------------------------------------------------------------- the run


@dataclass
class Report:
    today: str
    answers: dict = field(default_factory=dict)
    not_yet: dict[str, str] = field(default_factory=dict)
    partial: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    score: dict = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [f"Scenarios on {self.today}: {self.score.get('passed', 0)} of {self.score.get('of', 0)} passed."]
        by_gate: dict[int, list[dict]] = {}
        for r in self.score.get("results", []):
            by_gate.setdefault(r["gate"], []).append(r)
        for gate in sorted(by_gate):
            rs = by_gate[gate]
            out.append(f"Gate {gate}: {sum(r['passed'] for r in rs)} of {len(rs)} passed")
            for r in rs:
                sid = r["id"]
                if sid in self.not_yet:
                    out.append(f"  not yet   {sid}: {self.not_yet[sid]}")
                elif sid in self.errors:
                    out.append(f"  error     {sid}: {self.errors[sid]}")
                elif r["passed"]:
                    out.append(f"  passed    {sid}")
                else:
                    out.append(f"  wrong     {sid}: {_why(r)}")
                if sid in self.partial:
                    out.append(f"            (partial: {self.partial[sid]})")
        return out


def _why(r: dict) -> str:
    parts = []
    for k, f in (r.get("fields") or {}).items():
        if f.get("passed"):
            continue
        if "recall" in f:
            parts.append(
                f"{k} precision {f['precision']}, recall {f['recall']}"
                + (f", missing {', '.join(f['missing'][:3])}" if f["missing"] else "")
                + (f", extra {', '.join(f['extra'][:3])}" if f["extra"] else "")
            )
        else:
            parts.append(f"{k} {json.dumps(f.get('got'))} (expected {json.dumps(f.get('expected'))})")
    return "; ".join(parts)


def run(vm: VeroMesh, harness: httpx.Client, harness_base: str = "") -> Report:
    listed = harness.get(f"{harness_base}/harness/scenarios").json()
    report = Report(today=listed["today"])
    for sc in listed["scenarios"]:
        answer = ANSWERERS.get(sc["id"], missing("no answerer for this scenario yet"))
        try:
            got = answer(vm, sc)
            if PARTIAL in got:
                report.partial[sc["id"]] = got.pop(PARTIAL)
            report.answers[sc["id"]] = got
        except NotYet as exc:
            report.not_yet[sc["id"]] = str(exc)
        except httpx.HTTPError as exc:
            report.errors[sc["id"]] = f"{type(exc).__name__}: {exc}"[:300]
    r = harness.post(f"{harness_base}/harness/score", json=report.answers)
    r.raise_for_status()
    report.score = r.json()
    return report
