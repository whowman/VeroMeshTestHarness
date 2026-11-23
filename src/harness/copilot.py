"""The Clinician Copilot slice: questions a clinical AI copilot asks before it acts, and
what the organizational context layer must answer: may it act, who must sign or review,
who owns the data or content, and where the work goes.

Each scenario names a real patient in the EMR and a real clinician (the requester),
chosen from the simulated company, and an action from the profile's COPILOT_RULES. The
expected answer comes from the rules and the truth today:
- outcome: allow, draft (the Copilot drafts; a person signs), consult (people in the named
  roles decide) or refuse (no care relationship, or the requester has left);
- signers / reviewers: the roles, and the people in them today (a vacant role goes to the
  head of the rule's owning unit);
- dataOwner: the head of the unit that owns the data or content.
"""

from __future__ import annotations

import importlib

from .truth import Truth

PA_WITHIN, PA_ABOVE = 8_000, 60_000


def _people(t: Truth, role: str) -> list[str]:
    return sorted(p.id for p in t.active_people() if role in p.at(t.today).roles)


def _decide(t: Truth, roles: list[str], owner_unit: str) -> list[str]:
    """Who decides for these roles today, or the owning unit's head if none can."""
    out = sorted({x for r in roles for x in _people(t, r)})
    if not out and (h := t.head_of(owner_unit)):
        out = [h.id]
    return out


def scenarios(t: Truth, systems: dict) -> list[dict]:
    if "emr" not in systems:
        return []
    prof = importlib.import_module(f"harness.profiles.{t.profile}")
    rules = prof.COPILOT_RULES
    emr = systems["emr"].records()
    pid = lambda ref: ref.split("/prac-")[1] if "/prac-" in ref else None  # noqa: E731
    treating: dict[str, set[str]] = {}
    for e in emr["Encounter"].values():
        pt = e["subject"]["reference"].split("/")[1]
        for part in e["participant"]:
            if (p := pid(part["individual"]["reference"])) and t.people[p].active(t.today):
                treating.setdefault(pt, set()).add(p)
    for m in emr["MedicationRequest"].values():
        pt = m["subject"]["reference"].split("/")[1]
        if (p := pid(m["requester"]["reference"])) and t.people[p].active(t.today):
            treating.setdefault(pt, set()).add(p)
    prescribers = set(_people(t, "Prescriber"))
    # The scenarios' patient: patient 0 (two interacting orders), treated by an active prescriber.
    patient = "pat-00000"
    docs = sorted(treating.get(patient, set()) & prescribers)
    if not docs:
        patient = next((pt for pt in sorted(treating) if treating[pt] & prescribers), None)
        if patient is None:
            return []
        docs = sorted(treating[patient] & prescribers)
    doctor = docs[0]
    head = lambda unit: h.id if (h := t.head_of(unit)) else None  # noqa: E731
    outsider = next(
        (
            p.id
            for p in sorted(t.active_people(), key=lambda p: p.id)
            if "Clinician" in p.at(t.today).roles and p.id not in treating.get(patient, set())
        ),
        None,
    )
    nurses = [p.id for p in sorted(t.active_people(), key=lambda p: p.id) if p.at(t.today).title == "Registered Nurse"]
    rogue = {d["about"]["person"] for d in systems["emr"].defects() if d["kind"] == "privilege-not-in-hr"}
    nurse = next((n for n in nurses if n in rogue), nurses[0] if nurses else None)
    gp = emr["Patient"][patient]["generalPractitioner"]
    gp_person = pid(gp[0]["reference"]) if gp else None
    meds = sorted(
        m["medicationCodeableConcept"]["text"]
        for m in emr["MedicationRequest"].values()
        if m["subject"]["reference"] == f"Patient/{patient}"
    )
    interacting = [pair for pair in prof.MAJOR_INTERACTIONS if set(pair) <= set(meds)]
    out = []

    def add(sid, title, ask, expected):
        out.append(
            {
                "id": sid,
                "gate": 1,
                "slice": "clinician-copilot",
                "title": title,
                "ask": {"agent": "ClinicianCopilot", "patient": patient, **ask},
                "expected": expected,
            }
        )

    add(
        "copilot-history-treating",
        "Summarize the history of a patient the requester is treating",
        {"action": "SummarizePatientHistory", "requester": doctor},
        {"outcome": "allow", "dataOwner": head(rules["SummarizePatientHistory"]["owner"])},
    )
    if outsider:
        add(
            "copilot-history-not-treating",
            "Summarize the history of a patient the requester is not treating",
            {"action": "SummarizePatientHistory", "requester": outsider},
            {"outcome": "refuse", "reason": "no-care-relationship"},
        )
    add(
        "copilot-care-gap",
        "Identify a care gap and route it",
        {"action": "IdentifyCareGap", "requester": doctor},
        {"outcome": "allow", "routeTo": gp_person, "dataOwner": head(rules["IdentifyCareGap"]["owner"])},
    )
    r = rules["DraftMedicationOrder"]
    add(
        "copilot-draft-order",
        "Draft a medication order for the treating prescriber to sign",
        {"action": "DraftMedicationOrder", "requester": doctor, "medication": "lisinopril 10 mg"},
        {"outcome": "draft", "signers": [doctor], "reviewRoles": r["review"], "reviewers": _decide(t, r["review"], r["owner"])},
    )
    if nurse:
        add(
            "copilot-draft-order-nurse",
            "A nurse asks for an order: it is drafted for a treating prescriber, never for the nurse",
            {"action": "DraftMedicationOrder", "requester": nurse, "medication": "lisinopril 10 mg"},
            {"outcome": "draft", "signers": docs, "requesterMaySign": False, "reviewRoles": r["review"]},
        )
    r = rules["DraftHighAlertMedicationOrder"]
    add(
        "copilot-high-alert",
        "Draft a high-alert medication order: two independent pharmacist checks",
        {"action": "DraftHighAlertMedicationOrder", "requester": doctor, "medication": prof.HIGH_ALERT[0]},
        {"outcome": "draft", "signers": [doctor], "reviewRoles": r["review"], "independentChecks": len(r["review"])},
    )
    r = rules["CrossReferenceMedications"]
    add(
        "copilot-interaction",
        "Cross-reference the patient's medications",
        {"action": "CrossReferenceMedications", "requester": doctor},
        {
            "outcome": "consult" if interacting else "allow",
            "interactions": [list(p) for p in interacting],
            "consultRoles": r["onMajorInteraction"] if interacting else [],
            "dataOwner": head(r["owner"]),
        },
    )
    r = rules["ApprovePriorAuthorization"]
    add(
        "copilot-prior-auth-within",
        "A prior authorization within the reviewers' limit",
        {"action": "ApprovePriorAuthorization", "requester": doctor, "amount": PA_WITHIN, "currency": "USD"},
        {"outcome": "consult", "consultRoles": r["review"], "consultPeople": _decide(t, r["review"], r["owner"])},
    )
    add(
        "copilot-prior-auth-above",
        "A prior authorization above the reviewers' limit",
        {"action": "ApprovePriorAuthorization", "requester": doctor, "amount": PA_ABOVE, "currency": "USD"},
        {"outcome": "consult", "consultRoles": r["above"], "consultPeople": _decide(t, r["above"], r["owner"])},
    )
    r = rules["DenyPriorAuthorization"]
    add(
        "copilot-prior-auth-deny",
        "Denying a prior authorization: always a physician",
        {"action": "DenyPriorAuthorization", "requester": doctor, "amount": PA_WITHIN, "currency": "USD"},
        {"outcome": "consult", "consultRoles": r["review"], "consultPeople": _decide(t, r["review"], r["owner"])},
    )
    r = rules["ReleaseRecordsExternally"]
    add(
        "copilot-record-release",
        "Release the patient's records to an outside provider",
        {"action": "ReleaseRecordsExternally", "requester": doctor},
        {"outcome": "consult", "consultRoles": r["review"], "consultPeople": _decide(t, r["review"], r["owner"])},
    )
    return out


def semantic_context(t: Truth, systems: dict) -> dict | None:
    """Each semantic term's accountable owner (the owner of the table it maps to), the
    terms with no table, and the mappings broken by a change."""
    if "stardog" not in systems:
        return None
    prof = importlib.import_module(f"harness.profiles.{t.profile}")
    owners = {}
    for term, _domain, _definition, maps in prof.SEMANTIC_TERMS:
        tables = sorted({m if m.count(".") == 2 else m.rsplit(".", 1)[0] for m in maps})
        owners[term] = sorted({t.assets[x].owner for x in tables if x in t.assets and t.assets[x].owner})
    d = systems["stardog"].defects()
    return {
        "id": "semantic-context",
        "gate": 2,
        "slice": "clinician-copilot",
        "title": "Who is accountable for each semantic term",
        "ask": "For each term in the semantic layer, name the people accountable for the data behind it, and list unmapped terms and broken mappings.",
        "expected": {
            "owners": owners,
            "unmapped": sorted(x["about"]["term"] for x in d if x["kind"] == "unmapped-term"),
            "staleMappings": sorted(x["about"]["mappedTo"] for x in d if x["kind"] == "stale-mapping"),
        },
    }
