# Anaplan Audit History

[![CI](https://github.com/jferneau/anaplan-audit-with-history-v4/actions/workflows/ci.yml/badge.svg)](https://github.com/jferneau/anaplan-audit-with-history-v4/actions/workflows/ci.yml)

Turn Anaplan's tenant audit log — and, optionally, every model's change
history — into clean, report-ready data inside an Anaplan reporting model.
Run it on a schedule and you get an always-current, in-Anaplan view of
**who did what, when, and where** across your tenant.

---

## Why use it

- **See everything, in Anaplan.** Logins, access changes, actions,
  integrations, UX activity, and full model change history — all landing
  in a model your team can already navigate.
- **Set it and forget it.** Runs unattended on a schedule, retries
  transient API hiccups, and keeps working as Anaplan adds new event types.
- **Easy to deploy.** Ship it as a single executable (no Python required)
  or run it from source in one command.
- **Two independent feeds.** Audit events and Model History can run on
  their own schedules and land in separate reporting models.

---

## Before you start

You'll need:

- The Anaplan **Tenant Auditor** role (to read the audit log) and
  **Workspace Administrator** on your reporting workspace(s).
- Credentials for one auth mode: **OAuth** (recommended), certificate, or
  basic.
- An Anaplan model builder to build the reporting model(s) — a one-time
  setup of a few hours. Ready-to-use sample files are in [`examples/`](examples/).

The tool runs on Windows, macOS, and Linux.

---

## Quick start

### 1. Get the tool

- **Just want to run it?** Download the executable for your platform from
  the [latest release](https://github.com/jferneau/anaplan-audit-with-history-v4/releases/latest)
  and run `./anaplan-audit version` to confirm it works. No Python needed.
- **Running from source?** Clone the repo and run one command:
  - macOS / Linux: `bash setup.sh`
  - Windows: `powershell -ExecutionPolicy Bypass -File setup.ps1`

  It installs everything and offers to launch the setup wizard.

### 2. Configure

Copy [`settings.json.example`](settings.json.example) to `settings.json`
(or run `anaplan-audit init`) and fill in the few tenant-specific values:

- `anaplanTenantName` and your `authenticationMode`
- `workspaceModelCombos` — which model(s) to audit (names or IDs)
- `targetAnaplanModel` — your Audit reporting model
- `modelHistory.targetAnaplanModel` — a **separate** model for change
  history (only if you enable Model History)

For OAuth, run `anaplan-audit register --client-id <ID>` once; every later
run refreshes automatically. Tokens are encrypted at rest.

See **[Settings reference](#settings-reference)** below for what every
setting does and when to change it.

> One `settings.json` covers one tenant. For more tenants, use one file
> each and run with `--config path/to/that.json`.

### 3. Validate, test, go live

```bash
anaplan-audit validate-config          # check settings + credentials
anaplan-audit run --dry-run --verbose  # extract + transform, no upload
anaplan-audit run                       # live run
```

Every run writes a full log to `logs/run_<timestamp>.log` so you can always
see what happened.

### 4. Schedule it

Point cron, a scheduled task, or a CloudWorks job at `anaplan-audit run`.
A typical cadence is audit events every 1–4 hours and Model History nightly.
Overlapping runs are prevented automatically, so you can schedule freely.

---

## Settings reference

Everything lives in one `settings.json`. Most keys have safe defaults — you
only *need* to set your tenant name, auth mode, what to audit, and your
target model(s). Here's what each setting does and when to change it.

### Core

| Setting | What it does |
|---|---|
| `auditEnabled` | Master switch for the audit-events pipeline. At least one of this or `modelHistory.enabled` must be `true`. |
| `anaplanTenantName` | Your tenant's display name. Stamped onto every audit row so a multi-tenant report can tell them apart. |
| `authenticationMode` | `OAuth` (recommended), `cert_auth`, or `basic`. |
| `database` | Path to the local database file the tool builds and reuses. Point at a **fresh** file for v4. |
| `lastRun` | Watermark of the last successful run (managed for you). Leave `0` on first run — the tool only pulls new events after that. |
| `auditBatchSize` | Events fetched per API page (default `1000`). Rarely changed. |
| `auditRetentionYears` | How many years of audit events to keep locally. `0` = keep forever. A backup is taken before any purge. |

### What to audit

| Setting | What it does |
|---|---|
| `workspaceModelFilterApproach` | `select` = audit only the models you list; `skip` = audit everything *except* the ones you list. |
| `workspaceModelCombos` | The list of `{ workspaceId, modelId }` pairs for the mode above. **Names or IDs both work** — names are resolved at runtime, so config survives model rebuilds. |

> Audit **events** are always read tenant-wide, and all workspaces/models are
> loaded so every event resolves to its model. This scope controls which
> models the tool pulls detailed **action/file** metadata for — keep it
> focused to keep runs fast.

### Authentication

| Setting | What it does |
|---|---|
| `oauthClientId` | Your OAuth client ID. Filled in automatically when you run `anaplan-audit register`. |
| `rotatableToken` | Leave `true` for standard OAuth clients (tokens rotate on each refresh). |
| `certPublicPath` / `certPrivatePath` / `certPassphrase` | PEM certificate paths for `cert_auth` mode. |

> Basic-auth credentials go in a local `.env` file (`ANAPLAN_AUDIT_BASIC_USERNAME`
> / `ANAPLAN_AUDIT_BASIC_PASSWORD`), never in `settings.json`.

### Audit reporting model — `targetAnaplanModel`

`workspaceId` / `modelId` point at your Audit reporting model. Inside
`objects`:

| Setting | What it does |
|---|---|
| `processName` | The Anaplan process that imports the uploaded CSVs. Set this for the standard (multi-file) setup. |
| `...FileName` (e.g. `usersFileName`, `auditEventsFileName`) | The **exact names** of the file sources in your model. Defaults match the standard reporting model — override only if yours differ. |
| `eventCategoriesFileName` | File source for the EVENT_ID category seed (`EVENT_CATEGORIES.csv`). Blank by default = not uploaded; set it to re-seed the ~11 parent categories on **every run**, so the ACTIVITY_CODES import always has parents to map codes under. |

<details>
<summary><b>Optional: single-file mode, refresh log, list sync</b></summary>

- **Single-file mode** — instead of `processName`, set `auditFileName` +
  `auditImportName` to upload one blended CSV through a single import.
- **`lastRunFileName` / `lastRunImportName`** — optionally push the last-run
  timestamp into the model for display.
- **Refresh log** (`batchIdListName`, `refreshLogModuleName`,
  `refreshLogTimeStampLineItem`, `refreshLogRecordsLoadedLineItem`) — on each
  successful run, records when it ran and how many rows loaded. Blank any of
  these to turn it off.
- **`syncLists`** — a safety net that adds any brand-new codes (e.g.
  `EVENT_ID`, `AUDIT_ID`) into their lists directly, for models whose imports
  don't create list items on their own. Failures here never fail the run.

</details>

### Model History — `modelHistory`

| Setting | What it does |
|---|---|
| `enabled` | Turns on the per-model change-history pipeline. |
| `targetAnaplanModel` | The **separate** Anaplan model change history loads into (required when enabled — it never shares the audit model, because history grows a model much faster). |
| `exportActionName` | The Anaplan export action that dumps a model's history (default `MODEL_HISTORY_EXPORT`). |
| `anaplanProcess` | The process that loads the history CSVs (default `Load Model History`). |
| `retentionYears` | Years of history to keep (default `2`). |
| `exportTimeoutSeconds`, `maxConcurrentExports`, `backupBeforePurge`, `maxBackupsToKeep` | Performance/safety tuning — defaults are sensible. |

### Anaplan API endpoints & advanced

<details>
<summary><b>URIs and extracted-attribute categories</b></summary>

- **`uris`** — the Anaplan API endpoints. Defaults target the US (`us1a`)
  cloud; change them only if you're on a different or single-tenant cloud.
- **`additionalAttributes`** — pulls extra detail (UX app/page, integration,
  action, process, role, target-user) out of each event into named columns.
  Toggle a `categories` entry `enabled` on/off to keep or drop its columns;
  `emitLists` also builds a staging list for that category (only useful if
  your model imports it). `retainRawJson` keeps a full JSON archive column
  for forward compatibility.

</details>

The shipped [`settings.json.example`](settings.json.example) contains every
key with inline notes — it's the fastest way to see the full shape.

---

## Building the reporting model

The tool uploads a set of CSVs to **named file sources** in your reporting
model and runs a **process** that imports them. Your model builder creates
those file sources, import actions, lists, and the process once.

**Start from the samples.** [`examples/`](examples/) has one CSV per file
source with realistic (non-tenant) data and the exact columns — upload each
one to create its file source and define the import mapping, then the tool
refreshes them on every run.

A few things that make the imports smooth:

- **Use "Match on names or codes"** where your line items are named like the
  columns — new columns then map themselves.
- **Clear "All items"** on the metadata imports so each module reflects your
  current audit scope, not old runs.
- **UX pages nest under apps** — map `parent_code` on the UX_PAGE import, and
  run the UX_APP import before UX_PAGE.
- **The EVENT_ID list is hierarchical** (`All Events → category → event code`).
  Import `EVENT_CATEGORIES.csv` first to create the category tier, then map
  **Parent ← `Parent Code`** on the `ACTIVITY_CODES` import so every code lands
  under its category. See [How the EVENT_ID list stays orphan-free](#how-the-event_id-list-stays-orphan-free).
- **For Model History,** if `change_type` / `object_type` are list-formatted,
  pre-load the `MH_CHANGE_TYPES` / `MH_OBJECT_TYPES` lists so every value
  matches.

<details>
<summary><b>CSV file sources &amp; columns (reference)</b></summary>

**Audit model**

| File source (settings key) | Columns |
|---|---|
| `auditEventsFileName` → `AUDIT_LOG.csv` | blended audit events (60+ cols: dates, user, model, action, category, UX, IDs…) |
| `workspacesFileName` → `WORKSPACE_LIST.csv` | `WS_CT, id, name, active, sizeAllowance, currentSize` |
| `usersFileName` → `USER_LIST.csv` | `USR_CT, id, userName, displayName, firstName, lastName` |
| `modelsFileName` → `MODEL_LIST.csv` | model metadata (`id, name, activeState, lastModified, …`) |
| `actionsFileName` → `ACTION_LIST.csv` | `ACT_CT, id, name, type, workspace_id, model_id, workspace_name, model_name` |
| `filesFileName` → `FILE_LIST.csv` | file metadata + `workspace_id, model_id, workspace_name, model_name` |
| `cloudworksFileName` → `CLOUDWORKS_LIST.csv` | CloudWorks integration metadata |
| `activityCodesFileName` → `ACTIVITY_CODES.csv` | the EVENT_ID list source — `Event Code, Event Message, Associated Object ID, Notes, Parent Code, Parent, Event Name` |
| `eventCategoriesFileName` → `EVENT_CATEGORIES.csv` | the EVENT_ID category tier — `Code, Name, Parent`. Re-seeds the parents each run when configured |

Optional UX/attribution lists (uploaded when their file-name key is set):
`uxAppListFileName`, `uxPageListFileName`, and CloudWorks / Action / Process
/ Role / Target-User lists.

**Model History model**

| File source | Columns |
|---|---|
| `MODEL_REGISTRY.csv` | `model_id, model_name, workspace_id, workspace_name, last_synced_at` |
| `MODEL_HISTORY_LIST.csv` | `record_id, model_id, date_time_utc` |
| `MODEL_HISTORY_NORMALIZED.csv` | flat change detail + `change_type` / `object_type` |

</details>

---

## How Model History works

Model History is a **second, independent pipeline** that captures the
built-in change history Anaplan keeps for each model — every structural and
data change, who made it, and when — and accumulates it into a permanent,
report-ready store. It's separate from the audit-events pipeline: it can be
turned on or off on its own (`modelHistory.enabled`), runs on its own
schedule, and lands in its **own** Anaplan reporting model.

### Why it's separate from the audit log

The tenant audit log tells you *an action happened on a model*. Model
History tells you *what actually changed inside the model* — a new line
item, a formula edit, a list member added, a role change. They answer
different questions, and history grows a model **much** faster than audit
events do, so it lives in its own Anaplan model. That keeps the audit model
lean and lets you size, refresh, and iterate on each independently. This is
why `modelHistory.targetAnaplanModel` is **required** whenever the feature is enabled. NOTE: You can utilize the same Anaplan model for both Audit and Model History if you desire.

### What happens on each run

For every model in scope, the tool:

1. **Triggers the export.** It finds the export action named by
   `exportActionName` (default `MODEL_HISTORY_EXPORT`) in that model and
   fires it through the Anaplan Integration API. A model with no such export
   action is silently skipped — not every model needs history captured.
2. **Waits for completion.** It polls the export task every ~10 seconds
   until it reports `COMPLETE`, up to `exportTimeoutSeconds` (default 600 =
   10 minutes). A `FAILED`, `CANCELLED`, or timed-out export is logged as a
   warning and skipped — it never crashes the run.
3. **Downloads and normalizes the CSV.** Anaplan history exports have a
   *dynamic* column layout — the columns present depend on what changed in
   the window — so the tool maps those shifting headers onto a **fixed,
   predictable schema** (date, user, description, previous/new value,
   module/list, line item/property, object, and more). Ragged rows are
   padded, extra columns tolerated.
4. **Assigns a stable record ID.** Each change gets a deterministic ID
   (a SHA-256 hash of its immutable fields). That ID is the dedup key, so
   re-exporting an overlapping window never creates duplicate rows — the
   same change always lands on the same row.
5. **Classifies the change.** From the free-text `description`, two
   controlled-vocabulary columns are derived — `change_type` (e.g. *Line
   item created*, *Formula changed*) and `object_type` (e.g. *Module*,
   *List*) — so the report can pivot and filter cleanly instead of scanning
   free text. Classification is rule-based and **always** produces a value —
   a change no rule matches falls back to a generic label rather than a
   blank, and `anaplan-audit mh-unmatched` lists any descriptions worth a
   new rule.
6. **Stores, uploads, backs up, and purges** (details below).

Models are exported **in parallel** — up to `maxConcurrentExports` (default
5) at a time — but all database writes happen serially afterward on one
thread, by design, to avoid write contention. Raise the concurrency for
tenants with many models; lower it if you hit API rate limits.

### Retention, backups, and long-term storage

This is the part worth understanding well, because it's what makes the tool
a *history* system rather than a 30-day window.

- **Anaplan only exposes a limited history window; the tool keeps far
  more.** Every change the tool has ever downloaded is retained locally and
  re-uploaded, so your Anaplan report shows history reaching back well
  beyond what a single live export would return.
- **Retention window.** Model History keeps `retentionYears` of data
  (default **2 years**). On each run, records older than the cutoff
  (`today − retentionYears`) are deleted from the local store. Set it higher
  to keep more; there's no hard cap.
- **Backup before every purge.** When `backupBeforePurge` is `true` (the
  default), the tool writes a full timestamped copy of the local database —
  `<database>_backup_YYYYMMDD_HHMMSS.duckdb` — *before* it deletes anything. If a
  purge ever removed something it shouldn't, the prior state is recoverable
  from that backup.
- **Rolling backup window.** Only the most recent `maxBackupsToKeep`
  backups (default **7**) are kept; older ones are removed automatically so
  backups don't grow without bound. Seven daily backups ≈ a one-week
  recovery window on a nightly schedule.
- **Going beyond the retention window.** The local store is a working set,
  not a system of record for *unlimited* history. If you need history older
  than `retentionYears`, export it to an external SQL database or data
  warehouse before the cutoff passes. Raising `retentionYears` also works,
  at the cost of a larger local database and Anaplan model.

### What lands in Anaplan

Three CSVs load into the Model History model via the `anaplanProcess`
(default `Load Model History`):

| File source | What it is |
|---|---|
| `MODEL_REGISTRY.csv` | One row per model captured — `model_id, model_name, workspace_id, workspace_name, last_synced_at`. Your index of *which* models have history. |
| `MODEL_HISTORY_LIST.csv` | One row per change record (`record_id, model_id, date_time_utc`) — the numbered list every change item hangs off. |
| `MODEL_HISTORY_NORMALIZED.csv` | The full change detail — user, description, previous/new value, module/list, object, plus the derived `change_type` / `object_type`. |

### Failure isolation

Model History **never crashes the audit run**. A missing export action, a
failed or slow export, an upload hiccup — each is caught, logged as a
warning, and the run continues. A non-fatal model-history problem surfaces
as exit code `6` so a scheduler can distinguish it from a healthy run
without treating it as a hard failure.

---

## Additional attributes explained

Every Anaplan audit event carries an `additionalAttributes` payload — a
small bag of extra context specific to that event type. A UX event names the
app and page; an integration event names the CloudWorks integration; an
action event names the action and its type; and so on. The raw payload is
nested and event-type-specific, which makes it awkward to report on
directly. The **additional-attributes extractor** lifts those nested values
into **named, flat columns** your reporting model can pivot on.

### How it works

1. **Flatten.** As events are transformed, the nested `additionalAttributes`
   object is flattened — `additionalAttributes.appId`, `.appName`, etc.
2. **Extract into named columns.** Known fields are projected into stable
   snake_case columns (`app_id`, `app_name`, `page_id`, …). Every event gets
   every column — populated where the field exists for that event type,
   blank where it doesn't — so downstream imports see one uniform shape.
3. **Archive the raw payload.** When `retainRawJson` is `true` (default),
   the complete original payload is also stored as a JSON string in
   `additional_attributes_raw`. If Anaplan adds a new attribute the tool
   doesn't yet break out, it's still captured — nothing is lost.

### The six categories

Extraction is grouped into categories. Each can be toggled on or off
independently under `additionalAttributes.categories`, and each owns a
fixed set of columns:

| Category | Columns it fills | Comes from events like |
|---|---|---|
| `uxAppPage` | `app_id`, `app_name`, `page_id`, `page_name` | UX app/page views and activity |
| `cwIntegration` | `integration_id`, `integration_name`, `integration_flow_id` | CloudWorks integration runs |
| `action` | `action_id`, `action_name`, `action_type` | Import/export/other action runs |
| `process` | `process_id`, `process_name` | Process runs |
| `role` | `role_id`, `role_name` | Role / permission changes |
| `targetUser` | `target_user_id`, `target_user_name` | Admin events acting *on another user* |

> `targetUser` is the one that's easy to miss and valuable to call out: on an
> admin event, the `USER` fields are *who did it*, and `target_user_*` is
> *who it was done to* (e.g. an admin deactivating another user).

### Enable, disable, and lists

- **`enabled` per category** — turn a category on to populate its columns,
  off to force them blank. Disabling a category you don't report on keeps
  the data tidy and the CSVs narrower.
- **`emitLists`** — in addition to filling the columns, build a small
  staging list of the distinct values for that category (e.g. the set of
  UX apps seen). Only useful if your model actually imports that list;
  leave it off otherwise. UX pages nest under their app (a page carries its
  `parent_code`), so if you import both, run the app import before the page
  import.
- **`retainRawJson`** — keep (or drop) the full JSON archive column. Keeping
  it is the forward-compatibility hedge; dropping it trims width if you're
  certain you'll never need attributes beyond the named columns.

---

## How the EVENT_ID list stays orphan-free

Every audit event is one of Anaplan's event-type codes — `USR-8`, `AUTHZ-11`,
`WF-112`, `DSM-071`. In the reporting model these become a **hierarchical
list**, `EVENT_ID`: `All Events → category → event code`. The value of that
list is the grouping — "show me all Access Control events", "everything under
Workflow". A code with no parent breaks that grouping: it dangles at the root,
uncategorised.

The tool guarantees every code is parented, by two design choices:

1. **The category is derived from the code's prefix**, not looked up from a
   catalog. `USR-*` → `USER ACTIVITY`, `AUTHZ-*` → `ACCESS CONTROL`, `WF-*` →
   `WORKFLOW`, and so on, with an `UNCATEGORIZED` catch-all for any prefix the
   map hasn't seen. So a code Anaplan invented last week — one no catalog lists
   yet — still resolves to a parent the moment it appears.
2. **`ACTIVITY_CODES.csv` is the single writer of the list.** It ships as the
   documented catalog **plus** every code actually observed in your audit
   stream, each row carrying a `Parent Code` / `Parent`. Because one import
   owns the list and it always supplies a parent, nothing can be created
   orphaned. The audit fact import only *references* `EVENT_ID` — it never
   creates members.

To set it up: create an `EVENT_CATEGORIES.csv` file source and set
`eventCategoriesFileName` so the tool re-seeds the ~11 categories under
`All Events` on **every run** (belt-and-suspenders — the category tier can
never go missing). Then point the `ACTIVITY_CODES` import at `EVENT_ID` with **Name ← `Event
Name`**, **Parent ← `Parent Code`, matched on code**, and add both imports to
your process. On the next run this also re-parents any codes that were
previously orphaned. The category shows up on each event row too, as
`EVENT_CATEGORY`, so the Audit module can filter by it directly.

> **Why `Event Name`, not `Event Message`?** Anaplan caps list-item names at 60
> characters and requires them unique, so the tool computes `Event Name` — the
> message when it fits and is unique, otherwise the code (e.g. `CONN-4`,
> `USR-81`). Map the list item's **Name** to `Event Name`.
>
> **Keep the full description:** add a text-formatted list property to
> `EVENT_ID` (e.g. `Event Description`) and map it **← `Event Message`**,
> matched on `Event Code`. Properties have no 60-char limit, so every item
> keeps its complete description even when its name falls back to the code.

> **On the webinar:** *"Anaplan keeps inventing new event codes. Instead of
> chasing a master list, the tool reads the code's prefix and files it under
> the right category automatically — so a code we've never seen before still
> lands in the right place, and nothing ever dangles uncategorised."*

The category vocabulary lives in one place — `anaplan_audit/taxonomy.py` — so
adjusting a name or adding a prefix is a one-line change that flows to both the
list and the event feed.

---

## Commands

| Command | What it does |
|---|---|
| `anaplan-audit init` | Interactive setup wizard |
| `anaplan-audit run` | The full pipeline (extract → transform → upload) |
| `anaplan-audit register --client-id <ID>` | One-time OAuth setup |
| `anaplan-audit validate-config` | Check settings and credentials |
| `anaplan-audit mh-unmatched` | List model-history descriptions no rule matched |
| `anaplan-audit version` | Version + self-check |

<details>
<summary><b>Useful <code>run</code> flags</b></summary>

- `--config PATH` — settings file (default `./settings.json`)
- `--dry-run` — extract + transform, skip upload
- `--verbose` — detailed console output
- `--since EPOCH` / `--limit N` — bounded / sample runs
- `--log-dir PATH` / `--no-log-file` — control the per-run log file

</details>

---

## Good to know

- **Data retention.** Anaplan's audit API exposes ~30 days, but the tool
  keeps every event it has ever seen. Audit events are kept indefinitely by
  default; Model History defaults to 2 years. Both are configurable, and a
  backup is taken before any purge.
- **Keeps up with Anaplan.** New event types and attributes flow through
  automatically; updating the bundled activity-code list as Anaplan
  publishes new codes is the only routine maintenance.
- **Model History classification.** Each change is tagged with a
  `change_type` and `object_type` for easy reporting; the rules are simple
  CSVs you can edit, and `anaplan-audit mh-unmatched` flags anything new.
- **Exit codes.** `0` success · `2` config · `3` auth · `4` API · `5` data
  · `6` model-history (non-fatal) · `7` already running — so schedulers can
  alert vs. retry.

---

## Upgrading from an earlier version

v4 starts from a fresh database (its storage format changed and isn't
migrated — this is nearly free, since Anaplan only retains ~30 days and
Model History re-exports). Install v4, re-register OAuth if used, point
`database` at a new file, then `validate-config` → `run --dry-run` →
schedule.

---

## Support & license

You're expected to maintain your own deployment. Questions or issues: open
a GitHub issue on this repository.

Licensed under the MIT License — see [LICENSE](LICENSE).
