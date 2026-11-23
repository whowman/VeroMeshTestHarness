# VeroMesh test harness

A simulated company and its source systems, for testing [VeroMesh](https://github.com/whowman/VeroMeshOntology) end to end before a design partner's real systems are available, and as the acceptance tests to run against theirs.

VeroMesh's connectors read the simulated systems through each platform's public API (Microsoft Graph, the ServiceNow Table API and the rest), exactly as they would at a customer. [docs/api-fidelity.md](docs/api-fidelity.md) lists, for each system, the public documentation the mock follows, what it covers and what it simplifies. The harness knows the truth behind every system, so what VeroMesh concludes can be scored: every quirk a system introduces is written down in the **answer key**.

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
   Entra ID      ServiceNow     Workday       Databricks     Oracle ERP, EMR (FHIR), dbt, SharePoint, Stardog
   Graph API     Table API      REST, RaaS    Unity Catalog  (systems/: each behind a mock of its real API)
       │             │
       └──────┬──────┘                       answer key: every defect,
              ▼                              the events, the true owners
        VeroMesh connectors  ─────────────▶  scored against it
```

1. **The truth** is one reproducible company generated from a profile, a size and a seed. The same three always give the same company. People keep a history of positions, so a system can lag behind the truth. The default profile, `healthcare`, is Kestrel Valley Health: a health system with clinical, finance, HR, IT, data and compliance functions. It has clinicians and decision rights, AI agents (a Clinician Copilot, an Invoice Assistant) and a lakehouse with clinical, finance and people data. All names are invented.
2. **The clock** moves the company forward:
   - the everyday churn, about 1% of the headcount each month for each of hires, moves and leavers;
   - a script of changes the gates must catch: the Accounts Payable Manager leaves (day 20) and the post stays vacant until a new one is hired (day 50), Clinical Informatics moves under Data & Analytics (day 45), an invoice column is renamed in the lakehouse (day 60, the Gate 3 breaking change), the person the Invoice Assistant acts for leaves (day 75), and the revenue cycle platform is retired (day 120).
3. **Each system** is a projection of the truth into that system's shape, IDs and quirks, served behind a mock of its real API. Each quirk is recorded as a defect, so it's known what VeroMesh should find.
4. **Scenarios** (`GET /harness/scenarios`) are the questions VeroMesh must answer about the company, each with its expected answer, worked out from the truth on the day it's asked. `POST /harness/score` scores VeroMesh's answers: exact matches for values, precision and recall for lists, with what's missing and what's extra. A scenario becomes askable when the story reaches it (see below).
5. **The answer key** (`GET /harness/answer-key`) has every system's defects, the events so far, and the truth: who works where with which roles, who owns each system, data asset and KPI, and which agents' principals are still active.

## Systems

| System | Path | API | Quirks (defects) |
| --- | --- | --- | --- |
| Microsoft Entra ID | `/entra` | Microsoft Graph v1.0: users (with `$select`, `$filter`, `$top`, `$expand=manager`, paging), managers, `memberOf`, groups and members, service principals (the AI agents) and their owners, `users/delta`; OAuth client credentials at `/entra/{tenant}/oauth2/v2.0/token` | name-variant, stale-manager, missing-mail, leaver-account-enabled, contractor-only-here |
| ServiceNow | `/servicenow` | Table API: `sys_user`, `cmn_department`, `sys_user_group`, `sys_user_grmember`, `cmdb_ci_business_app`, `cmdb_ci_server`, `cmdb_ci_db_instance`, `cmdb_rel_ci`, `incident`, `change_request`. Encoded queries, `sysparm_fields`, `sysparm_display_value`, paging with `X-Total-Count` and `Link`; basic auth or `/oauth_token.do` | stale-user, leaver-still-active, no-technical-owner, duplicate-ci, owner-is-leaver |
| Workday HCM | `/workday` | REST API: `/ccx/api/v1/{tenant}/workers` (with `limit`, `offset`, `search`), `workers/{id}`, `supervisoryOrganizations`. Reports as a Service: `/ccx/service/customreport2/{tenant}/{owner}/VeroMesh_Workers` and `VeroMesh_Worker_Changes` (`Updated_From`, `Updated_Through`). OAuth at `/ccx/oauth2/{tenant}/token` (refresh token or client credentials). Employees only: no contractors | retro-termination, vacant-supervisory-org, legal-and-preferred-name |
| Databricks | `/databricks` | Unity Catalog: `/api/2.1/unity-catalog/catalogs`, `schemas`, `tables` (columns, owner, properties, paging), `tables/{full_name}`. Lineage: `/api/2.0/lineage-tracking/table-lineage`. SQL Statement Execution (`/api/2.0/sql/statements`): column and table tags from `system.information_schema`, and `SELECT * FROM <table> LIMIT n` returning simulated records (encounters, medication orders, invoices, workers) consistent with the truth. A personal access token, or OAuth at `/oidc/v1/token` | owner-is-group, owner-is-leaver, untagged-sensitive-column, schema-drift |
| Oracle Fusion ERP | `/erp` | REST API: `/fscmRestApi/resources/11.13.18.05/suppliers`, `purchaseOrders`, `invoices` (each with its approvers) and `/hcmRestApi/resources/11.13.18.05/userAccounts` (with job roles). Paging with `limit`, `offset`, `hasMore` and `totalResults=true`; filters with `q` (`Field op value;...`). Basic auth. Every invoice is approved by someone who held the authority that day | approved-over-limit, missing-co-approval, self-approved-po, leaver-retains-role |
| EMR (Epic, FHIR R4) | `/emr` | `/fhir/Organization`, `Practitioner` (with NPI), `PractitionerRole` (department, role and clinical privileges), `Patient`, `Encounter`, `MedicationRequest`; searchset Bundles with a `next` link, `_count`, `_offset`, `identifier`, `active`, `practitioner`, `subject`, `_lastUpdated`; `/fhir/metadata`. Client credentials at `/oauth2/token`. Patients are synthetic; practitioners are the company's clinicians | practitioner-active-after-leaving, missing-npi, privilege-not-in-hr |
| dbt | `/dbt` | The run's artifacts: `manifest.json` (sources, models with their dependencies, columns with classification, owners, tests), `run_results.json` (the nightly run), `catalog.json` | model-failing (after the breaking change; the models after it are skipped) |
| SharePoint | `/sharepoint` | Microsoft Graph: `/v1.0/sites` (`?search=`), `sites/{id}/drive`, `drives/{id}/root/children`, `items/{id}/children`, `items/{id}`, `items/{id}/content` (markdown), `root/delta`; the Graph token from `/entra`. Sites for Finance, Clinical Operations, IT, Data Governance and People hold the delegation of authority policy, an escalation procedure, an SOP, a charter, a runbook per system, the data owner register, monthly council minutes and an org chart per unit. Each document's statements are checked against the truth every day | policy-system-mismatch, register-conflicts-with-catalog; and as the clock moves, document-names-leaver, document-outdated |
| Semantic layer (Stardog) | `/stardog` | Stardog's HTTP API: `/admin/databases`, SPARQL at `/{db}/query` (GET or POST), `application/sparql-results+json`; basic auth. Business terms with definitions and domains (Patient/Individual, Clinical Encounter, Provider, Terminology, Claims/Coverage, Finance), each mapped to the lakehouse tables or columns that hold it, and no owners: who is accountable is what VeroMesh adds | unmapped-term, overlapping-terms; after the breaking change, stale-mapping |

Lag is part of the simulation. Workday is the record, though some terminations are entered a few days late. Entra ID is provisioned from HR nightly. ServiceNow's users sync weekly and its CMDB owners are reviewed monthly. Unity Catalog's owners change only when someone sets them. So a change lands in each system at a different time, as it would at a real company.

Mock credentials (test values only): OAuth client `veromesh-connector` / `harness-secret` (Graph, Workday, Databricks, the EMR); ERP `integration.user` / `harness`; semantic layer `admin` / `harness`; Workday refresh token `harness-workday-refresh`; ServiceNow `admin` / `harness`; Databricks token `dapi-harness-token`.

## Difficulty

`--difficulty` scales every quirk's rate. At `0` there are no quirks and no sync lag, so every system agrees with the truth at once: a clean baseline. At `1` the quirks are as described; at `2` there are twice as many. `--quirk KIND=RATE` (repeatable) sets one quirk by its defect kind, for example `--quirk name-variant=0.9 --quirk duplicate-ci=0`. On a running harness, use `GET` or `PUT /harness/difficulty` with `{"scale": 0, "overrides": {}}`. Some defects are facts about the company, not system quirks, and stay at every difficulty: contractors with no HR record, a vacant post, the breaking change and what it breaks (including the semantic mapping to the renamed column), and documents written before a change.

## Misbehaving systems

`--chaos 0.05` (or `PUT /harness/chaos` with `{"rate": 0.05, "kinds": [...], "slow_ms": 1500, "seed": 1}`) makes 5% of system requests fail the way real APIs do:
- **throttle:** 429 with `Retry-After`;
- **server-error:** 500;
- **unavailable:** 503 with `Retry-After`;
- **token-expired:** 401 with an `invalid_token` challenge;
- **slow:** the answer arrives late.

Which requests fail depends only on their order and the seed, so a connector failure can be reproduced. `GET /harness/chaos` counts what was injected. The harness's own `/harness` routes always answer.

## Scenarios

| Scenario | Gate | From day | What VeroMesh must answer |
| --- | --- | ---: | --- |
| identity-resolution | 1 | 0 | Everyone working, merged across Workday, Entra ID, ServiceNow and the EMR; who has no HR record; the name variants merged |
| agent-within-limit | 1 | 0 | The Invoice Assistant approves 800 USD: allow (refuse once its principal has left) |
| agent-over-limit | 1 | 0 | It asks for 12,000 USD: consult the AP Manager, or a finance approver while that post is vacant |
| agent-principal-left | 1 | 75 | Its principal has left: refuse |
| leaver-access | 1 | 1 | People who have left and can still act somewhere: an enabled account, an active practitioner record, an ERP role |
| prescribing-privilege | 1 | 0 | Who may sign the orders the Clinician Copilot drafts; EMR privileges HR does not give |
| sensitive-data | 1 | 0 | Every PHI and PII column, including the one the catalog has not tagged |
| segregation-of-duties | 1 | 0 | Purchase orders raised and approved by the same person |
| document-claims | 1 | 0 | The statements in the SharePoint documents that are out of date or contradict the systems |
| system-ownership | 2 | 0 | Each system's owners; the CMDB's duplicate, its application with no technical owner, its owners who have left |
| reorganization | 2 | 45 | Whom the head of Clinical Informatics now reports to |
| vacant-owner | 2 | 20 | Who owns the on-time payment KPI while its owner's post is vacant, and where its decisions go |
| breaking-change | 3 | 60 | What the renamed column affects (assets, KPIs, the failing model, the stale semantic mapping) and whom to notify |
| semantic-context | 2 | 0 | Who is accountable for each semantic term (the owners of the tables it maps to); unmapped terms; broken mappings |

**The Clinician Copilot slice.** Eleven scenarios put the questions a clinical AI copilot asks before it acts. Each names a real patient in the EMR and a real clinician, and expects the outcome (allow, draft for signature, consult or refuse), the roles and people who must sign or review, and the data's owner. The rules are in the profile (`COPILOT_RULES`).

| Scenario | The ask | Expected |
| --- | --- | --- |
| copilot-history-treating | Summarize the history of a patient the requester treats | allow; the data's owner |
| copilot-history-not-treating | The same, from a clinician not treating the patient | refuse: no care relationship |
| copilot-care-gap | Identify a care gap | allow; route to the patient's general practitioner |
| copilot-draft-order | Draft a medication order | draft for the treating prescriber to sign; pharmacist review |
| copilot-draft-order-nurse | A nurse asks for an order | drafted for the patient's treating prescribers, never the nurse, even one whose EMR privileges say otherwise |
| copilot-high-alert | Draft a high-alert medication order | draft; two independent pharmacist checks |
| copilot-interaction | Cross-reference the patient's medications | consult the prescriber and a pharmacist: the patient takes warfarin and aspirin |
| copilot-prior-auth-within | A prior authorization of 8,000 USD | a utilization review nurse decides |
| copilot-prior-auth-above | A prior authorization of 60,000 USD | the utilization management medical director decides |
| copilot-prior-auth-deny | Deny a prior authorization | always the medical director |
| copilot-record-release | Release the records to an outside provider | the privacy officer decides |

A role with no one in it today goes to the head of the unit that owns the rule.

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

**On one machine:** `docker compose up` (`HARNESS_PEOPLE`, `HARNESS_SEED` and `HARNESS_DIFFICULTY` set the company).

**Beside VeroMesh on Kubernetes:** `deploy/helm/veromesh-test-harness`. `company.*` sets the profile, size, seed, starting day, difficulty, quirks and systems. `networkPolicy.fromNamespaces` lists the namespaces allowed to reach it, such as VeroMesh's. It runs as one replica, because the clock lives in memory, with a read-only root filesystem and no egress.

```bash
helm install harness deploy/helm/veromesh-test-harness -n veromesh-harness --create-namespace \
  --set 'networkPolicy.fromNamespaces={veromesh}' --set company.people=20000
# VeroMesh's connectors then use http://harness-test-harness.veromesh-harness:8100/<system>
```

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
  - a runner that puts each scenario to VeroMesh's API and posts its answers to `/harness/score`, once VeroMesh has connectors for these systems;
  - Salesforce, if order-to-cash is in scope;
  - more profiles (a payer, a manufacturer), and the systems a design partner names.
- **Real sandboxes:** an Entra ID developer tenant and a ServiceNow developer instance. A loader writes the same truth into them, so connectors can be checked against the real APIs.
- **Teams messages** beside the documents (who actually decides, as against what the policy says), and documents in Word and PDF as well as markdown.
