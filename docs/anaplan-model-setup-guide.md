---
title: "Anaplan Audit History v4 — Model Setup Guide"
author: "Jon Ferneau, Operational Excellence Group (OEG)"
date: "September 2026"
---

# 1. Introduction

## 1.1 What you will build

This guide walks you through building the Anaplan reporting model(s) that
receive data from the `anaplan-audit` CLI:

- The **Audit Reporting Model** — receives the transformed audit events
  (one row per event, with workspace/model/user resolved to readable
  names and each event-type code parented under its category).
- The **Model History Reporting Model** (optional) — receives the
  normalized per-model change history when `modelHistory.enabled = true`.
  You may reuse the same Anaplan model for both if you prefer.

## 1.2 The fastest path: start from the samples

The repo ships one ready-to-use CSV per file source in
[`examples/`](../examples/), each with realistic (non-tenant) data and
the **exact columns** the tool produces. The intended workflow:

1. In your model, **upload each `examples/*.csv`** to create its file
   source (Data → Imports → the file becomes a named source).
2. Define the **import action** for each, mapping to your list/module.
3. Add the imports to a **process**.
4. Point `settings.json` at those object names; the tool then refreshes
   the same file sources on every run.

Because the columns match, prefer **"Match on names or codes"** on your
imports — new columns then map themselves as Anaplan evolves.

## 1.3 How data flows

```
┌────────────────────────────┐      ┌────────────────────────────┐
│  Audit Reporting Model     │      │ Model History Reporting    │
│  - EVENT_ID list (nested)  │      │ - Model Registry list      │
│  - AUDIT_ID list           │      │ - Model History List       │
│  - Audit Detail module     │◀─────│ - Detail module            │
│  - file sources + process  │      │ - file sources + process   │
└────────────────────────────┘      └────────────────────────────┘
        ▲                                  ▲
        │ Anaplan Integration + Transactional APIs
        │                                  │
┌───────┴──────────────────────────────────┴────────────────────┐
│   anaplan-audit CLI (Python 3.13 / single executable, v4)       │
└─────────────────────────────────────────────────────────────────┘
```

---

# 2. Audit Reporting Model

## 2.1 File sources

The tool uploads these CSVs to **named file sources** and runs a process
that imports them. Defaults match the standard model — override the
`...FileName` keys in `settings.json` only if yours differ.

| File source (settings key) | Loads into | Notes |
|---|---|---|
| `auditEventsFileName` → `AUDIT_LOG.csv` | Audit Detail module | The blended audit rows (60+ columns). Loads the **delta** by default. |
| `usersFileName` → `USER_LIST.csv` | Users list/module | Current-state snapshot |
| `workspacesFileName` → `WORKSPACE_LIST.csv` | Workspaces | |
| `modelsFileName` → `MODEL_LIST.csv` | Models | |
| `actionsFileName` → `ACTION_LIST.csv` | Actions | |
| `filesFileName` → `FILE_LIST.csv` | Files | |
| `cloudworksFileName` → `CLOUDWORKS_LIST.csv` | CloudWorks integrations | |
| `activityCodesFileName` → `ACTIVITY_CODES.csv` | `EVENT_ID` list | Columns: `Event Code, Event Message, Associated Object ID, Notes, Parent Code, Parent, Event Name` |
| `eventCategoriesFileName` → `EVENT_CATEGORIES.csv` | `EVENT_ID` category tier | Columns: `Code, Name, Parent`. Re-seeds the ~11 parents each run when set |

Optional staging lists (uploaded when their file-name key is set):
`uxAppListFileName`, `uxPageListFileName`, and CloudWorks / Action /
Process / Role / Target-User lists.

## 2.2 The `EVENT_ID` list (hierarchical)

In v4 the event-type codes live in a **hierarchical** list,
`EVENT_ID`: `All Events → category → event code`. The value is the
grouping — "all Access Control events", "everything under Workflow".

Build it in two imports:

1. **`EVENT_CATEGORIES.csv` → the category tier.** Map `Code`, `Name`,
   and `Parent` (= `All Events`). Set `eventCategoriesFileName` so the
   tool re-seeds the ~11 categories every run (belt-and-suspenders — the
   tier can never go missing).
2. **`ACTIVITY_CODES.csv` → the event codes.** On the `EVENT_ID` import:
   - **Name ← `Event Name`**
   - **Parent ← `Parent Code`**, **matched on code**
   - Code ← `Event Code`

Add both to your process, category import first. Every code is then
parented — including codes brand-new to your stream, because the tool
derives the parent from the code's prefix.

> **Why `Event Name`, not `Event Message`?** Anaplan caps list-item names
> at 60 characters and requires them unique. The tool computes a
> message-first `Event Name`: the message verbatim when it fits; trimmed
> to a word boundary when too long; message + bracketed code when two
> codes share a message; and just the code when the only "message" is a
> "pending Anaplan documentation" placeholder. To keep the full text,
> add a text-formatted list **property** (e.g. `Event Description`) mapped
> **← `Event Message`**, matched on `Event Code` — properties have no
> 60-char cap.

v4 ships **231 codes** across User Activity, Access Control, SAML
Connection, Encryption Activity, CloudWorks + ADO Integration,
Forecaster/PlanIQ, Workflow (task + template), Comments, and OAuth — and
extends itself from your live stream.

## 2.3 The `AUDIT_ID` list and Audit Detail module

`AUDIT_ID` (one member per audit event, keyed by the event's ID) is the
row dimension of the **Audit Detail** module. The `AUDIT_LOG.csv` import
populates the module's line items from the transform's columns. Because
columns match, map with **"Match on names or codes"**. Key columns to
surface:

| Line item | Format | Source column |
|---|---|---|
| event date / created date | Date | `EVENT_DATE`, `CREATED_DATE` |
| event_id | List (`EVENT_ID`) | `EVENT_ID` |
| event_message | Text | `EVENT_MESSAGE` |
| event_category | List/Text | `EVENT_CATEGORY` (always populated) |
| user_name / display_name | Text | `USER_NAME`, `DISPLAY_NAME` |
| workspace / model | Text | `WORKSPACE_NAME`, `MODEL_NAME` |
| object / action | Text | `OBJECT_NAME`, `ACTION_NAME` |
| success | Boolean | `SUCCESS` |
| UX / ADO / Workflow / Comment | Text | `UX_APP_ID/NAME`, `UX_PAGE_ID/NAME`, `ADO_*`, `WORKFLOW_*`, `COMMENT_ID` |
| device context | Text | `IP_ADDRESS`, `USER_AGENT`, `SESSION_ID`, `HOST_NAME`, `CHECKSUM` |

`EVENT_CATEGORY` is populated for every event, so it makes an excellent
top-level dashboard filter. The `UX_*` / `ADO_*` / `WORKFLOW_*` /
`COMMENT_ID` columns are populated only on the relevant event types.

> **Incremental load — keep the `AUDIT_LOG` import additive.** By default
> the tool uploads only new audit facts (the delta). For that to work, the
> `AUDIT_LOG` import (and the process) must **not** clear the Audit Detail
> module or the `AUDIT_ID` list before importing. If you deliberately want
> a full reload each run, set `incrementalLoad: false` or run `--full`.

## 2.4 Optional: staging lists, refresh log, list sync

- **UX apps/pages.** If you import them, map `parent_code` on the
  `UX_PAGE` import (pages nest under apps) and run the **UX_APP import
  before UX_PAGE**. The tool guarantees every page's parent app exists.
- **Refresh log.** Set `batchIdListName`, `refreshLogModuleName`, and the
  timestamp/records line-item names to record when each run ran and how
  many rows it loaded (the delta count) — no file/import needed.
- **List sync.** `syncLists` is a Transactional-API safety net that adds
  brand-new `EVENT_ID` / `AUDIT_ID` codes directly, for models whose
  imports don't create members themselves. Failures never fail the run.

## 2.5 Process and `settings.json`

Create a process (default name **`Update Anaplan Audit Environment`**)
that runs the imports. Then in `settings.json`:

```json
"targetAnaplanModel": {
  "workspaceId": "<reporting workspace id>",
  "modelId": "<audit reporting model id>",
  "objects": {
    "processName": "Update Anaplan Audit Environment",
    "eventCategoriesFileName": "EVENT_CATEGORIES.csv",
    "incrementalLoad": true
  }
}
```

The `...FileName` keys default to the standard names above, so most
deployments only set `processName` (and `eventCategoriesFileName` if you
seed the category tier).

---

# 3. Model History Reporting Model (optional)

Build this only if you will enable `modelHistory.enabled = true`.

## 3.1 Lists

| List | Code | Display | Properties |
|---|---|---|---|
| `Model Registry` | `model_id` | `model_name` | `workspace_id`, `workspace_name`, `last_synced_at` |
| `Model History List` | `record_id` (SHA-256) | `record_id` | `model_id`, `date_time_utc` |

## 3.2 Module: Model History Detail

Dimensioned by `Model History List`. Map from
`MODEL_HISTORY_NORMALIZED.csv`. Notable line items (v4):

| Line item | Source column |
|---|---|
| date_time_utc | `date_time_utc` |
| user | `user` |
| description | `description` |
| previous_value / new_value | `previous_value`, `new_value` |
| module_list / line_item_property / object | `module_list`, `line_item_property`, `object` |
| target_user | `target_user` (who a role change was done *to*) |
| **change_type** | `change_type` (derived, e.g. *Formula changed*) |
| **object_type** | `object_type` (derived, e.g. *Module*, *List*) |
| captured_at | `captured_at` |

`change_type` and `object_type` are controlled-vocabulary columns the
tool derives from the free-text description — make them List-formatted to
pivot and filter cleanly.

## 3.3 File sources, imports, process

| File source | Target | 
|---|---|
| `MODEL_REGISTRY.csv` | `Model Registry` list |
| `MODEL_HISTORY_LIST.csv` | `Model History List` list |
| `MODEL_HISTORY_NORMALIZED.csv` | `Model History Detail` module |

Create a process whose name matches `modelHistory.anaplanProcess`
(default **`Load Model History`**) running registry → list → detail.
Because each change carries a stable `record_id`, re-exports never
duplicate rows regardless of your import mode.

---

# 4. Validation

1. **Dry-run.** `anaplan-audit run --dry-run --verbose` — confirm row
   counts before any upload.
2. **Validate config.** `anaplan-audit validate-config` should report
   success.
3. **First live run.** Drop `--dry-run`; confirm the process runs and the
   imports land the expected rows.
4. **Schedule.** See the Operations Runbook, Section 3.

## 4.1 EVENT_ID list verification

After the first run, your `EVENT_ID` list should show the ~11 categories
under `All Events` with codes nested beneath them, each named by its
message. Verify the bundled catalog is complete (**231**):

```bash
anaplan-audit run --dry-run   # then, from source:
uv run python -c "import importlib.resources as r; print(len(r.files('anaplan_audit.data').joinpath('activity_events.csv').read_text().splitlines()) - 1)"
# 231
```

## 4.2 Delta-load verification

Run twice back-to-back. The Refresh Log's `Audit Records Loaded` should
show the full/near-full set on the first run and ~0 on the second — that
is the incremental delta working. If you ever need a full reload (after
rebuilding the model), run `anaplan-audit run --full`.

---

## Document control

- **Maintainer:** Jon Ferneau (OEG Data Integration)
- **Original v1 author credit:** Quin Eddy, Chris Stauffer (Anaplan OEG, 2023)
- **v4 release:** September 2026
- **Repository:** https://github.com/jferneau/anaplan-audit-with-history-v4
