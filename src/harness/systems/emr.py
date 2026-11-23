"""The EMR, through FHIR R4: the clinical system the Clinician Copilot works in.

What a connector reads: Organization (the clinical departments), Practitioner (the
clinicians, with their NPI), PractitionerRole (department, role and clinical
privileges), Patient, Encounter and MedicationRequest. Searches return a searchset
Bundle with a ``next`` link; _count, _offset and the usual search parameters
(identifier, active, practitioner, subject, _lastUpdated) are supported. Sign-in is
SMART backend services, here as client credentials at /emr/oauth2/token.

Patients, encounters and medication orders are synthetic and consistent with the
truth: the attending and prescribing practitioners are the company's own clinicians.

Privileges (PractitionerRole.code, system urn:kestrel:privilege) are what the EMR lets
a clinician do: ``prescribe`` for prescribers, ``verify-medication`` for pharmacists.

Quirks, each recorded as a defect:
- practitioner-active-after-leaving: the EMR deprovisions 14 days after HR;
- missing-npi: a few practitioners have no NPI identifier;
- privilege-not-in-hr: a few nurses have the prescribe privilege, which their HR role
  does not give them.
"""

from __future__ import annotations

import random
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request

from .base import SourceSystem, defect

CLIENTS = {"veromesh-connector": "harness-secret"}
TOKEN = "harness-emr-token"  # noqa: S105
DEPROVISION_LAG = 14
NPI = "http://hl7.org/fhir/sid/us-npi"
PRIVILEGE = "urn:kestrel:privilege"
MRN = "urn:kestrel:mrn"
CLINICAL_UNITS = ("EMER", "INTM", "CARD", "PHAR", "NURS", "CINF", "CLIN")
PATIENTS = 400


def role_code(roles: list[str]) -> tuple[str, str]:
    if "Pharmacist" in roles:
        return "pharmacist", "Pharmacist"
    if "Prescriber" in roles:
        return "doctor", "Doctor"
    return "nurse", "Nurse"


class Emr(SourceSystem):
    name = "emr"
    title = "Epic EHR (FHIR R4)"

    def project(self):
        t = self.truth
        rng = random.Random(f"{t.seed}:emr")
        defects: list[dict] = []
        res: dict[str, dict[str, dict]] = {
            k: {} for k in ("Organization", "Practitioner", "PractitionerRole", "Patient", "Encounter", "MedicationRequest")
        }
        for u in CLINICAL_UNITS:
            oid = f"org-{u.lower()}"
            res["Organization"][oid] = {
                "resourceType": "Organization",
                "id": oid,
                "name": t.units[u].name,
                "active": True,
                "identifier": [{"system": "urn:kestrel:unit", "value": u}],
            }
        clinicians = []
        for p in sorted(t.people.values(), key=lambda p: p.id):
            last = p.at(t.today) or p.history[-1]
            if not p.npi or last.unit not in CLINICAL_UNITS or p.hired > t.today:
                continue
            gone = p.left and p.left <= t.today
            if gone and t.today >= p.left + timedelta(days=self.lag(DEPROVISION_LAG)):
                active = False
            else:
                active = True
                if gone:
                    defects.append(
                        defect(
                            "practitioner-active-after-leaving",
                            self.name,
                            {"person": p.id, "left": p.left.isoformat()},
                            "practitioner still active in the EMR",
                        )
                    )
            pid = f"prac-{p.id}"
            ident = [{"system": NPI, "value": p.npi}]
            if self.quirk(0.02, "missing-npi", p.id):
                ident = []
                defects.append(defect("missing-npi", self.name, {"person": p.id}, "practitioner has no NPI identifier"))
            ident.append({"system": "urn:kestrel:employee", "value": p.employee_id})
            res["Practitioner"][pid] = {
                "resourceType": "Practitioner",
                "id": pid,
                "active": active,
                "identifier": ident,
                "name": [{"family": p.family, "given": [p.given]}],
                "telecom": [{"system": "email", "value": p.email}],
                "meta": {"lastUpdated": f"{self.changed_on(p)}T00:00:00Z"},
            }
            code, display = role_code(last.roles)
            privileges = (["prescribe"] if "Prescriber" in last.roles else []) + (
                ["verify-medication"] if "Pharmacist" in last.roles else []
            )
            if code == "nurse" and self.quirk(0.03, "privilege-not-in-hr", p.id):
                privileges.append("prescribe")
                defects.append(
                    defect(
                        "privilege-not-in-hr",
                        self.name,
                        {"person": p.id, "privilege": "prescribe", "title": last.title},
                        "EMR privilege not given by the person's HR role",
                    )
                )
            res["PractitionerRole"][f"role-{p.id}"] = {
                "resourceType": "PractitionerRole",
                "id": f"role-{p.id}",
                "active": active,
                "practitioner": {"reference": f"Practitioner/{pid}"},
                "organization": {"reference": f"Organization/org-{last.unit.lower()}", "display": t.units[last.unit].name},
                "code": [
                    {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/practitioner-role", "code": code, "display": display}]},
                    *({"coding": [{"system": PRIVILEGE, "code": pr}]} for pr in privileges),
                ],
            }
            if active and not gone:
                clinicians.append((p, last, "prescribe" in privileges))
        prescribers = [c for c in clinicians if c[2]] or clinicians
        for i in range(PATIENTS):
            pt = f"pat-{i:05d}"
            gp = rng.choice(clinicians)[0] if clinicians else None
            res["Patient"][pt] = {
                "resourceType": "Patient",
                "id": pt,
                "identifier": [{"system": MRN, "value": f"MRN{1000000 + i}"}],
                "name": [
                    {
                        "family": rng.choice(["Ward", "Ellis", "Hayes", "Flores", "Reed", "Price", "Ortiz", "Bennett"]),
                        "given": [rng.choice(["Ann", "Joe", "Lena", "Ray", "Tess", "Omar", "Ivy", "Hugo"])],
                    }
                ],
                "birthDate": f"{rng.randint(1935, 2015)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                "generalPractitioner": [{"reference": f"Practitioner/prac-{gp.id}"}] if gp else [],
            }
            for k in range(rng.randint(0, 3)):
                doc, pos, _ = rng.choice(clinicians)
                start = t.today - timedelta(days=rng.randint(1, 365))
                eid = f"enc-{i:05d}-{k}"
                res["Encounter"][eid] = {
                    "resourceType": "Encounter",
                    "id": eid,
                    "status": "finished",
                    "class": {
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                        "code": "EMER" if pos.unit == "EMER" else "AMB",
                    },
                    "subject": {"reference": f"Patient/{pt}"},
                    "participant": [{"individual": {"reference": f"Practitioner/prac-{doc.id}"}}],
                    "serviceProvider": {"reference": f"Organization/org-{pos.unit.lower()}"},
                    "period": {"start": f"{start}T08:00:00Z", "end": f"{start}T11:00:00Z"},
                }
                if rng.random() < 0.6 and prescribers:
                    doc = rng.choice(prescribers)[0]
                    mid = f"med-{i:05d}-{k}"
                    res["MedicationRequest"][mid] = {
                        "resourceType": "MedicationRequest",
                        "id": mid,
                        "status": "active",
                        "intent": "order",
                        "medicationCodeableConcept": {
                            "text": rng.choice(["metformin 500 mg", "lisinopril 10 mg", "atorvastatin 20 mg", "albuterol inhaler"])
                        },
                        "subject": {"reference": f"Patient/{pt}"},
                        "encounter": {"reference": f"Encounter/{eid}"},
                        "requester": {"reference": f"Practitioner/prac-{doc.id}"},
                        "authoredOn": f"{start}T10:00:00Z",
                    }
        return res, defects

    # ------------------------------------------------------------ the API

    def router(self) -> APIRouter:
        r = APIRouter()

        def bearer(authorization: str = Header("")):
            if authorization != f"Bearer {TOKEN}":
                raise HTTPException(401, detail={"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "login"}]})

        auth = [Depends(bearer)]

        @r.post("/oauth2/token")
        def token(
            grant_type: str = Form(...), client_id: str = Form(""), client_secret: str = Form(""), scope: str = Form("system/*.read")
        ):
            if grant_type != "client_credentials" or CLIENTS.get(client_id) != client_secret:
                raise HTTPException(401, detail={"error": "invalid_client"})
            return {"access_token": TOKEN, "token_type": "bearer", "expires_in": 300, "scope": scope}

        @r.get("/fhir/metadata")
        def metadata():
            return {
                "resourceType": "CapabilityStatement",
                "status": "active",
                "fhirVersion": "4.0.1",
                "format": ["json"],
                "rest": [{"mode": "server", "resource": [{"type": k} for k in self.records()]}],
            }

        @r.get("/fhir/{rtype}", dependencies=auth)
        def search(rtype: str, request: Request):
            items = self._of(rtype)
            q = request.query_params
            items = [i for i in items if fhir_match(i, q)]
            count, offset = min(int(q.get("_count", 50)), 500), int(q.get("_offset", 0))
            base = str(request.url_for("search", rtype=rtype))
            bundle = {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(items),
                "link": [{"relation": "self", "url": str(request.url)}],
                "entry": [
                    {"fullUrl": f"{base}/{i['id']}", "resource": i, "search": {"mode": "match"}} for i in items[offset : offset + count]
                ],
            }
            if offset + count < len(items):
                bundle["link"].append(
                    {"relation": "next", "url": str(request.url.include_query_params(_offset=offset + count, _count=count))}
                )
            return bundle

        @r.get("/fhir/{rtype}/{rid}", dependencies=auth)
        def read(rtype: str, rid: str):
            for i in self._of(rtype):
                if i["id"] == rid:
                    return i
            raise HTTPException(404, detail={"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "not-found"}]})

        return r

    def _of(self, rtype: str) -> list[dict]:
        if rtype not in self.records():
            raise HTTPException(
                400,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "not-supported", "diagnostics": f"{rtype} is not served by the harness"}],
                },
            )
        return sorted(self.records()[rtype].values(), key=lambda i: i["id"])


def _refs(item: dict, field: str) -> list[str]:
    v = item.get(field)
    if isinstance(v, dict):
        return [v.get("reference", "")]
    if isinstance(v, list):
        return [x.get("individual", x).get("reference", "") for x in v if isinstance(x, dict)]
    return []


def fhir_match(item: dict, q) -> bool:
    for key, want in q.items():
        if key.startswith("_") and key not in ("_lastUpdated", "_id"):
            continue
        if key == "_id":
            if item["id"] != want:
                return False
        elif key == "_lastUpdated":
            op, value = want[:2], want[2:]
            got = item.get("meta", {}).get("lastUpdated", "")[:10]
            if not {"gt": got > value, "ge": got >= value, "lt": got < value, "le": got <= value}.get(op, got == want):
                return False
        elif key == "identifier":
            system, _, value = want.rpartition("|")
            if not any(i.get("value") == value and (not system or i.get("system") == system) for i in item.get("identifier", [])):
                return False
        elif key == "active":
            if str(item.get("active")).lower() != want.lower():
                return False
        elif key in ("practitioner", "subject", "patient", "requester", "organization"):
            field = {"patient": "subject"}.get(key, key)
            want_ref = (
                want
                if "/" in want
                else f"{'Patient' if key in ('subject', 'patient') else 'Organization' if key == 'organization' else 'Practitioner'}/{want}"
            )
            if want_ref not in _refs(item, field) and want_ref not in _refs(item, "participant"):
                return False
        else:
            raise HTTPException(
                400,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "not-supported", "diagnostics": f"search parameter {key}"}],
                },
            )
    return True
