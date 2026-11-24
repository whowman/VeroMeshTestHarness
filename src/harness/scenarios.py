"""Scenarios: the questions VeroMesh must answer about the simulated company, each with
its expected answer, worked out from the truth on the day it is asked.

A scenario is askable from its day on (the clock's script decides when it becomes
interesting). Its ``expected`` holds the answer: a value, or a list compared as a set.
``score(answers)`` compares VeroMesh's answers with the expected ones: an exact match
for a value; precision and recall for a list.

The same scenarios become the acceptance tests for a design partner, with their own
truth (their HR and their owners) in place of the simulated company's.
"""

from __future__ import annotations

import importlib

from . import copilot
from .truth import Truth

AGENT_LIMIT_ASK = 12_000  # above the AP specialist's 5,000, within the AP Manager's 50,000
AGENT_SMALL_ASK = 800
AGENT_TRADEOFF_ASK = 4_500  # within the specialist's 5,000, above the working-capital threshold


def _holders(t: Truth, role: str) -> list[str]:
    return sorted(p.id for p in t.active_people() if role in p.at(t.today).roles)


def _day(t: Truth) -> int:
    return (t.today - t.start).days


def scenarios(t: Truth, systems: dict) -> list[dict]:
    """Every scenario askable today, with its expected answer."""
    day = _day(t)
    defects = [d for s in systems.values() for d in s.defects()]
    by_kind: dict[str, list[dict]] = {}
    for d in defects:
        by_kind.setdefault(d["kind"], []).append(d)
    agent = t.agents["InvoiceAssistant"]
    principal = t.people[agent.acts_for]
    principal_active = principal.active(t.today)
    out = []

    def add(sid, gate, title, ask, expected, since=0):
        if day >= since:
            out.append({"id": sid, "gate": gate, "title": title, "ask": ask, "expected": expected})

    # ---------------------------------------------------------------- Gate 1: people, roles, agents
    add(
        "identity-resolution",
        1,
        "One person per human across Workday, Entra ID, ServiceNow and the EMR",
        "List everyone currently working, merged across the sources, and those with no HR record.",
        {
            "people": sorted(p.id for p in t.active_people()),
            "noHrRecord": sorted(p.id for p in t.active_people() if p.worker_type == "contractor"),
            # Shown by a short name in Entra ID (a nickname) or in Workday (a preferred name),
            # while ServiceNow and the EMR carry the legal name: two names for one person.
            "nameVariantsMerged": sorted(
                {
                    d["about"]["person"]
                    for kind in ("name-variant", "legal-and-preferred-name")
                    for d in by_kind.get(kind, [])
                    if t.people[d["about"]["person"]].active(t.today)
                }
            ),
        },
    )
    consult_role = "APManager" if _holders(t, "APManager") else "FinanceApprover"
    add(
        "agent-within-limit",
        1,
        "The Invoice Assistant approves a small invoice",
        {"agent": agent.id, "action": "ApproveSupplierInvoice", "amount": AGENT_SMALL_ASK, "currency": "USD"},
        {"outcome": "allow"} if principal_active else {"outcome": "refuse", "reason": "principal-inactive"},
    )
    add(
        "agent-over-limit",
        1,
        "The Invoice Assistant asks above its principal's limit",
        {"agent": agent.id, "action": "ApproveSupplierInvoice", "amount": AGENT_LIMIT_ASK, "currency": "USD"},
        {"outcome": "consult", "consultRole": consult_role, "consultPeople": _holders(t, consult_role)}
        if principal_active
        else {"outcome": "refuse", "reason": "principal-inactive"},
    )
    add(
        "agent-principal-left",
        1,
        "The person the Invoice Assistant acts for has left",
        {"agent": agent.id, "action": "ApproveSupplierInvoice", "amount": AGENT_SMALL_ASK, "currency": "USD"},
        {"outcome": "refuse", "reason": "principal-inactive"},
        since=75,
    )
    add(
        "leaver-access",
        1,
        "People who have left but can still act somewhere",
        "List people who have left and still have an enabled account, an active practitioner record or an ERP role.",
        {
            "people": sorted(
                {
                    d["about"]["person"]
                    for k in ("leaver-account-enabled", "practitioner-active-after-leaving", "leaver-retains-role", "leaver-still-active")
                    for d in by_kind.get(k, [])
                    if "person" in d["about"]
                }
                | {d["about"]["approver"] for d in by_kind.get("leaver-retains-role", []) if "approver" in d["about"]}
            )
        },
        since=1,
    )
    add(
        "prescribing-privilege",
        1,
        "Who may sign medication orders the Clinician Copilot drafts",
        "List the clinicians who may sign medication orders, and EMR privileges their HR role does not give.",
        {
            "maySign": _holders(t, "Prescriber"),
            "privilegeNotInHr": sorted(d["about"]["person"] for d in by_kind.get("privilege-not-in-hr", [])),
        },
    )
    add(
        "sensitive-data",
        1,
        "Which lakehouse columns hold patient or personal data",
        "List every column classified PHI or PII, including any the catalog has not tagged.",
        {
            "columns": sorted(f"{a.name}.{c}" for a in t.assets.values() for c, _ty, cls in a.columns if cls in ("phi", "pii")),
            "untagged": sorted(f"{d['about']['asset']}.{d['about']['column']}" for d in by_kind.get("untagged-sensitive-column", [])),
        },
    )
    add(
        "segregation-of-duties",
        1,
        "Purchase orders raised and approved by the same person",
        "List purchase orders whose buyer approved them.",
        {"orders": sorted(d["about"]["po"] for d in by_kind.get("self-approved-po", []))},
    )

    stale = sorted(
        {
            f"{d['about']['document']}#{d['about']['claim']}"
            for k in ("document-names-leaver", "document-outdated", "policy-system-mismatch", "register-conflicts-with-catalog")
            for d in by_kind.get(k, [])
        }
    )
    add(
        "document-claims",
        1,
        "Statements in the documents that are out of date or contradict the systems",
        "Read the SharePoint documents and list each statement (document#claim) that no longer matches the organization or the systems.",
        {"statements": stale},
    )

    # ---------------------------------------------------------------- Gate 2: systems
    add(
        "system-ownership",
        2,
        "Each system's business and technical owner, reconciled with the CMDB",
        "Give each system's owners, and the CMDB records that disagree.",
        {
            "owners": {s.id: {"business": s.business_owner, "technical": s.technical_owner} for s in t.systems.values()},
            "duplicateRecords": sorted(d["about"]["system"] for d in by_kind.get("duplicate-ci", [])),
            "missingTechnicalOwner": sorted(d["about"]["system"] for d in by_kind.get("no-technical-owner", [])),
            "ownerHasLeft": sorted(
                {f"{d['about']['system']}.{d['about']['field']}" for d in by_kind.get("owner-is-leaver", []) if "system" in d["about"]}
            ),
        },
    )
    head_cinf, head_data = t.head_of("CINF"), t.head_of("DATA")
    add(
        "reorganization",
        2,
        "Clinical Informatics now reports into Data & Analytics",
        "Who does the head of Clinical Informatics report to?",
        {
            "unit": "CINF",
            "parent": t.units["CINF"].parent,
            "headReportsTo": head_data.id if head_data else None,
            "head": head_cinf.id if head_cinf else None,
        },
        since=45,
    )
    ap_vacant = 20 <= day < 50
    add(
        "vacant-owner",
        2,
        "The Accounts Payable Manager's post is vacant",
        "Who owns the On-time payment rate KPI, and whom should a decision about it go to?",
        {
            "kpiOwner": t.kpis["OnTimePaymentRate"].owner,
            "escalateTo": t.head_of("FIN").id if t.head_of("FIN") else None,
            "vacant": ap_vacant,
        },
        since=20,
    )

    # Decision criteria: which objective wins, and whether a capability is degraded.
    prof = importlib.import_module(f"harness.profiles.{t.profile}")
    objectives = {o[0]: o for o in getattr(prof, "OBJECTIVES", [])}
    if "PreserveWorkingCapital" in objectives:
        # Both objectives' owners, and the owner of each KPI the payment moves materially (the
        # working-capital KPI: above its 4,000 USD threshold), as the KPI's owner's role.
        kpi_owner = t.people.get(t.kpis["DaysPayableOutstanding"].owner or "")
        pos = kpi_owner.at(t.today) if kpi_owner and kpi_owner.active(t.today) else None
        kpi_role = next((r for r in (pos.roles if pos else []) if r in t.roles), None)
        owners = sorted(
            {objectives["PaySuppliersOnTime"][4], objectives["PreserveWorkingCapital"][4]} | ({kpi_role} if kpi_role else set())
        )
        add(
            "objective-tradeoff",
            2,
            "Paying an invoice on time costs working capital",
            {"agent": agent.id, "action": "ApproveSupplierInvoice", "amount": AGENT_TRADEOFF_ASK, "currency": "USD"},
            {"outcome": "consult", "consultRoles": owners, "priority": "PaySuppliersOnTime"}
            if principal_active
            else {"outcome": "refuse", "reason": "principal-inactive"},
        )
    for cid, name, owner, _, _, (kpi, thr, better) in getattr(prof, "CAPABILITIES", []):
        value = prof.on_time_payment_rate(day) if kpi == "OnTimePaymentRate" else None
        if value is None:
            continue
        degraded = value < thr if better == "higher" else value > thr
        add(
            "capability-degraded",
            2,
            f"Is {name.lower()} healthy?",
            {"capability": cid, "question": f"Is the {name} capability degraded, and who must agents consult about it?"},
            {"capability": cid, "degraded": degraded, "owner": owner if degraded else None},
        )

    # ---------------------------------------------------------------- Gate 3: data products and change
    change = next((e for e in t.applied if e["kind"] == "schema-change"), None)
    if change:
        affected = _downstream(t, change["asset"])
        kpis = sorted(k.id for k in t.kpis.values() if k.asset in affected)
        notify = sorted(
            {a.owner for name in affected if (a := t.assets[name]).owner}
            | {t.kpis[k].owner for k in kpis if t.kpis[k].owner}
            | ({t.systems["LAKE"].technical_owner} if t.systems["LAKE"].technical_owner else set())
        )
        add(
            "breaking-change",
            3,
            "A column renamed in the bronze invoice table",
            {"asset": change["asset"], "renamed": change["renamed"]},
            {
                "affectedAssets": affected,
                "affectedKpis": kpis,
                "notify": notify,
                "failingModels": sorted(d["about"]["asset"] for d in by_kind.get("model-failing", [])),
                "staleSemanticMappings": sorted(d["about"]["mappedTo"] for d in by_kind.get("stale-mapping", [])),
            },
            since=60,
        )

    # ---------------------------------------------------------------- the Clinician Copilot slice
    out += copilot.scenarios(t, systems)
    if (sem := copilot.semantic_context(t, systems)) is not None:
        out.append(sem)
    return out


def _downstream(t: Truth, asset: str) -> list[str]:
    out, todo = set(), [asset]
    while todo:
        a = todo.pop()
        for s, d in t.lineage:
            if s == a and d not in out:
                out.add(d)
                todo.append(d)
    return sorted(out)


def aliases(t: Truth) -> dict[str, str]:
    """Every identifier the systems use for a person (employee ID, email, the email's local
    part, in any case) -> the person's ID in the truth. VeroMesh answers in the IDs it was
    given, so the scorer translates them before comparing."""
    out: dict[str, str] = {}
    for p in t.people.values():
        for alias in (p.id, p.employee_id, p.email, p.email.split("@")[0]):
            out[alias.lower()] = p.id
    # Systems: the CMDB's application number (APM...) and the system's name.
    for i, s in enumerate(t.systems):
        out[f"apm{1000 + i:07d}"] = s
        out[t.systems[s].name.lower()] = s
    return out


def _translate(value, names: dict[str, str]):
    if isinstance(value, str):
        if value.lower() in names:
            return names[value.lower()]
        head, dot, tail = value.rpartition(".")  # "APM0001000.owned_by": a record and its field
        if dot and head.lower() in names:
            return f"{names[head.lower()]}.{tail}"
        return value
    if isinstance(value, list):
        return [_translate(v, names) for v in value]
    if isinstance(value, dict):
        return {_translate(k, names) if isinstance(k, str) else k: _translate(v, names) for k, v in value.items()}
    return value


def score(expected: list[dict], answers: dict, names: dict[str, str] | None = None) -> dict:
    """Compare VeroMesh's answers ({scenario id: {field: value}}) with the expected ones.
    With ``names`` (from ``aliases``), people may be named by any identifier the systems use."""
    if names:
        answers = {sid: _translate(a, names) if isinstance(a, dict) else a for sid, a in answers.items()}
    results = []
    for sc in expected:
        got = answers.get(sc["id"])
        if got is None:
            results.append({"id": sc["id"], "gate": sc["gate"], "answered": False, "passed": False})
            continue
        fields = {}
        for k, want in sc["expected"].items():
            have = got.get(k)
            if isinstance(want, list):
                w, h = set(want), set(have or [])
                tp = len(w & h)
                fields[k] = {
                    "precision": round(tp / len(h), 3) if h else (1.0 if not w else 0.0),
                    "recall": round(tp / len(w), 3) if w else 1.0,
                    "missing": sorted(w - h)[:20],
                    "extra": sorted(h - w)[:20],
                }
                fields[k]["passed"] = fields[k]["precision"] == fields[k]["recall"] == 1.0
            else:
                fields[k] = {"passed": have == want, "expected": want, "got": have}
        results.append(
            {"id": sc["id"], "gate": sc["gate"], "answered": True, "passed": all(f["passed"] for f in fields.values()), "fields": fields}
        )
    passed = sum(r["passed"] for r in results)
    return {"passed": passed, "of": len(results), "results": results}
