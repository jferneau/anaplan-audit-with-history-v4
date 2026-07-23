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
| `activityCodesFileName` → `ACTIVITY_CODES.csv` | the activity-code catalog |

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
