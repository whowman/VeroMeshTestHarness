"""Databricks: the lakehouse, through Unity Catalog, lineage and SQL.

What a connector (and DataHub's Databricks ingestion) reads:
- Unity Catalog: /api/2.1/unity-catalog/catalogs, schemas, tables (with columns,
  owner and properties) and tables/{full_name};
- lineage: /api/2.0/lineage-tracking/table-lineage, upstreams and downstreams;
- SQL, through the Statement Execution API (/api/2.0/sql/statements): column and table
  tags from system.information_schema, and the tables' rows. The rows are simulated
  records (encounters, medication orders, invoices, workers), consistent with the truth.
Sign-in is a personal access token or OAuth machine-to-machine (/oidc/v1/token).

Quirks, each recorded as a defect:
- owner-is-group: bronze tables are owned by the data engineering group, not a person;
- owner-is-leaver: Unity Catalog does not follow HR, so a table owner who has left stays
  its owner until someone sets a new one (here: when the post is filled);
- untagged-sensitive-column: one column holding patient data has no classification tag;
- schema-drift: after the clock's breaking change, the bronze invoice table has the new
  column name while its downstream table still reads the old one.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, Header, HTTPException
from pydantic import BaseModel

from .base import SourceSystem, defect

PAT = "dapi-harness-token"  # noqa: S105 — the mock's personal access token
CLIENTS = {"veromesh-connector": "harness-secret"}
ENGINEERING_GROUP = "data-engineering"
UNTAGGED = ("clinical.silver.encounters", "admit_date")
TYPES = {"string": "STRING", "timestamp": "TIMESTAMP", "date": "DATE", "int": "INT", "double": "DOUBLE", "boolean": "BOOLEAN"}


def type_name(t: str) -> str:
    return "ARRAY" if t.startswith("array") else "DECIMAL" if t.startswith("decimal") else TYPES.get(t, t.upper())


class Statement(BaseModel):
    statement: str
    warehouse_id: str = ""
    wait_timeout: str = "10s"


class Databricks(SourceSystem):
    name = "databricks"
    title = "Databricks (Unity Catalog, lineage and SQL)"

    def project(self):
        t = self.truth
        defects: list[dict] = []
        catalogs, schemas, tables = {}, {}, {}
        # Unity Catalog takes its owners from whoever set them; it does not follow HR. A
        # table's owner is the truth's owner as of the start, unless a vacancy was filled.
        first_owner = {a: self._owner_at_start(a) for a in t.assets}
        for a in t.assets.values():
            cat, sch, name = a.name.split(".")
            catalogs.setdefault(
                cat, {"name": cat, "owner": ENGINEERING_GROUP, "comment": f"{cat.title()} data", "created_at": 1767225600000}
            )
            schemas.setdefault(f"{cat}.{sch}", {"name": sch, "catalog_name": cat, "full_name": f"{cat}.{sch}", "owner": ENGINEERING_GROUP})
            if a.layer == "bronze" and self.on("owner-is-group"):
                owner = ENGINEERING_GROUP
                defects.append(
                    defect(
                        "owner-is-group",
                        self.name,
                        {"asset": a.name, "owner": ENGINEERING_GROUP},
                        "owned by a group, not an accountable person",
                    )
                )
            else:
                pid = a.owner or (first_owner[a.name] if self.lag(1) else None)
                owner = t.people[pid].email if pid else ENGINEERING_GROUP
                if pid and t.people[pid].left and t.people[pid].left <= t.today:
                    defects.append(defect("owner-is-leaver", self.name, {"asset": a.name, "person": pid}, "table owner has left"))
            cols = []
            for i, (col, typ, cls) in enumerate(a.columns):
                untagged = (a.name, col) == UNTAGGED and self.on("untagged-sensitive-column")
                tagged = None if untagged else cls
                if untagged:
                    defects.append(
                        defect(
                            "untagged-sensitive-column",
                            self.name,
                            {"asset": a.name, "column": col, "classification": cls},
                            "patient data with no classification tag",
                        )
                    )
                cols.append({"name": col, "type_text": typ, "type_name": type_name(typ), "position": i, "nullable": True, "_tag": tagged})
            steward = t.people[a.steward].email if a.steward else None
            tables[a.name] = {
                "name": name,
                "catalog_name": cat,
                "schema_name": sch,
                "full_name": a.name,
                "table_type": "MANAGED",
                "data_source_format": "DELTA",
                "owner": owner,
                "comment": f"{a.layer.title()} table from {t.systems[a.source_system].name}",
                "columns": cols,
                "properties": {"layer": a.layer, "source_system": a.source_system, **({"steward": steward} if steward else {})},
                "updated_at": int(datetime.combine(t.start, time(), UTC).timestamp()) * 1000,
            }
        for e in t.applied:
            if e["kind"] == "schema-change":
                old, new = e["renamed"]
                for d in e["downstream"]:
                    defects.append(
                        defect(
                            "schema-drift",
                            self.name,
                            {"asset": e["asset"], "downstream": d, "renamed": e["renamed"]},
                            f"{e['asset']} renamed {old} to {new}; {d} still reads {old}",
                        )
                    )
        return {"catalogs": catalogs, "schemas": schemas, "tables": tables}, defects

    def _owner_at_start(self, asset: str) -> str | None:
        for e in self.truth.applied:
            for v in e.get("vacated", []):
                if v.get("asset") == asset and v["field"] == "owner":
                    return e["person"]
        return self.truth.assets[asset].owner

    # ------------------------------------------------------------ simulated rows

    def rows(self, full_name: str, limit: int) -> tuple[list[str], list[list]]:
        t = self.truth
        table = self.records()["tables"].get(full_name)
        if table is None:
            raise HTTPException(
                404,
                detail={
                    "error_code": "TABLE_OR_VIEW_NOT_FOUND",
                    "message": f"[TABLE_OR_VIEW_NOT_FOUND] The table or view `{full_name}` cannot be found.",
                },
            )
        cols = [c["name"] for c in table["columns"]]
        rng = random.Random(f"{t.seed}:{full_name}")
        people = [p for p in t.active_people() if p.worker_type == "employee"]
        clinicians = [p for p in people if p.npi] or people
        out = []
        for i in range(limit):
            if full_name.startswith("people."):
                p = people[i % len(people)]
                pos = p.at(t.today)
                vals = {
                    "worker_id": p.employee_id,
                    "person_id": p.employee_id,
                    "legal_name": p.name,
                    "email": p.email,
                    "position_id": f"P-{p.employee_id[1:]}",
                    "manager_id": t.people[pos.manager].employee_id if pos.manager else None,
                    "department": t.units[pos.unit].name,
                    "job_title": pos.title,
                    "hire_date": p.hired.isoformat(),
                }
            else:
                doc = clinicians[rng.randrange(len(clinicians))]
                day = t.today - timedelta(days=rng.randrange(365))
                vals = {
                    "encounter_id": f"E{rng.randrange(10**8):08d}",
                    "order_id": f"O{rng.randrange(10**8):08d}",
                    "patient_mrn": f"MRN{rng.randrange(10**7):07d}",
                    "individual_id": f"IND{rng.randrange(10**7):07d}",
                    "admit_ts": f"{day}T{rng.randrange(24):02d}:00:00",
                    "admit_date": day.isoformat(),
                    "signed_ts": f"{day}T{rng.randrange(24):02d}:15:00",
                    "signed_at": f"{day}T{rng.randrange(24):02d}:15:00",
                    "attending_npi": doc.npi,
                    "prescriber_npi": doc.npi,
                    "prescriber_id": doc.employee_id,
                    "dx_codes": ["E11.9", "I10"][: rng.randrange(1, 3)],
                    "primary_dx": rng.choice(["E11.9", "I10", "J45.909", "N18.3"]),
                    "department": rng.choice(["Emergency Medicine", "Internal Medicine", "Cardiology"]),
                    "drug_ndc": f"{rng.randrange(10**10):010d}",
                    "drug": rng.choice(["metformin", "lisinopril", "atorvastatin", "albuterol"]),
                    "encounters_12m": rng.randrange(1, 12),
                    "active_medications": ["metformin"],
                    "care_gaps": rng.choice([[], ["A1C overdue"], ["Statin not prescribed"]]),
                    "invoice_id": f"INV{rng.randrange(10**7):07d}",
                    "supplier_id": f"S{rng.randrange(10**4):04d}",
                    "supplier": rng.choice(["MedSupply Co", "Northline Pharma", "Clearwater Linen"]),
                    "amount": f"{rng.uniform(100, 90000):.2f}",
                    "invoice_amount": f"{rng.uniform(100, 90000):.2f}",
                    "amount_usd": f"{rng.uniform(100, 90000):.2f}",
                    "due_date": (day + timedelta(days=30)).isoformat(),
                    "approved_by": rng.choice(people).employee_id,
                    "paid_on_time": rng.random() < 0.87,
                    "month": day.replace(day=1).isoformat(),
                    "on_time_rate": round(rng.uniform(0.8, 0.95), 3),
                }
            out.append([vals.get(c) for c in cols])
        return cols, out

    # ------------------------------------------------------------ the API

    def router(self) -> APIRouter:
        r = APIRouter()

        def bearer(authorization: str = Header("")):
            if authorization != f"Bearer {PAT}":
                raise HTTPException(401, detail={"error_code": "UNAUTHENTICATED", "message": "Invalid access token."})

        auth = [Depends(bearer)]

        @r.post("/oidc/v1/token")
        def token(grant_type: str = Form(...), client_id: str = Form(""), client_secret: str = Form(""), scope: str = Form("all-apis")):
            if grant_type != "client_credentials" or CLIENTS.get(client_id) != client_secret:
                raise HTTPException(401, detail={"error": "invalid_client"})
            return {"access_token": PAT, "token_type": "Bearer", "expires_in": 3600, "scope": scope}

        def public(table: dict) -> dict:
            return {**table, "columns": [{k: v for k, v in c.items() if not k.startswith("_")} for c in table["columns"]]}

        @r.get("/api/2.1/unity-catalog/catalogs", dependencies=auth)
        def catalogs():
            return {"catalogs": sorted(self.records()["catalogs"].values(), key=lambda c: c["name"])}

        @r.get("/api/2.1/unity-catalog/schemas", dependencies=auth)
        def schemas(catalog_name: str):
            return {
                "schemas": sorted(
                    (s for s in self.records()["schemas"].values() if s["catalog_name"] == catalog_name), key=lambda s: s["name"]
                )
            }

        @r.get("/api/2.1/unity-catalog/tables", dependencies=auth)
        def tables(catalog_name: str, schema_name: str, max_results: int = 50, page_token: str | None = None):
            items = sorted(
                (t for t in self.records()["tables"].values() if (t["catalog_name"], t["schema_name"]) == (catalog_name, schema_name)),
                key=lambda t: t["name"],
            )
            start = int(page_token or 0)
            out = {"tables": [public(t) for t in items[start : start + max_results]]}
            if start + max_results < len(items):
                out["next_page_token"] = str(start + max_results)
            return out

        @r.get("/api/2.1/unity-catalog/tables/{full_name}", dependencies=auth)
        def table(full_name: str):
            t = self.records()["tables"].get(full_name)
            if t is None:
                raise HTTPException(404, detail={"error_code": "TABLE_DOES_NOT_EXIST", "message": f"Table '{full_name}' does not exist."})
            return public(t)

        @r.get("/api/2.0/lineage-tracking/table-lineage", dependencies=auth)
        def lineage(table_name: str, include_entity_lineage: bool = False):
            def info(name):
                cat, sch, tbl = name.split(".")
                return {"tableInfo": {"name": tbl, "catalog_name": cat, "schema_name": sch, "table_type": "TABLE"}}

            edges = self.truth.lineage
            return {
                "upstreams": [info(s) for s, d in edges if d == table_name],
                "downstreams": [info(d) for s, d in edges if s == table_name],
            }

        @r.post("/api/2.0/sql/statements", dependencies=auth)
        def statement(body: Statement):
            cols, rows = self.sql(body.statement)
            return {
                "statement_id": self.guid("stmt", body.statement),
                "status": {"state": "SUCCEEDED"},
                "manifest": {
                    "format": "JSON_ARRAY",
                    "schema": {"column_count": len(cols), "columns": [{"name": c, "position": i} for i, c in enumerate(cols)]},
                    "total_row_count": len(rows),
                },
                "result": {"row_count": len(rows), "data_array": rows},
            }

        return r

    def sql(self, statement: str) -> tuple[list[str], list[list]]:
        """The statements a connector sends: column and table tags from
        system.information_schema, and ``SELECT * FROM <table> [LIMIT n]``."""
        s = " ".join(statement.strip().rstrip(";").split())
        if re.fullmatch(r"(?i)select \* from system\.information_schema\.column_tags", s):
            cols = ["catalog_name", "schema_name", "table_name", "column_name", "tag_name", "tag_value"]
            rows = [
                [t["catalog_name"], t["schema_name"], t["name"], c["name"], "classification", c["_tag"]]
                for t in self.records()["tables"].values()
                for c in t["columns"]
                if c["_tag"]
            ]
            return cols, rows
        if re.fullmatch(r"(?i)select \* from system\.information_schema\.table_tags", s):
            cols = ["catalog_name", "schema_name", "table_name", "tag_name", "tag_value"]
            return cols, [
                [t["catalog_name"], t["schema_name"], t["name"], "layer", t["properties"]["layer"]]
                for t in self.records()["tables"].values()
            ]
        m = re.fullmatch(r"(?i)select \* from ([a-z_]+\.[a-z_]+\.[a-z_]+)(?: limit (\d+))?", s)
        if m:
            return self.rows(m[1], min(int(m[2] or 100), 1000))
        raise HTTPException(
            400,
            detail={
                "error_code": "BAD_REQUEST",
                "message": f"The harness runs only the statements a connector sends; not: {statement[:200]}",
            },
        )
