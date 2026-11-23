---
title: VeroMesh Test Harness — User Guide
---

<!--
Source of the Word copy: scripts/guides/build_docx.py builds user-guide.docx from this file
in CI. Edit here, never in Word. Sections marked "To write" are still skeleton.
-->

# About this guide

This guide is for connector developers and testers: pointing a connector at a simulated system, moving time forward, checking what the connector found against the answer key, and scoring VeroMesh's answers. Deploying and extending the harness is covered in the **Administrator Guide**.

# Concepts

- **The company** is generated from a profile and a seed. The default is Kestrel Valley Health, a healthcare organization.
- **Systems** each hold their own view of the company: their own IDs, their own lag, their own mistakes.
- **Defects** are those mistakes, recorded as the system's quirks are applied. Together they are the **answer key**: what VeroMesh should find.
- **The clock** moves the company forward: hires, leavers, a reorganization, a schema change, a retired system.
- **Scenarios** are the questions VeroMesh must answer (who may approve this, who owns that, what does this change break), each with an expected answer for today.

# Quick start

```bash
harness serve --people 2000 --seed 7 --port 8100
curl localhost:8100/harness/status
```

The API documentation is at `http://localhost:8100/harness/docs`.

# Pointing a connector at a system

Each system is served under its own path, with the same endpoints, paging, errors and sign-in as the real API:

| System | Base URL | Sign-in |
| --- | --- | --- |
| Microsoft Entra ID (Graph) | `/entra/v1.0` | `POST /entra/{tenant}/oauth2/v2.0/token`, client credentials |
| Workday | `/workday/ccx/api/v1/{tenant}`, `/workday/ccx/service/customreport2/{tenant}/...` | `POST /workday/ccx/oauth2/{tenant}/token` |
| ServiceNow | `/servicenow/api/now/table` | basic auth or `/servicenow/oauth_token.do` |
| Databricks | `/databricks/api/2.1/unity-catalog`, `/databricks/api/2.0/lineage-tracking`, `/databricks/api/2.0/sql/statements` | personal access token, or `POST /databricks/oidc/v1/token` |
| Oracle Fusion ERP | `/erp/fscmRestApi/resources/11.13.18.05`, `/erp/hcmRestApi/resources/11.13.18.05` | basic auth |
| EMR (FHIR R4) | `/emr/fhir` | `POST /emr/oauth2/token` |
| dbt | `/dbt/manifest.json`, `run_results.json`, `catalog.json` | none (published artifacts) |
| SharePoint (Graph) | `/sharepoint/v1.0` | the Entra ID token |
| Semantic layer (Stardog) | `/stardog/{database}/query` (SPARQL) | basic auth |

To write: verify each base path and credential against the system modules, and add an example request for each.

# Moving the clock

```bash
curl -X POST localhost:8100/harness/clock -H 'content-type: application/json' -d '{"days": 30}'
```

Scripted events happen on fixed days: the AP head leaves (day 20), a reorganization (45), a new AP head (50), a renamed column (60), the Invoice Assistant's principal leaves (75), a system is retired (120). Monthly churn adds hires, leavers and moves.

# Reading the answer key

`GET /harness/answer-key` lists every defect each system holds today: its kind, system, the entities involved and a description. A connector run is checked by comparing what VeroMesh reports (conflicts, findings, leavers with access) with the key.

To write: the defect kinds by system, and which VeroMesh finding each should produce.

# Scenarios and scoring

`GET /harness/scenarios` lists today's questions with their expected answers. Post VeroMesh's answers to `POST /harness/score` as `{scenario id: {field: value}}`; the score gives each field right or wrong, and precision and recall for lists. People may be named by any identifier the systems use: employee ID, email or sign-in name.

## Running them against VeroMesh

`harness run --veromesh <URL> --token <steward token> -o report.json` does both steps: it asks VeroMesh each question through its API and scores the answers. Load VeroMesh first with its connectors (Workday and Entra ID today). The report lists each scenario by gate as passed, wrong (with what is missing or extra), or not yet (with what VeroMesh lacks), and marks partial answers.

To write: the scenario list by gate, with the answer format for each.

# Making it harder

- **Difficulty:** `PUT /harness/difficulty` with `{"scale": 2, "overrides": {"name-variant": 0.9}}`. Scale `0` is a clean baseline with no quirks and no lag.
- **Chaos:** `PUT /harness/chaos` with `{"rate": 0.05}` makes 5% of system requests fail as real APIs do (429 and 503 with `Retry-After`, 500, an expired token, a slow answer). `GET /harness/chaos` counts what was injected. A connector should retry, refresh its token and finish with the same result as without chaos.

# Reference: the /harness routes

| Route | Purpose |
| --- | --- |
| `GET /harness/status` | The company, today's date, the systems served |
| `POST /harness/clock` | Move the clock forward |
| `GET /harness/answer-key` | Today's defects |
| `GET /harness/scenarios` | Today's questions and expected answers |
| `POST /harness/score` | Score answers |
| `GET` / `PUT /harness/difficulty` | Read or set difficulty and quirk overrides |
| `GET` / `PUT /harness/chaos` | Read or set misbehaving requests |
