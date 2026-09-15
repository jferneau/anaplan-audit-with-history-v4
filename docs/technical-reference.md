---
title: "Anaplan Audit History v4 — Technical Reference"
author: "Jon Ferneau, Operational Excellence Group (OEG)"
date: "September 2026"
---

# 1. Overview

## 1.1 What this project is

A Python 3.13 CLI (`anaplan-audit`) that extracts Anaplan tenant audit
events and, optionally, per-model change history via the Anaplan REST
APIs, blends them with metadata (Users, Workspaces, Models, Actions,
Processes, Files, CloudWorks integrations), transforms the result in
**DuckDB**, and loads the report-ready data into a dedicated Anaplan
reporting model.

The pipeline is a seven-step orchestrator that runs end-to-end under a
single OS-level run lock:

1. **Authenticate** (basic / certificate / OAuth)
2. **Fetch metadata** (Users · Workspaces · Models · Actions · Processes · Files · CloudWorks integrations · activity-code catalog)
3. **Fetch audit events** (paginated, since `lastRun`)
4. **Load into DuckDB** (event upsert, activity-catalog augmentation, staging views)
5. **Run SQL transform** (`audit_query.sql` — multi-join)
6. **Upload** to the Anaplan audit reporting model (incremental delta by default)
7. **(Optional) Model History** — per-model: trigger export → poll → normalize → classify → upsert → backup → purge → upload

Steps 2–6 are gated by `auditEnabled` (default `true`). Step 7 is
gated by `modelHistory.enabled` (default `false`). The two flags are
independent; at least one must be `true`.

## 1.2 Stack summary

| Concern | v1 | v4 |
|---|---|---|
| Language | Python 3.11 | Python 3.13 |
| Storage engine | SQLite | **DuckDB (≥ 1.5)** |
| Packaging | `pip` + `requirements.txt` | `uv` + `pyproject.toml`; ships as a single **PyInstaller** executable (no Python required) |
| HTTP client | `requests` | `httpx` (sync, HTTP/2, persistent client) |
| Retries | None | `tenacity` — exponential backoff + jitter, 5 attempts, honors `Retry-After` |
| Config | Module globals + JSON | `pydantic-settings` (CLI > env > `.env` > `settings.json` > defaults) |
| Auth token store | JWT keyed by `client_id` | Fernet (AES-128-CBC + HMAC-SHA256) with `0600` keyfile |
| Concurrency | Not protected | OS-level run lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows) + `ThreadPoolExecutor` for per-model exports |
| Logging | `logging` stdlib (plain text) | `structlog` (JSON, rich console with `--verbose`, per-run log file) |
| Audit catalog | ~140 codes | **231 codes** — full coverage of Anaplan's current event catalog, self-extending from the live stream |
| Audit fact load | Full reload every run | **Incremental delta** — only new `AUDIT_ID`s are uploaded |
| EVENT_ID naming | Code-named | **Message-first** — the human message becomes the list-item name, degrading only as Anaplan's 60-char/uniqueness rules force |

Why DuckDB: the transform is a set of wide multi-table joins over the
whole event history, which is exactly DuckDB's columnar/OLAP sweet spot.
It runs in-process (no server), stores the working set in one file, and
removed the SQLite-era `PRAGMA` tuning entirely.

---

# 2. Pipeline

## 2.1 Pipeline stages

| # | Stage | Details |
|---|---|---|
| 1 | Authenticate | Dispatches by `authenticationMode`: `basic` / `cert_auth` / `OAuth`. Returns an `AuthToken`; the client refreshes it proactively ~5 minutes before expiry. |
| 2 | Fetch metadata | Workspaces and models are listed tenant-wide (for complete name lookups); actions, processes, and files are fetched only for the selected `workspaceModelCombos`. Users come from SCIM, integrations from CloudWorks, and the bundled `activity_events.csv` seeds the catalog. |
| 3 | Fetch audit events | Paginated `GET` since `lastRun` via a generator that yields one event at a time — peak memory is bounded by the batch size. Audit events are read **tenant-wide**. |
| 4 | Load into DuckDB | `pd.json_normalize` flattens nested `additionalAttributes` into dotted columns; events are **upserted** (`ON CONFLICT(id)`), so the local store accumulates history beyond Anaplan's ~30-day window. The activity catalog is then augmented (Section 8) and the additionalAttributes staging views refreshed (Section 6.5). |
| 5 | SQL transform | Executes `audit_query.sql` (a multi-join over events × users × workspaces × models × cloudworks × actions × act_codes) and returns a `pandas.DataFrame`. |
| 6 | Upload | Uploads the transformed CSV(s) to named file sources and runs the model's process. The audit-fact file carries **only the delta** by default (Section 9). Metadata tables are re-pushed in full. |
| 7 | Model History | Optional. Iterates every in-scope model, exports change history in parallel, normalizes the dynamic CSV into a fixed flat schema, classifies each change, upserts the three tables, backs up the database, purges beyond the retention window, then uploads to the dedicated Model History reporting model. |

## 2.2 Retry policy

| Policy parameter | Value |
|---|---|
| Library | `tenacity` |
| Backoff | exponential with jitter, floored by any `Retry-After` header |
| Attempts | 5 |
| Retried on | HTTP 429, 500, 502, 503, 504; timeouts; network errors |
| Rate-limit handling | `RateLimitError` carries the `Retry-After` value, used as a floor for the next wait |
| On final failure | Raises a subclass of `APIError`; the orchestrator exits with code 4 |

## 2.3 DuckDB storage mechanics

DuckDB replaces SQLite for all local storage. The plumbing differs where
DuckDB has no SQLite-compatible shortcut:

| Concern | How v4 does it |
|---|---|
| Session timezone | `SET TimeZone = 'UTC'` on **every** connection. DuckDB's session timezone defaults to the host's local zone and `strftime` renders in session time — without this, every formatted timestamp would silently shift by the host's UTC offset. |
| Bulk load | `register()` a DataFrame + `CREATE OR REPLACE TABLE … AS SELECT` (metadata tables) instead of `df.to_sql`. |
| Event upsert | `INSERT … ON CONFLICT(id) DO UPDATE` — content-based idempotency on the audit event `id`, so overlapping fetch windows never duplicate. |
| Schema version | Stored in a `_schema_meta` table (DuckDB has no `PRAGMA user_version`). Current events-schema version: **2**. |
| Model-history integrity | The model-history tables declare **no** `FOREIGN KEY`s. DuckDB executes `UPDATE` as `DELETE`+`INSERT`, which would raise a spurious FK violation when re-upserting a parent that has children; insert order in `upsert_model_history()` preserves the invariant instead. |
| No journal/synchronous PRAGMAs | DuckDB manages its own WAL and enforces declared types without opt-in pragmas — the SQLite-era tuning is gone. |

### Events-table schema (typed, self-migrating)

DuckDB enforces column types (SQLite's dynamic typing tolerated
anything), so the type policy is explicit: four fields are non-`VARCHAR`
(`eventDate`/`createdDate`/`index` → `BIGINT`, `success` → `BOOLEAN`);
everything else — including every dynamically discovered
`additionalAttributes.*` column — is `VARCHAR`.

Anaplan ships new audit categories continuously, each of which can carry
`additionalAttributes.*` keys the first batch never contained. Before
each write, the loader reconciles the table schema against the incoming
DataFrame and issues `ALTER TABLE … ADD COLUMN` for anything new. A list
of well-known optional columns (`additionalAttributes.appId`, `.pageId`,
`.pageName`, `.pipelineId`, `.dataspaceId`, `.scheduleId`,
`.connectionId`, `.taskId`, `.workflowTemplateId`, `.commentId`, …) is
pre-declared so the SQL transform never errors on a tenant that hasn't
yet emitted events in those categories. This is what keeps v4
forward-compatible with Anaplan's evolving catalog without code edits.

---

# 3. Module Reference

## 3.1 Package layout

```
src/anaplan_audit/
├── __init__.py
├── __main__.py
├── api/
│   ├── audit.py            # Audit API client (paginated generator)
│   ├── client.py           # httpx client + tenacity retry + token refresh
│   ├── cloudworks.py       # CloudWorks integration metadata
│   ├── integration.py      # Integration API (workspaces, models, actions, processes, files)
│   ├── models.py           # Pydantic response models (extra="allow")
│   ├── scim.py             # SCIM user metadata
│   └── transactional.py    # Transactional API — list items, module cells (refresh log, list sync)
├── auth/
│   ├── basic.py            # Username + password
│   ├── cert.py             # PEM cert + passphrase
│   ├── oauth.py            # Device-grant + refresh token
│   ├── models.py           # AuthToken with proactive expiry check
│   └── token_store.py      # Fernet-encrypted token persistence
├── backfill.py             # Re-parse additionalAttributes on historical events
├── cli.py                  # typer commands (Section 4 of the Operations Runbook)
├── config.py               # pydantic-settings — layered config
├── data/
│   └── activity_events.csv # Event-code catalog (231 codes)
├── exceptions.py           # Typed hierarchy with exit codes 1–7
├── logging_config.py       # structlog JSON / rich setup + per-run log file
├── model_history/
│   ├── classification.py   # change_type / object_type rule engine
│   ├── history_service.py  # Trigger + poll + download exports
│   ├── history_transform_service.py  # Streaming csv.reader normalize
│   └── upload.py           # File upload + Anaplan process trigger
├── orchestrator.py         # 7-step pipeline + run lock + token factory
├── taxonomy.py             # Prefix → EVENT_ID category (single source of truth)
├── transform/
│   ├── additional_attributes.py  # Extractor + staging views
│   ├── catalog.py          # Activity-catalog augmentation + Event Name derivation
│   ├── loader.py           # DuckDB loader, upsert, staging views, backup/purge
│   ├── runner.py           # Executes audit_query.sql, returns DataFrame
│   └── queries/
│       └── audit_query.sql # Multi-join SQL transform
└── upload.py               # Top-level audit upload to Anaplan (incremental delta)
```

## 3.2 Module descriptions

| Module | Responsibility |
|---|---|
| `api.client` | The single `httpx.Client` shared across the run. Wraps every request with `tenacity` retry, and refreshes the `AuthToken` proactively (5-min margin) via the `token_factory` callable. A lock serializes concurrent refresh attempts. |
| `api.audit` | Generator that yields one `AuditEvent` at a time, paginated from `lastRun`. |
| `api.transactional` | Anaplan Transactional API — reads a list's existing item identifiers (paginated), adds list items, and writes module cells. Powers the incremental-delta diff, the list sync, and the refresh log. |
| `api.models` | All response models use `extra="allow"` so new top-level fields survive deserialization. |
| `auth.token_store` | AES-128-CBC + HMAC-SHA256 (`cryptography.fernet`). Keyfile lives under the user's home with `0600` permissions. |
| `config` | `pydantic-settings` Settings model. Field validators catch common misconfigurations (stale `lastRun`, missing cert paths, both pipelines disabled) at startup. |
| `taxonomy` | The single source of truth for EVENT_ID parentage: a prefix → `(parent_code, parent_name)` map, projected into SQL as the `event_parent_name` / `event_parent_code` UDFs so the derivation is never duplicated. |
| `transform.catalog` | Augments the activity catalog with a prefix-derived `Parent` and every code observed in the stream, prefers the live `events.message` over the shipped text, and computes the message-first `Event Name` (Section 7). |
| `transform.loader` | DuckDB load, event upsert, additionalAttributes staging views, `backup_database()`, and purge helpers. |
| `transform.additional_attributes` | Flattens and extracts the `additionalAttributes` payload into named columns and defines the per-category staging views. |
| `transform.runner` | Loads `audit_query.sql` via `importlib.resources`, substitutes template variables, executes, and returns a DataFrame. |
| `model_history.classification` | Rule-based mapping of a change's free-text `description` to controlled `change_type` / `object_type` values. |
| `model_history.history_transform_service` | Streams the dynamic export CSV via `csv.reader` and normalizes into the fixed schema (Section 7). |
| `upload` | Uploads to Anaplan. Computes the audit-fact **delta** (Section 9), runs the process, writes the refresh log, and syncs safety-net lists. |
| `orchestrator` | The seven-step pipeline. Holds the `_RunLock`, wires the token factory, manages the `ThreadPoolExecutor` for parallel model exports, and surfaces typed exceptions to the CLI. |

---

# 4. Configuration

## 4.1 Selected top-level fields

Full descriptions live in the README's Settings reference and in the
annotated `settings.json.example`; the fields most relevant to internals:

| Key | Type | Default | Description |
|---|---|---|---|
| `anaplanTenantName` | string | (required) | Tenant name. Injected into `audit_query.sql` as the tenant name and stamped on every row. |
| `authenticationMode` | string | `OAuth` | One of `basic`, `cert_auth`, `OAuth`. |
| `database` | string | `anaplan_audit.db` | Path to the **DuckDB** database file. Point at a fresh file for v4. |
| `lastRun` | int (Unix seconds) | `0` | Watermark for the next audit fetch. `0` backfills from the start of Anaplan's ~30-day retention. Written back to the loaded settings file after every successful upload (respects `--config`). |
| `auditBatchSize` | int | `1000` | Page size for audit event fetches. |
| `auditRetentionYears` | int | `0` | Purge audit events older than this many years (`0` = keep forever). A timestamped backup precedes each purge. |
| `auditEnabled` | bool | `true` | Gates Steps 2–6. |
| `workspaceModelFilterApproach` | string | `select` | `select` (audit the listed combos) or `skip` (audit all except the listed). |
| `workspaceModelCombos` | list | `[]` | `{workspaceId, modelId}` pairs — **names or IDs**; names resolve against the live tenant at startup. |
| `targetAnaplanModel` | object | (required) | The audit reporting model + its file/import/process object names (Section 4.2). |
| `modelHistory` | object | see Section 7 | The optional Model History pipeline. |

## 4.2 `targetAnaplanModel.objects` — v4 additions

The reporting model's file, import, and process object names live under
`objects`. Defaults match the standard reporting model, so most
deployments only set `processName`. New in v4:

| Key | Default | Description |
|---|---|---|
| `incrementalLoad` | `true` | Upload only audit facts whose key is absent from the model (the delta). Falls back to a full load if the key list can't be resolved. |
| `auditKeyColumn` | `AUDIT_ID` | The transformed-frame column that uniquely keys each fact. |
| `auditKeyListName` | `AUDIT_ID` | The model list whose members are the fact keys already loaded. |
| `eventCategoriesFileName` | `""` | File source for the EVENT_ID category seed (`EVENT_CATEGORIES.csv`). Blank = not uploaded. |
| `syncLists` | `[]` | Transactional-API safety net: adds brand-new codes (e.g. `EVENT_ID`, `AUDIT_ID`) into their lists for models whose imports don't create members themselves. |
| Refresh log | `""` | `batchIdListName` + `refreshLogModuleName` + line-item names record when each run ran and how many rows it loaded (the **delta** count). |

## 4.3 Environment-variable overrides

Any field can be overridden by an environment variable prefixed
`ANAPLAN_AUDIT_`; nested fields use double-underscore separators.

```bash
export ANAPLAN_AUDIT_AUDITENABLED=true
export ANAPLAN_AUDIT_BASIC_USERNAME=user@example.com
export ANAPLAN_AUDIT_BASIC_PASSWORD=secret
export ANAPLAN_AUDIT_MODELHISTORY__ENABLED=true
```

Configuration precedence (highest wins):

```
CLI flag  >  ANAPLAN_AUDIT_* env var  >  .env file  >  settings.json  >  defaults
```

Basic-auth credentials must come from the environment / `.env`
(`ANAPLAN_AUDIT_BASIC_USERNAME` / `ANAPLAN_AUDIT_BASIC_PASSWORD`), never
from `settings.json`.

---

# 5. Exceptions and Exit Codes

## 5.1 Exception hierarchy

```
AnaplanAuditError                     (base, exit 1)
├── ConfigError                       — invalid / missing config (exit 2)
├── AuthError                         — authentication failure (exit 3)
│   ├── BasicAuthError
│   ├── CertAuthError
│   └── OAuthError
│       ├── DeviceRegistrationError
│       └── RefreshTokenError
├── APIError                          — API call failure (exit 4)
│   ├── RateLimitError                — 429 with Retry-After
│   ├── UpstreamError                 — 5xx
│   └── UnexpectedResponseError
├── TransformError                    — DuckDB / SQL failure (exit 5)
│   ├── StorageLoadError              — load step
│   └── QueryExecutionError           — transform query
├── ModelHistoryError                 — caught in orchestrator, never crashes the run (exit 6)
└── RunLockError                      — another instance already running (exit 7)
```

## 5.2 Exit codes

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic failure (catch-all base) |
| 2 | Invalid / missing config |
| 3 | Authentication failure |
| 4 | API call failure after retries |
| 5 | DuckDB / SQL failure |
| 6 | Model History failure (never propagates — audit run still succeeds) |
| 7 | Another instance already running |

Schedulers can branch on the code to alert vs. retry without parsing
log text.

## 5.3 Context dict

Every exception carries a `context: dict[str, str]` populated at the
raise site (`db_path`, `table`, `status_code`, `workspace_id`,
`model_id`, `retry_after`, …). The context appears in the structured
JSON log when the exception propagates.

---

# 6. SQL Transform

## 6.1 Template variables

Two values are injected into `audit_query.sql` at execution time: the
run's batch timestamp (epoch milliseconds, grouping all rows from one
run) and the tenant name from settings.

## 6.2 Tables joined

| Table | Alias | Source |
|---|---|---|
| `events` | `e` | Audit API — core records with flattened `additionalAttributes` columns |
| `users` | `u`, `u2` | SCIM API — display names and usernames |
| `workspaces` | `w` | Integration API — workspace names |
| `models` | `m`, `m2` | Integration API — model names |
| `cloudworks` | `cw` | CloudWorks API — integration names and associated model IDs |
| `act_codes` | `ac` | augmented activity catalog — code → message, parent |
| `actions` | `a` | Integration API — action names |

`objectId` joins are **case-insensitive**: the Integration API returns
model IDs uppercased while events carry them mixed-case, so a
case-sensitive join would silently miss.

## 6.3 Key output columns

The transform produces one flat DataFrame ready for upload. Highlights:

| Column | Description |
|---|---|
| `AUDIT_ID` | Anaplan event ID — the immutable fact key and the incremental-load diff key |
| `BATCH_ID` | Epoch milliseconds at query time — groups all rows from one run |
| `EVENT_DATE`, `CREATED_DATE` | Human-readable UTC timestamps |
| `EVENT_ID` | The event-type code (`USR-8`, `WF-1002`, `INT-52`, …) |
| `EVENT_MESSAGE` | Human-readable message (live-stream value preferred over the shipped catalog) |
| `EVENT_CATEGORY` | The EVENT_ID list parent, derived in SQL from the code prefix via the `event_parent_name` UDF |
| `USER_ID`, `USER_NAME`, `DISPLAY_NAME` | Resolved from the SCIM join |
| `TENANT_ID`, `TENANT_NAME` | `TENANT_NAME` injected from settings |
| `WORKSPACE_ID/NAME`, `MODEL_ID/NAME` | Resolved from `additionalAttributes` and the workspace/model/cloudworks joins |
| `OBJECT_ID/TYPE/NAME`, `ACTION_ID/NAME` | CASE logic across model, CloudWorks, and user joins |
| `UX_APP_ID/NAME`, `UX_PAGE_ID/NAME` | From the `uxAppPage` extractor |
| `ADO_*`, `WORKFLOW_*`, `COMMENT_ID` | From the corresponding extractor categories |
| device/context | `IP_ADDRESS`, `USER_AGENT`, `SESSION_ID`, `HOST_NAME`, `SERVICE_VERSION`, `CHECKSUM` |

## 6.4 Event-category derivation

`EVENT_CATEGORY` is **not** a hard-coded `CASE` in v4. It calls the
`event_parent_name` DuckDB UDF, which is registered from
`anaplan_audit.taxonomy` — the same prefix map that supplies the
`ACTIVITY_CODES` `Parent` column. One source of truth, so the event feed
and the EVENT_ID list can never disagree.

| Prefix | Category |
|---|---|
| `USR-*` | USER ACTIVITY |
| `AUTHZ-*` | ACCESS CONTROL |
| `CONN-*` | SAML CONNECTION |
| `INT-*` | INTEGRATION |
| `FRCST-*` | FORECASTER |
| `PIQ-*` | PLANIQ |
| `WF-*` | WORKFLOW |
| `DSM-*` | ENCRYPTION ACTIVITY |
| `OAUTH-*` | OAUTH |
| `COMMENT-*` | COMMENT |
| any other | UNCATEGORIZED |

Adjusting a name or adding a prefix is a one-line change in
`taxonomy.py` that flows to both the list and the feed.

## 6.5 additionalAttributes staging views

The extractor lifts nested payload fields into named snake_case columns
(`app_id`, `app_name`, `page_id`, …). When a category's `emitLists` is
on, the loader also builds a `(code, name)` staging **view** per
category — `v_ux_app`, `v_ux_page`, `v_action`, `v_process`, `v_role`,
`v_target_user`, `v_cw_integration` — that the model can import as a
list. UX pages are hierarchical (`v_ux_page` carries `parent_code =
app_id`), and `v_ux_app` emits **every** app_id seen in the stream —
named by id when no event supplied a name — so a page's parent app
always exists.

---

# 7. Model History

The Model History pipeline (Step 7) collects per-model change logs,
normalizes the dynamic export CSV into a fixed flat schema, classifies
each change, persists to DuckDB with retention management, and uploads to
a dedicated reporting model. It is gated by `modelHistory.enabled` and
runs after the audit upload on every enabled execution. See the README's
"How Model History works" for the operational narrative.

## 7.1 Tables

Three DuckDB tables back the pipeline (no `FOREIGN KEY`s — see Section
2.3):

| Table | Purpose | Primary key |
|---|---|---|
| `model_registry` | One row per in-scope model, with `last_synced_at`. | `model_id` |
| `model_history_list` | One row per change record — drives the Anaplan list. | `record_id` |
| `model_history_normalized` | Full change detail — 21 columns incl. `user`, `description`, `previous_value`, `new_value`, `module_list`, `line_item_property`, `object`, `target_user`, and the derived `change_type` / `object_type`. | `record_id` |

`record_id` is a deterministic SHA-256 of a change's immutable fields, so
re-exporting an overlapping window never duplicates a row. Migration
columns are applied with idempotent `ALTER TABLE … ADD COLUMN` each run.

## 7.2 Classification

`change_type` (e.g. *Line item created*, *Formula changed*) and
`object_type` (e.g. *Module*, *List*) are derived from the free-text
`description` by a rule engine (`model_history.classification`). The
rules are simple CSVs shipped under `model_history/data/`. Classification
**always** produces a value — an unmatched description falls back to a
generic label and is recorded in `mh_unmatched_descriptions`; surface
them with `anaplan-audit mh-unmatched`.

## 7.3 Failure isolation

`ModelHistoryError` is **always caught** by the orchestrator and logged
as a warning. Exit code 6 is recorded, but the process still exits 0 if
the audit pipeline completed cleanly — a model-history problem never
blocks the audit run.

---

# 8. Activity Event Catalog

The catalog ships at `src/anaplan_audit/data/activity_events.csv` and
feeds the reporting model's `EVENT_ID` list via `ACTIVITY_CODES.csv` on
every run. v4 ships **231 codes** across every category Anaplan publishes
today (User Activity, Access Control, SAML Connection, Encryption
Activity, CloudWorks + ADO Integration, Forecaster/PlanIQ, Workflow task
+ template, Comments, OAuth).

Two design choices make the catalog self-maintaining:

1. **Self-extending.** `augment_activity_catalog` unions the shipped
   catalog with **every code observed in the live stream**, so a code
   Anaplan invented last week arrives parented (via its prefix) the
   moment it appears — no code edit required.
2. **Live-message-first.** Each code's `Event Message` is taken from the
   most frequent non-blank `events.message`, falling back to the shipped
   text only for codes not seen live. Stale "pending Anaplan
   documentation" placeholders self-heal as Anaplan publishes the real
   wording.

When you *do* want to extend the shipped catalog (for codes not yet in
your stream): edit `activity_events.csv`, redeploy, and the next run
reloads the list — no reporting-model rebuild needed.

---

# 9. Incremental Audit-Fact Load

Audit facts are immutable and keyed by `AUDIT_ID`, so a fact already in
the model never needs re-sending. On each run the uploader:

1. Fetches the model's existing `AUDIT_ID` identifiers via the
   Transactional API (paginated to completeness).
2. Filters the transformed frame to rows whose key is **not** already
   present — the delta.
3. Uploads only that delta to `AUDIT_LOG.csv` and records the delta count
   in the Refresh Log (`Audit Records Loaded`).

Properties:

- **Self-correcting.** An empty model list (fresh/rebuilt model) yields a
  delta of *everything* — a full reload automatically.
- **Safe degradation.** If the key column or list can't be resolved, the
  uploader falls back to a full load rather than dropping rows.
- **Requires an additive import.** The `AUDIT_LOG` import must not clear
  the module/list before importing; otherwise history would be lost.
- **Escape hatch.** `run --full` forces a complete reload (after a model
  rebuild or an `additionalAttributes` backfill). `incrementalLoad:
  false` disables it permanently.

Metadata tables (users, models, actions, …) are current-state snapshots,
not append-only facts, so they always load in full.

---

## Document control

- **Maintainer:** Jon Ferneau (OEG Data Integration)
- **Original v1 author credit:** Quin Eddy, Chris Stauffer (Anaplan OEG, 2023)
- **v4 release:** September 2026
- **Repository:** https://github.com/jferneau/anaplan-audit-with-history-v4
