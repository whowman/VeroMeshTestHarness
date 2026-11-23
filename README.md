# VeroMesh test harness

A simulated company and its source systems, for testing [VeroMesh](https://github.com/whowman/VeroMeshOntology) end to end before a design partner's real systems are available, and as the acceptance tests to run against theirs.

VeroMesh's connectors read the simulated systems through their real APIs (Microsoft Graph, the ServiceNow Table API and the rest), exactly as they would at a customer. The harness knows the truth behind every system, so what VeroMesh concludes can be scored: every quirk a system introduces is written down in the **answer key**.

## How it works

```
            profile + seed + size
                     │
                     ▼
      ┌──────────────────────────────┐        ┌──────────────────────┐
      │  The truth (truth.py)        │◀──────│  The clock (clock.py) │
      │  people with history, units, │ events │  hires, moves,        │
      │  roles, authorities, systems,│        │  leavers, a reorg, a  │
      │  data, KPIs, agents          │        │  schema change, ...   │
      └──────────────┬───────────────┘        └──────────────────────┘
                     │ projected, with each system's lag and quirks
       ┌─────────────┼─────────────┬─────────────┬──────────────┐
       ▼             ▼             ▼             ▼              ▼
   Entra ID      ServiceNow     Workday       Databricks     ERP, EMR ... (systems/)
   Graph API     Table API      REST, RaaS    Unity Catalog  (each behind a mock of its real API)
       │             │
       └──────┬──────┘                       answer key: every defect,
              ▼                              the events, the true owners
        VeroMesh connectors  ─────────────▶  scored against it
```

1. **The truth** is one reproducible company generated from a profile, a size and a seed. The same three always give the same company. People keep a history of positions, so a system can lag behind the truth. The default profile, `healthcare`, is Kestrel Valley Health: a health system with clinical, finance, HR, IT, data and compliance functions. It has clinicians and decision rights, AI agents (a Clinician Copilot, an Invoice Assistant) and a lakehouse with clinical, finance and people data. All names are invented.
2. **The clock** moves the company forward:
   - the everyday churn, about 1% of the headcount each month for each of hires, moves and leavers;
   - a script of changes the gates must catch: the Accounts Payable Manager leaves (day 20) and the post stays vacant until a new one is hired (day 50), Clinical Informatics moves under Data & Analytics (day 45), an invoice column is renamed in the lakehouse (day 60, the Gate 3 breaking change), and the revenue cycle platform is retired (day 120).
3. **Each system** is a projection of the truth into that system's shape, IDs and quirks, served behind a mock of its real API. Each quirk is recorded as a defect, so it's known what VeroMesh should find.
4. **The answer key** (`GET /harness/answer-key`) has every system's defects, the events so far, and the truth: who works where with which roles, who owns each system, data asset and KPI, and which agents' principals are still active.

## Systems

| System | Path | API | Quirks (defects) |
| --- | --- | --- | --- |
| Microsoft Entra ID | `/entra` | Microsoft Graph v1.0: users (with `$select`, `$filter`, `$top`, paging), managers, `memberOf`, groups and members, service principals (the AI agents) and their owners, `users/delta`; OAuth client credentials at `/entra/{tenant}/oauth2/v2.0/token` | name-variant, stale-manager, missing-mail, leaver-account-enabled, contractor-only-here |
| ServiceNow | `/servicenow` | Table API: `sys_user`, `cmn_department`, `sys_user_group`, `sys_user_grmember`, `cmdb_ci_business_app`, `cmdb_ci_server`, `cmdb_ci_db_instance`, `cmdb_rel_ci`, `incident`, `change_request`. Encoded queries, `sysparm_fields`, `sysparm_display_value`, paging with `X-Total-Count` and `Link`; basic auth or `/oauth_token.do` | stale-user, leaver-still-active, no-technical-owner, duplicate-ci, owner-is-leaver |
| Workday HCM | `/workday` | REST API: `/ccx/api/v1/{tenant}/workers` (with `limit`, `offset`, `search`), `workers/{id}`, `supervisoryOrganizations`. Reports as a Service: `/ccx/service/customreport2/{tenant}/{owner}/VeroMesh_Workers` and `VeroMesh_Worker_Changes` (`Updated_From`, `Updated_Through`). OAuth at `/ccx/oauth2/{tenant}/token` (refresh token or client credentials). Employees only: no contractors | retro-termination, vacant-supervisory-org, legal-and-preferred-name |
| Databricks | `/databricks` | Unity Catalog: `/api/2.1/unity-catalog/catalogs`, `schemas`, `tables` (columns, owner, properties, paging), `tables/{full_name}`. Lineage: `/api/2.0/lineage-tracking/table-lineage`. SQL Statement Execution (`/api/2.0/sql/statements`): column and table tags from `system.information_schema`, and `SELECT * FROM <table> LIMIT n` returning simulated records (encounters, medication orders, invoices, workers) consistent with the truth. A personal access token, or OAuth at `/oidc/v1/token` | owner-is-group, owner-is-leaver, untagged-sensitive-column, schema-drift |

Lag is part of the simulation. Workday is the record, though some terminations are entered a few days late. Entra ID is provisioned from HR nightly. ServiceNow's users sync weekly and its CMDB owners are reviewed monthly. Unity Catalog's owners change only when someone sets them. So a change lands in each system at a different time, as it would at a real company.

Mock credentials (test values only): OAuth client `veromesh-connector` / `harness-secret` (Graph, Workday, Databricks); Workday refresh token `harness-workday-refresh`; ServiceNow `admin` / `harness`; Databricks token `dapi-harness-token`.

## Run it

```bash
pip install -e ".[test]"
harness serve --people 2000 --seed 7 --port 8100   # or: docker build -t veromesh/test-harness . && docker run -p 8100:8100 veromesh/test-harness
curl localhost:8100/harness/status
curl -X POST localhost:8100/harness/clock -H 'content-type: application/json' -d '{"days": 30}'
curl localhost:8100/harness/answer-key
harness answer-key --days 70 -o key.json             # the same, without serving
harness systems
```

API docs: `http://localhost:8100/harness/docs`.

## Adding a system

A system is one module in `src/harness/systems/` with a `SourceSystem` subclass:

```python
class Workday(SourceSystem):
    name = "workday"  # its path: /workday
    title = "Workday HCM (REST and RaaS)"

    def project(self):  # the truth, as this system holds it today
        records, defects = {}, [defect("...", self.name, {}, "...")]
        return records, defects

    def router(self):  # a FastAPI router mocking the system's real API
        ...
```

Import it in `systems/__init__.py`. `self.quirk(rate, *keys)` decides stably which entities a quirk falls on, `self.seen_as_of(lag_days)` gives the system's lag, and `defect(...)` records what VeroMesh should find. Build the API from the vendor's public documentation, and check it against a real tenant when one is available.

## Roadmap

- **Next:**
  - **A real ERP** (Odoo or ERPNext) or a mock of Oracle Fusion: suppliers, purchase orders, invoices and the ledger.
  - **A FHIR EMR** (HAPI FHIR, real software) with synthetic patients, practitioners and encounters, for the Clinician Copilot slice.
- **Then:**
  - a dbt project over the lakehouse, for lineage and quality results;
  - Salesforce, if order-to-cash is in scope;
  - **scenarios:** scripted checks with expected outcomes, scored against the answer key (an agent asks after its principal has left: refused; the AP Manager's post is vacant: consult goes up a level; the invoice column renamed: its consumers and KPI owners are routed).
- **Real sandboxes:** an Entra ID developer tenant and a ServiceNow developer instance. A loader writes the same truth into them, so connectors can be checked against the real APIs.
- **Deploy:** a Helm chart, to run beside VeroMesh in VeroMesh's Azure as the simulated customer's systems.
