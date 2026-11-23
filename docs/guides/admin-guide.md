---
title: VeroMesh Test Harness — Administrator Guide
---

<!--
Source of the Word copy: scripts/guides/build_docx.py builds admin-guide.docx from this file
in CI. Edit here, never in Word. Sections marked "To write" are still skeleton.
-->

# About this guide

This guide is for the people who run the test harness and extend it: deploying it beside VeroMesh, choosing the company it simulates, and adding systems, profiles and scenarios. Using it to test a connector is covered in the **User Guide**.

# What the harness is

The harness simulates a company and the systems that hold its data, behind mocks of each system's **public API**. Connectors under test call those APIs exactly as they would call the real systems. Because the harness knows the truth, it also knows what each system gets wrong, and so what VeroMesh should find.

| Part | What it does | Code |
| --- | --- | --- |
| Truth | A seeded company: units, positions, people and their history, systems, data, agents | `src/harness/truth.py`, `src/harness/profiles/` |
| Clock | Scripted events and monthly churn as days pass | `src/harness/clock.py` |
| Systems | Each system's view of the truth, its quirks, and its API | `src/harness/systems/` |
| Answer key | The defects each system holds today | `GET /harness/answer-key` |
| Scenarios | Questions for VeroMesh with expected answers, and a scorer | `src/harness/scenarios.py`, `src/harness/copilot.py` |

Systems today: Microsoft Entra ID, ServiceNow, Workday HCM, Databricks, Oracle Fusion ERP, an EMR (FHIR R4), dbt, SharePoint and a semantic layer (Stardog). The README lists each system's API and quirks, and `docs/api-fidelity.md` links each mocked endpoint to the vendor's documentation.

# Deploying

## On a laptop

```bash
pip install -e ".[test]"
harness serve --people 2000 --seed 7 --port 8100
```

## With Docker

`docker compose up`. `HARNESS_PEOPLE`, `HARNESS_SEED` and `HARNESS_DIFFICULTY` set the company.

## Beside VeroMesh on Kubernetes

```bash
helm install harness deploy/helm/veromesh-test-harness -n veromesh-harness --create-namespace \
  --set 'networkPolicy.fromNamespaces={veromesh}' --set company.people=20000
```

VeroMesh's connectors then reach each system at `http://harness-test-harness.veromesh-harness:8100/<system>`. The harness runs as one replica (the clock lives in memory), with a read-only root filesystem and no outbound network.

To write: the chart values table (`company.*`, `networkPolicy.*`, resources) and sizing by number of people.

# Configuring the company

| Setting | Flag | Chart value | Default |
| --- | --- | --- | --- |
| Profile (industry) | `--profile` | `company.profile` | `healthcare` |
| Number of people | `--people` | `company.people` | 2000 |
| Seed | `--seed` | `company.seed` | 7 |
| Starting day | `--days` | `company.days` | 0 |
| Difficulty | `--difficulty` | `company.difficulty` | 1 |
| One quirk's rate | `--quirk KIND=RATE` | `company.quirks.KIND` | from the profile |
| Systems served | `--systems a,b` | `company.systems` | all |
| Misbehaving requests | `--chaos RATE` | — | 0 |

The same seed and settings always produce the same company, the same quirks and the same answer key.

**Difficulty** scales every quirk's rate; at `0` there are no quirks and no sync lag (a clean baseline). Some defects are facts about the company and stay at every difficulty (contractors with no HR record, a vacant post, the breaking change). Change it on a running harness with `PUT /harness/difficulty`.

**Chaos** makes a share of system requests fail as real APIs do (429, 500, 503, an expired token, a slow answer), reproducibly for a seed. Set it with `PUT /harness/chaos`.

# Credentials of the mock systems

The mocks accept fixed test credentials; none of them is a secret.

To write: the table of each system's sign-in (token URL, client ID and secret, or basic-auth user) from the system modules.

# Adding a system

1. Read the vendor's public API documentation for the endpoints a connector would use.
2. Add a module in `src/harness/systems/` with a `SourceSystem` subclass: `project()` returns the system's records and defects; `router()` returns a FastAPI router mocking the API, under `/<name>`.
3. Use `self.quirk(rate, kind, *keys)` for quirks (stable per entity, scaled by difficulty), `self.seen_as_of(days)` for sync lag, and `owner_on` / `parent_on` for what was true on a past date.
4. Register it in `systems/__init__.py`.
5. Add tests in `tests/` that call the API as a connector would, and rows in the README and `docs/api-fidelity.md`.

To write: a worked example, and the conventions for IDs, paging and error bodies.

# Adding a profile or a scenario

To write: what a profile defines (units, roles, systems, data assets, lineage, rules), and how a scenario states its question, expected answer and scoring (lists are scored by precision and recall).

# Upgrading

To write: versioning of the harness, and keeping it in step with the connectors it tests.
