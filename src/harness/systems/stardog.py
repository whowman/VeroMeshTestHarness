"""The semantic layer, through Stardog's HTTP API: business terms, their definitions and
domains, and the lakehouse tables and columns each is mapped to.

What a connector reads: GET /admin/databases (the databases), and SPARQL through
/{db}/query (GET with ?query= or POST, form-encoded or application/sparql-query),
answered as application/sparql-results+json. Basic authentication.

The model lives in the database ``semantic``, in the namespace
https://kestrelvalley.example/semantic#: each term is an owl:Class with rdfs:label,
skos:definition and sem:domain, and sem:mappedTo the asset (catalog.schema.table) or
column (catalog.schema.table.column) that holds it. The semantic layer says what data
means; who owns it and who may act on it is what VeroMesh adds (the Ontological Context
Layer), so the terms carry no owners.

Quirks, each recorded as a defect:
- unmapped-term: a term with no table behind it;
- overlapping-terms: two terms for nearly the same thing with different definitions
  (Practitioner and Provider);
- stale-mapping: after the clock's breaking change, a term is still mapped to the
  column that was renamed.
"""

from __future__ import annotations

import base64
import importlib

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from rdflib import OWL, RDF, RDFS, SKOS, Graph, Literal, Namespace, URIRef

from .base import SourceSystem, defect

USERS = {"admin": "harness"}
SEM = Namespace("https://kestrelvalley.example/semantic#")
DB = "semantic"
OVERLAP = ("Practitioner", "Provider")


class Stardog(SourceSystem):
    name = "stardog"
    title = "Semantic layer (Stardog HTTP API, SPARQL)"

    def project(self):
        t = self.truth
        prof = importlib.import_module(f"harness.profiles.{t.profile}")
        g = Graph()
        g.bind("sem", SEM)
        g.bind("skos", SKOS)
        defects = []
        columns = {f"{a.name}.{c[0]}" for a in t.assets.values() for c in a.columns}
        for term, domain, definition, maps in prof.SEMANTIC_TERMS:
            s = SEM[term]
            g.add((s, RDF.type, OWL.Class))
            g.add((s, RDFS.label, Literal(term)))
            g.add((s, SKOS.definition, Literal(definition)))
            g.add((s, SEM.domain, Literal(domain)))
            for m in maps:
                g.add((s, SEM.mappedTo, Literal(m)))
                is_column = m.count(".") == 3
                if is_column and m not in columns and m.rsplit(".", 1)[0] in t.assets:
                    defects.append(
                        defect("stale-mapping", self.name, {"term": term, "mappedTo": m}, "mapped to a column that no longer exists")
                    )
            if not maps and self.on("unmapped-term"):
                defects.append(defect("unmapped-term", self.name, {"term": term}, "a term with no table behind it"))
        if self.on("overlapping-terms"):
            g.add((SEM[OVERLAP[0]], SKOS.related, SEM[OVERLAP[1]]))
            defects.append(
                defect("overlapping-terms", self.name, {"terms": list(OVERLAP)}, "two terms for nearly the same thing, defined differently")
            )
        return {"graph": g}, defects

    def router(self) -> APIRouter:
        r = APIRouter()

        def signed_in(authorization: str = Header("")):
            if authorization.startswith("Basic "):
                user, _, pw = base64.b64decode(authorization[6:]).decode().partition(":")
                if USERS.get(user) == pw:
                    return
            raise HTTPException(401, detail={"message": "Authentication required", "code": "000012"})

        auth = [Depends(signed_in)]

        @r.get("/admin/databases", dependencies=auth)
        def databases():
            return {"databases": [DB]}

        async def run(db: str, request: Request) -> Response:
            if db != DB:
                raise HTTPException(404, detail={"message": f"Database '{db}' does not exist.", "code": "0D0DU2"})
            query = request.query_params.get("query")
            if query is None and request.method == "POST":
                ctype = request.headers.get("content-type", "")
                if ctype.startswith("application/sparql-query"):
                    query = (await request.body()).decode()
                else:
                    query = (await request.form()).get("query")
            if not query:
                raise HTTPException(400, detail={"message": "No query given", "code": "QE0PE2"})
            if any(w in query.upper().split() for w in ("INSERT", "DELETE", "DROP", "CLEAR", "LOAD", "CREATE")):
                raise HTTPException(400, detail={"message": "The harness's semantic layer is read-only", "code": "QE0PE2"})
            try:
                result = self.records()["graph"].query(query)
            except Exception as exc:  # a malformed query, as Stardog answers it
                raise HTTPException(400, detail={"message": str(exc)[:500], "code": "QE0PE2"}) from exc
            return Response(result.serialize(format="json"), media_type="application/sparql-results+json")

        r.add_api_route("/{db}/query", run, methods=["GET", "POST"], dependencies=auth)
        return r


__all__ = ["Stardog", "SEM", "URIRef"]
