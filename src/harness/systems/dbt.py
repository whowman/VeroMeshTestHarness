"""dbt over the lakehouse: the artifacts a dbt run leaves, as DataHub and connectors read
them (/dbt/manifest.json, /dbt/run_results.json, /dbt/catalog.json).

The project's sources are the bronze tables; its models are the silver and gold tables,
with the lakehouse's lineage as their dependencies, each column's classification and
the asset's owner in ``meta``, and not_null and unique tests on their key columns. The
run results come from the most recent nightly run.

Quirk, recorded as a defect:
- model-failing: after the clock's breaking change, the model reading the renamed
  column fails (the column cannot be resolved), and the models after it are skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from fastapi import APIRouter

from .base import SourceSystem, defect

PROJECT = "kestrel"


def node_id(name: str, layer: str) -> str:
    cat, sch, tbl = name.split(".")
    return f"source.{PROJECT}.{cat}_{sch}.{tbl}" if layer == "bronze" else f"model.{PROJECT}.{cat}_{sch}_{tbl}"


class Dbt(SourceSystem):
    name = "dbt"
    title = "dbt (run artifacts)"

    def project(self):
        t = self.truth
        defects: list[dict] = []
        sources, nodes = {}, {}
        ids = {a.name: node_id(a.name, a.layer) for a in t.assets.values()}
        renamed = {e["asset"]: e["renamed"] for e in t.applied if e["kind"] == "schema-change"}
        for a in t.assets.values():
            cat, sch, tbl = a.name.split(".")
            columns = {c: {"name": c, "data_type": typ, "meta": {"classification": cls}} for c, typ, cls in a.columns}
            owner = t.people[a.owner].email if a.owner else None
            common = {
                "database": cat,
                "schema": sch,
                "name": tbl,
                "columns": columns,
                "meta": {"owner": owner, "layer": a.layer},
                "relation_name": f"`{cat}`.`{sch}`.`{tbl}`",
            }
            if a.layer == "bronze":
                sources[ids[a.name]] = {"unique_id": ids[a.name], "resource_type": "source", "source_name": f"{cat}_{sch}", **common}
                continue
            ups = [ids[s] for s, d in t.lineage if d == a.name]
            nodes[ids[a.name]] = {
                "unique_id": ids[a.name],
                "resource_type": "model",
                "config": {"materialized": "table"},
                "depends_on": {"nodes": ups},
                **common,
            }
            key = a.columns[0][0]
            for test in ("not_null", "unique"):
                tid = f"test.{PROJECT}.{test}_{cat}_{sch}_{tbl}_{key}"
                nodes[tid] = {
                    "unique_id": tid,
                    "resource_type": "test",
                    "name": f"{test}_{tbl}_{key}",
                    "column_name": key,
                    "test_metadata": {"name": test},
                    "depends_on": {"nodes": [ids[a.name]]},
                    "attached_node": ids[a.name],
                }
        # The last nightly run: a model reading a renamed column fails; what depends on it is skipped.
        failed: set[str] = set()
        for asset, (old, new) in renamed.items():
            for s, d in t.lineage:
                if s == asset:
                    failed.add(ids[d])
                    defects.append(
                        defect(
                            "model-failing",
                            self.name,
                            {"model": ids[d], "asset": d, "renamed": [old, new]},
                            f"column {old} cannot be resolved after {asset} renamed it to {new}",
                        )
                    )
        order = topo(nodes)
        skipped: set[str] = set()
        results = []
        run_at = datetime.combine(t.today, time(3), UTC).isoformat()
        for nid in order:
            n = nodes[nid]
            deps = set(n["depends_on"]["nodes"])
            if nid in failed:
                status, msg = "error", "Runtime Error: [UNRESOLVED_COLUMN] A column cannot be resolved"
            elif deps & (failed | skipped):
                status, msg = "skipped", None
                skipped.add(nid)
            else:
                status, msg = ("pass" if n["resource_type"] == "test" else "success"), None
            results.append(
                {
                    "unique_id": nid,
                    "status": status,
                    "message": msg,
                    "execution_time": 1.2,
                    "timing": [{"name": "execute", "started_at": run_at, "completed_at": run_at}],
                }
            )
        manifest = {
            "metadata": {
                "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                "project_name": PROJECT,
                "generated_at": run_at,
                "adapter_type": "databricks",
            },
            "nodes": nodes,
            "sources": sources,
        }
        run = {
            "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v6.json", "generated_at": run_at},
            "results": results,
            "args": {"which": "build"},
        }
        catalog = {
            "metadata": {"generated_at": run_at},
            "nodes": {
                i: {
                    "metadata": {"type": "table", "schema": n["schema"], "name": n["name"]},
                    "columns": {c: {"type": v["data_type"], "index": k} for k, (c, v) in enumerate(n["columns"].items())},
                }
                for i, n in {**nodes, **sources}.items()
                if n.get("columns")
            },
        }
        return {"manifest": manifest, "run_results": run, "catalog": catalog}, defects

    def router(self) -> APIRouter:
        r = APIRouter()

        @r.get("/manifest.json")
        def manifest():
            return self.records()["manifest"]

        @r.get("/run_results.json")
        def run_results():
            return self.records()["run_results"]

        @r.get("/catalog.json")
        def catalog():
            return self.records()["catalog"]

        return r


def topo(nodes: dict) -> list[str]:
    out, seen = [], set()

    def visit(n):
        if n in seen or n not in nodes:
            return
        seen.add(n)
        for d in nodes[n]["depends_on"]["nodes"]:
            visit(d)
        out.append(n)

    for n in sorted(nodes):
        visit(n)
    return out
