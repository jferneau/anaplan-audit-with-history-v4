# Anaplan Audit History v4

[![CI](https://github.com/jferneau/anaplan-audit-with-history-v4/actions/workflows/ci.yml/badge.svg)](https://github.com/jferneau/anaplan-audit-with-history-v4/actions/workflows/ci.yml)

A Python CLI that turns Anaplan's raw audit log — and, optionally, every
model's change history — into report-ready data inside an Anaplan
reporting model.

**v4** is the DuckDB rebuild of the solution: the SQLite storage layer
was replaced with DuckDB (faster, columnar, vectorized CSV ingestion),
the two hot paths were rewritten to push work into the engine, and the
tool can now be shipped as a **single self-contained binary** (no Python
install required on the target machine). The auth flows, API client, and
upload orchestration carry over unchanged.

> **v4 databases are not compatible with v1–v3.** DuckDB and SQLite file
> formats are incompatible; v4 starts from a fresh database. This is
> nearly free — Anaplan's audit API only retains ~30 days, and model
> history re-exports from Anaplan on the first run.

---

## Intended Audience

**Level of Difficulty:** Intermediate. Requires familiarity with Anaplan
REST APIs and a command-line shell (Linux, macOS, or Windows). Running
the packaged binary needs no Python knowledge; running from source needs
Python 3.13+ and [uv](https://docs.astral.sh/uv/). The reporting-model
build is for an experienced Anaplan model builder.

**Access requirements:** Anaplan **Tenant Auditor** role (reads audit
events), **Workspace Administrator** on the target reporting workspace(s),
and Basic, Certificate, or OAuth credentials for the chosen auth mode.

**Estimated effort:** initial deploy + validation against one tenant,
~2–4 hours. Building the reporting model(s), ~2–3 hours per environment
(one-time).

---

## What the tool does

Anaplan's tenant audit log exposes who did what, when, and from where.
This tool extracts it via the REST API, blends it with tenant metadata
(Users, Workspaces, Models, Actions, Processes, Files, CloudWorks
integrations), normalizes everything in DuckDB, and loads it into a
dedicated Anaplan **Audit reporting model**. Optionally, the same
orchestrator exports every model's **change history**, classifies each
change, normalizes it to a flat schema, retention-purges it, and loads it
into a **separate Model History reporting model**.

### Pipeline

```
1. Authenticate (Basic / Cert / OAuth)
   │
   ├─ auditEnabled = true ─────────────────────────────────────────────
   │  2. Fetch metadata (Users · Workspaces · Models · Actions ·
   │     Processes · Files · CloudWorks · activity-code lookup)
   │  3. Fetch audit events (paginated, since lastRun)
   │  4. Load into DuckDB (set-based upsert, self-migrating schema)
   │  5. SQL transform (audit_query.sql — multi-join)
   │  6. Upload to the Audit reporting model + run its process
   │
   └─ modelHistory.enabled = true ────────────────────────────────────
      7. Per model (5 concurrent): export → poll → normalize + classify
         → upsert → backup → purge → upload to the Model History model
```

`auditEnabled` and `modelHistory.enabled` are independent flags — at
least one must be `true`. Audit uploads to `targetAnaplanModel`; Model
History uploads to its **own** `modelHistory.targetAnaplanModel` (required
when Model History is enabled — the two never share a model).

---

## Quick start

### 1. Get the tool

**Option A — packaged binary (no Python needed).** Download the
single-file executable for your platform from the
[latest release](https://github.com/jferneau/anaplan-audit-with-history-v4/releases/latest),
mark it executable, and run `./anaplan-audit version` to confirm it
launches.

**Option B — from source.** Clone the repo (or download the source zip),
then:

| Platform | One-command setup |
|---|---|
| **macOS / Linux** | `bash setup.sh` |
| **Windows** (PowerShell) | `powershell -ExecutionPolicy Bypass -File setup.ps1` |

The script installs `uv`, Python 3.13, and all dependencies, then offers
to launch the config wizard. It's safe to re-run. Manual equivalent:

```bash
uv sync                     # installs Python 3.13 + runtime deps
uv run anaplan-audit init   # interactive config wizard
# …or copy the example and edit by hand:
cp settings.json.example settings.json
```

Developers who want the test suite: `uv sync --all-extras`.

### 2. Choose an authentication mode

| Mode | When to use | Setup |
|---|---|---|
| `basic` | Quick local testing | Set `ANAPLAN_AUDIT_BASIC_USERNAME` / `ANAPLAN_AUDIT_BASIC_PASSWORD` (env vars or a local `.env` — copy `.env.example`) |
| `cert_auth` | Automated / service-account runs | PEM cert paths in `settings.json` |
| `OAuth` | **Recommended for production** | `anaplan-audit register --client-id <ID>` once — it stores the client ID in `settings.json` so later runs refresh unattended |

OAuth tokens are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256)
using a machine-local keyfile with `0600` permissions.

### 3. Configure `settings.json`

[`settings.json.example`](settings.json.example) is the complete
reference — every key with a safe default. Fill in the handful specific
to your tenant:

- `anaplanTenantName`
- `authenticationMode` + credentials (`oauthClientId` is set for you by
  `register`)
- `workspaceModelCombos` — the source workspace/model(s) to audit (names
  or IDs both work; names resolve at runtime)
- `targetAnaplanModel` — the Audit reporting workspace/model and its file
  sources / process (see **Reporting model setup** below)
- `modelHistory.targetAnaplanModel` — the *separate* Model History model
  (required when `modelHistory.enabled` is `true`)

> **One settings file targets one tenant.** For multiple tenants, keep a
> settings file per tenant and run each with `--config /path/to/that.json`.

Precedence (highest wins):
`CLI flag > ANAPLAN_AUDIT_* env var > .env > settings.json > defaults`.

### 4. Validate, dry-run, then go live

```bash
uv run anaplan-audit validate-config          # settings + auth, no data touched
uv run anaplan-audit run --dry-run --verbose  # extract + transform, no upload
uv run anaplan-audit run                       # live: uploads + advances lastRun
```

Every run also writes a full-fidelity JSON log to `logs/run_<timestamp>.log`
(disable with `--no-log-file`, relocate with `--log-dir`), so nothing
scrolls out of reach on a long run.

### 5. Schedule

Wire `anaplan-audit run` into cron, a systemd timer, or a
CloudWorks-triggered job. Typical cadence: audit every 1–4 hours, Model
History nightly. An OS-level exclusive lock makes overlapping invocations
exit with code `7` instead of colliding, so aggressive schedules are safe.

---

## Reporting model setup

The tool uploads a set of CSVs to **named file sources** in the reporting
model, then runs a **process** that imports them. What "needs to be done"
on the Anaplan side is: create those file sources + import actions + lists
+ modules, and sequence the imports in one process. The column contracts
below are what each import maps.

> **Ready-to-use sample files** with synthetic (non-tenant) data live in
> [`examples/`](examples/) — one CSV per file source, exact columns. Upload
> them once to create each file source and define its import mapping, then
> the tool refreshes them on every run.

### Audit model — file → CSV contracts

| File source (`settings.json` key) | Columns |
|---|---|
| `auditEventsFileName` (`AUDIT_LOG.csv`) | The blended audit events — `LOAD_ID, BATCH_ID, AUDIT_ID, EVENT_DATE, USER_NAME, DISPLAY_NAME, WORKSPACE_NAME, MODEL_NAME, ACTION_NAME, EVENT_CATEGORY, UX_APP_ID, UX_APP_NAME, UX_PAGE_ID, UX_PAGE_NAME, …` (60+ cols) |
| `workspacesFileName` (`WORKSPACE_LIST.csv`) | `WS_CT, id, name, active, sizeAllowance, currentSize` |
| `usersFileName` (`USER_LIST.csv`) | `USR_CT, id, userName, displayName, firstName, lastName` |
| `modelsFileName` (`MODEL_LIST.csv`) | model metadata (`id, name, activeState, lastModified, …`) |
| `actionsFileName` (`ACTION_LIST.csv`) | `ACT_CT, id, name, type, workspace_id, model_id, workspace_name, model_name` |
| `filesFileName` (`FILE_LIST.csv`) | `FILE_CT, id, name, …, workspace_id, model_id, workspace_name, model_name` |
| `cloudworksFileName` (`CLOUDWORKS_LIST.csv`) | CloudWorks integration metadata (dotted `latestRun.*` / `schedule.*`) |
| `activityCodesFileName` (`ACTIVITY_CODES.csv`) | the ~220-code activity catalog |

Optional **UX / attribution staging lists** (uploaded only when their
file-name key is set): `uxAppListFileName` (`code,name`) and
`uxPageListFileName` (`code,name,parent_code`), plus CloudWorks / Action /
Process / Role / Target-User lists.

### Model History model — file → CSV contracts

| File source | Columns |
|---|---|
| `MODEL_REGISTRY.csv` | `model_id, model_name, workspace_id, workspace_name, last_synced_at` |
| `MODEL_HISTORY_LIST.csv` | `record_id, model_id, date_time_utc` |
| `MODEL_HISTORY_NORMALIZED.csv` | the flat change schema + derived `change_type` / `object_type` (last two columns) |

### Key conventions to match when building the imports

- **snake_case provenance.** `actions` / `files` (and `processes`) carry
  `workspace_id`, `model_id`, `workspace_name`, `model_name`. Map those,
  or resolve names with `FINDITEM(WORKSPACE/MODEL, …)` off the ids.
- **Clear "All items" on the metadata imports.** The tool only fetches
  actions/files for the models currently in `workspaceModelCombos`, so
  clearing on import keeps each SYS module a clean mirror of your current
  scope instead of accumulating stale rows from prior scopes.
- **UX pages are hierarchical.** `UX_PAGE_LIST.csv` carries `parent_code`
  (the app's id) — map it to the `UX_PAGE` list's Parent so pages nest
  under their `UX_APP`. Import `UX_APP` **before** `UX_PAGE` in the process.
- **`change_type` / `object_type` are controlled-vocabulary.** If those
  reporting-model line items are list-formatted, pre-load the matching
  lists (`MH_CHANGE_TYPES`, `MH_OBJECT_TYPES`, incl. the catch-all
  `Model change (no details available)`) or those cells fail to import.
- **`type` on actions** is the action kind (`IMPORT` / `EXPORT` /
  `DELETE_BY_SELECTION` / `PROCESS`).

---

## CLI reference

```
anaplan-audit init              Interactive wizard — writes a minimal settings.json
  --output PATH                 Where to write (default: ./settings.json)
  --force                       Overwrite an existing file

anaplan-audit run               Full pipeline: extract → transform → upload
  --config PATH                 Path to settings.json (default: ./settings.json)
  --verbose / -v                DEBUG-level detail (hides HTTP wire chatter)
  --debug                       Also show HTTP wire chatter (network debugging)
  --json                        Force JSON console output (auto when piped)
  --dry-run                     Extract + transform only, skip upload
  --since EPOCH                 Override lastRun for this execution (Unix seconds)
  --limit N                     Fetch at most N audit events (bounded sample runs)
  --log-dir PATH                Directory for the per-run JSON log (default: ./logs)
  --no-log-file                 Do not write a per-run log file

anaplan-audit register          One-time OAuth device registration
  --client-id TEXT              OAuth client ID (persisted to settings.json on success)

anaplan-audit validate-config   Validate settings AND test authentication
  --skip-auth                   Settings-only validation (offline)

anaplan-audit mh-unmatched      Report model-history descriptions no rule matched
anaplan-audit version           Print version + probe bundled resources
```

---

## What's tracked

The activity-code catalog ships in
[`src/anaplan_audit/data/activity_events.csv`](src/anaplan_audit/data/activity_events.csv)
and loads into the reporting model's `act_codes` list on every upload. It
covers every category Anaplan publishes today: user activity (`USR-*`),
access control (`AUTHZ-*`), connection management (`CONN-*`),
encryption/BYOK (`DSM-*`), CloudWorks (`INT-0*`), Anaplan Data
Orchestrator (`INT-5*`/`INT-6*`), Workflow tasks & templates (`WF-*`),
comments (`COMMENT-*`), and Forecaster (`FRCST-*`, which replaced legacy
`PIQ-*`).

The tool is **forward-compatible**: unknown event-type ids flow through
with their raw code, and unknown `additionalAttributes` keys are added to
the events table (`ALTER TABLE ADD COLUMN`) at write time. Bumping the
activity-code CSV as Anaplan publishes new codes is the only routine
maintenance.

---

## Model History classification

Each model-history row's free-text `Description` is mapped to two
controlled-vocabulary columns appended to `MODEL_HISTORY_NORMALIZED.csv`:

- `change_type` — the normalized change (identity-mapped from Anaplan's
  own short description vocabulary; e.g. `Add Item`, `User Role Changed`).
- `object_type` — a coarse 12-bucket object dimension (Module/List, Line
  Item/Property, User, Role, …).

Rules and vocabularies are bundled CSVs under
`src/anaplan_audit/model_history/data/` — edit those (no code change) to
adjust. Any description that matches no rule is surfaced by
`anaplan-audit mh-unmatched` so the vocabulary can be extended. On re-run,
the two derived columns are refreshed for existing rows, so rule changes
propagate.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic failure |
| 2 | Config error (fix `settings.json`, re-run) |
| 3 | Auth failure (check credentials / re-register OAuth) |
| 4 | API failure after retries (retry later) |
| 5 | DuckDB / SQL failure |
| 6 | Model History failure (never escalates — logged as a warning) |
| 7 | Another instance is already running |

Schedulers can branch on the code to alert vs. retry.

---

## FAQ

**Do I need Python?** Only for Option B (from source) or to run the tests.
The packaged binary bundles its own runtime.

**Do I need pytest / mypy / ruff?** No — those are developer tools.
`setup.sh` / `uv sync` install only what's needed to *run* the tool.
Developers use `uv sync --all-extras`.

**How long is data retained?** Anaplan's audit API exposes ~30 days, but
the tool dedups and upserts every run, so the DuckDB database retains
every event ever seen. Audit events are kept forever by default (set
`auditRetentionYears` to purge, with an automatic backup first); Model
History defaults to `retentionYears: 2`. All windows are configurable.

**Can audit and Model History run on different schedules?** Yes — they're
independently toggleable. Use two settings files with different flags and
two scheduled invocations.

**Where do logs go?** Structured JSON to stderr (ready for journald /
Splunk / Datadog / CloudWatch) plus a per-run JSON file under `logs/`.
`--verbose` switches the console to rich formatting for interactive runs.

---

## Upgrading from v1–v3

v4 starts from a fresh DuckDB database (the SQLite file is not migrated —
the formats are incompatible). Steps: stop the old scheduler; install v4;
re-register OAuth tokens if used; point `database` at a new file; then
`validate-config` → `run --dry-run --verbose` → schedule. `lastRun` is
Unix **seconds** (as in v3); the first run over-fetches once and converges.

---

## Maintainer

The downloader of this solution is expected to maintain it themselves.
Feedback and issues: open a GitHub issue on this repository.

## License

MIT — see [LICENSE](LICENSE).
