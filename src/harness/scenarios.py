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

from .truth import Truth

AGENT_LIMIT_ASK = 12_000  # above the AP specialist's 5,000, within the AP Manager's 50,000
AGENT_SMALL_ASK = 800


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
            "nameVariantsMerged": sorted({d["about"]["person"] for d in by_kind.get("name-variant", [])}),
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
            },
            since=60,
        )
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


def score(expected: list[dict], answers: dict) -> dict:
    """Compare VeroMesh's answers ({scenario id: {field: value}}) with the expected ones."""
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
