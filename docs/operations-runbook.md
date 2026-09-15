---
title: "Anaplan Audit History v4 — Operations Runbook"
author: "Jon Ferneau, Operational Excellence Group (OEG)"
date: "September 2026"
---

# 1. Installation

There are two ways to run v4. Most operators want the first.

## 1.1 Option A — the single executable (no Python)

v4 ships as a **PyInstaller** executable per platform. Download it from
the [latest release](https://github.com/jferneau/anaplan-audit-with-history-v4/releases/latest),
put it on the machine that will run the schedule, and confirm it works:

```bash
./anaplan-audit version
# anaplan-audit-history 4.0.0.dev0
```

No Python, `uv`, or dependencies are required. Everywhere below that a
command reads `uv run anaplan-audit …`, the executable equivalent is just
`./anaplan-audit …`.

## 1.2 Option B — run from source

| Item | Requirement |
|---|---|
| Operating system | Linux, macOS, or Windows. The run lock uses `fcntl.flock` on POSIX and `msvcrt.locking` on Windows. |
| Python | 3.13+ |
| `uv` | Latest — install from <https://docs.astral.sh/uv/> |
| Disk | ~200 MB for code + dependencies. The DuckDB working set depends on tenant size; budget 100 MB–10 GB. |
| Memory | 512 MB minimum; peak ~1 GB during large exports. |
| Network | Outbound HTTPS to `auth.anaplan.com`, `api.anaplan.com`, `audit.anaplan.com`, `api.cloudworks.anaplan.com`. |

**Recommended — one-command setup.** From the project folder:

```bash
bash setup.sh                                          # macOS / Linux
powershell -ExecutionPolicy Bypass -File setup.ps1     # Windows
```

The script installs `uv`, Python 3.13, and all dependencies, then offers
to launch the config wizard. It is idempotent and needs no pre-installed
Python or `uv`. To do it manually instead: `uv sync`, then
`cp settings.json.example settings.json`.

Then configure, validate, and dry-run:

```bash
uv run anaplan-audit init             # interactive config wizard
uv run anaplan-audit validate-config  # confirm settings + auth
uv run anaplan-audit run --dry-run --limit 500 --verbose
```

## 1.3 Verify the release

```bash
uv run anaplan-audit version
# anaplan-audit-history 4.0.0.dev0
# … also prints Python + dependency versions and probes the bundled
#   DuckDB engine and package-data files (a broken bundle fails here).
```

The bundled catalog should report **231** codes:

```bash
uv run python -c "
import importlib.resources as r
csv = r.files('anaplan_audit.data').joinpath('activity_events.csv').read_text()
print(f'{len(csv.splitlines()) - 1} event codes loaded')
"
# 231 event codes loaded
```

---

# 2. Authentication

## 2.1 Choosing a mode

| Mode | When to use |
|---|---|
| `basic` | Quick local testing, ad-hoc backfills. Credentials live in environment variables — never in `settings.json`. |
| `cert_auth` | Automated / service-account runs where you control the cert lifecycle. |
| `OAuth` | **Recommended for production.** Register once, then unattended. |

## 2.2 Basic

```bash
export ANAPLAN_AUDIT_BASIC_USERNAME="user@example.com"
export ANAPLAN_AUDIT_BASIC_PASSWORD="secret"
```

Set `authenticationMode: "basic"` in `settings.json`. These credentials
must never go in `settings.json`.

## 2.3 Certificate

1. Obtain a PEM public certificate and matching private key.
2. Store them outside the project directory.
3. In `settings.json`:

   ```json
   "authenticationMode": "cert_auth",
   "certPublicPath":  "/path/to/public.pem",
   "certPrivatePath": "/path/to/private.pem"
   ```

   If the key is passphrase-protected, set `certPassphrase`. On Windows,
   always use the dedicated `certPassphrase` field (the legacy inline
   `path:passphrase` form still parses safely around drive letters).

## 2.4 OAuth device grant (recommended)

1. Get your OAuth `client_id` from your Anaplan administrator.
2. Register (needs browser access once):

   ```bash
   uv run anaplan-audit register --client-id <YOUR_CLIENT_ID>
   ```

3. Open the printed URL, log in, and approve the device.
4. The refresh token is encrypted with Fernet (AES-128-CBC +
   HMAC-SHA256) and stored under the user's home; the keyfile has `0600`
   permissions.
5. `register` writes the client ID into `settings.json` as
   `oauthClientId`. Set `authenticationMode: "OAuth"` and every later run
   refreshes automatically.

---

# 3. Scheduling

Point cron, launchd, Task Scheduler, or a CloudWorks job at
`anaplan-audit run`. Use the executable path (`/opt/anaplan-audit/anaplan-audit run`)
or the source path (`uv run anaplan-audit run`) — the examples below show
the source form; swap in the executable if you took Option A.

## 3.1 Linux — cron

```cron
# Audit pipeline every hour
0 * * * * cd /opt/anaplan-audit && ./anaplan-audit run >> /var/log/anaplan-audit.log 2>&1

# Model History nightly at 02:30 (separate settings file)
30 2 * * * cd /opt/anaplan-audit && ./anaplan-audit run --config settings-mh.json >> /var/log/anaplan-audit-mh.log 2>&1
```

## 3.2 macOS — launchd

Point `ProgramArguments` at the executable (or `uv run anaplan-audit run`),
set `WorkingDirectory` to the folder holding `settings.json`, and a
`StartCalendarInterval`. Load with `launchctl load …`.

## 3.3 Windows — Task Scheduler

Create a Basic Task; set the action's Program/script to the executable
(or `uv.exe` with arguments `run anaplan-audit run`); set **Start in** to
the folder holding `settings.json`; enable "Run task as soon as possible
after a scheduled start is missed." The run lock works on Windows via
`msvcrt.locking`, so overlapping starts exit cleanly with code 7.

## 3.4 Cadence

| Pipeline | Recommended cadence |
|---|---|
| Audit only | Every 1–4 hours |
| Audit + Model History | Audit hourly; Model History nightly via a separate settings file |
| Model History only | Nightly or weekly |

The OS-level run lock makes overlapping invocations safe — a second
process exits cleanly with code 7 instead of contending for the database.

**One settings file per tenant.** Each run targets one tenant; give each
tenant its own settings file (`--config`) and its own `database` path (the
lock is keyed to the database file).

**Large tenants — `exportTimeoutSeconds`.** The Model History export
timeout (default 600s) is per model. A very large model can exceed it;
the export is skipped and logged, and the audit run is unaffected. Raise
`modelHistory.exportTimeoutSeconds` if you see `model_history_export_timeout`.

---

# 4. Monitoring and Logging

## 4.1 Exit codes

| Code | Action |
|---|---|
| 0 | Success |
| 1 | Unhandled — inspect logs |
| 2 | Config — fix `settings.json`, re-run |
| 3 | Auth — check credentials / re-register OAuth |
| 4 | API failure after retries — retry later |
| 5 | DuckDB / SQL failure — inspect the transform and the events table |
| 6 | Model History failure — never crashes the run; logged as a warning |
| 7 | Another instance is running — wait, or delete a stale `.lock` file |

## 4.2 Logs

Every run writes a full JSON log to `logs/run_<timestamp>.log` (change
with `--log-dir`, disable with `--no-log-file`), and also emits to the
console — rich formatting with `--verbose`. Each line is one structured
event:

```json
{"event": "pipeline_step_done", "step": "fetch_audit_events",
 "record_count": 73, "duration_ms": 358, "level": "info",
 "timestamp": "2026-09-14T21:42:59Z"}
```

## 4.3 Key events to watch

| Event | Meaning |
|---|---|
| `pipeline_step_start` / `pipeline_step_done` | Boundaries of each pipeline step |
| `audit_events_fetched` | Count of **new** events fetched this run (since `lastRun`) |
| `table_loaded` | Per-table row counts after the DuckDB load |
| `activity_catalog_augmented` | Catalog rebuilt; `messages_from_stream` shows how many codes took a live message |
| `audit_load_incremental` | The delta computation — `observed`, `already_loaded`, `delta` |
| `refresh_log_written` | Refresh log updated with the delta count |
| `list_sync_added` / `list_sync_already_current` | Safety-net list sync outcome |
| `pipeline_complete` | Run finished successfully |
| `model_history_*` (warning) | A single model export/upload issue — other models continue; audit run unaffected |

---

# 5. Troubleshooting

## 5.1 "Records loaded looks the same every run"

This is expected and correct — it's the **two different counts**:

- `audit_events_fetched` is incremental — only events new since
  `lastRun`. Back-to-back runs fetch ~0.
- The Refresh Log's `Audit Records Loaded` is the **delta uploaded** this
  run (`audit_load_incremental`'s `delta`). On an immediate re-run it is
  ~0.

If instead you're reading a metadata table's row count (users, models),
those are current-state snapshots and always load in full — stable by
design. If `audit_events_fetched` itself stays large on an immediate
re-run, the watermark isn't advancing — check `lastRun` in `settings.json`
between runs and look for `last_run_persist_failed`.

## 5.2 Import completed but reported failure

`run` polls the Anaplan task and **raises when the Anaplan-side result is
unsuccessful** (exit 4). On `import_failed_in_anaplan` with
`failure_dump_available: true`, open the import action in the model and
download the failure dump — usual causes are column-mapping drift or list
items missing from the target model.

## 5.3 Zero records — is this a failure?

No. No activity since `lastRun` is a legitimate outcome; the transform and
upload skip cleanly. Investigate only if zero counts persist across many
consecutive runs (likely a config/auth issue, not Anaplan inactivity).

## 5.4 `RunLockError` (exit 7)

Another instance holds the lock. Either a run is genuinely in progress
(wait), or a previous run crashed without releasing it — verify no process
is active, then delete `anaplan_audit.lock` next to the database file.

## 5.5 OAuth refresh fails

Confirm the refresh token hasn't been revoked, then re-register
(`anaplan-audit register --client-id <ID>`). On
`cryptography.fernet.InvalidToken`, the keyfile/token store was moved or
corrupted — delete the `~/.anaplan_audit/` directory and re-register.

## 5.6 Slow runs / rate limits

Reduce `auditBatchSize` (1000 → 500) and
`modelHistory.maxConcurrentExports` (5 → 2–3). Confirm retries are
honoring `Retry-After` — search logs for `RateLimitError`.

## 5.7 New Anaplan attribute appears

No action needed. The DuckDB loader reconciles the events-table schema
before each write and adds any new `additionalAttributes.*` column
automatically, so the transform never errors on an attribute Anaplan just
introduced. To surface it in the report, add a matching line item — see
the Model Setup Guide.

---

# 6. Catalog Maintenance

The catalog ships in `src/anaplan_audit/data/activity_events.csv` and
feeds the `EVENT_ID` list on every run. **v4 is largely self-maintaining:**

- **New codes** seen in the live stream are unioned into the list
  automatically, parented by prefix — no edit required.
- **Messages** come from the live stream first, so wording stays current
  and "pending documentation" placeholders self-heal.

You only need to edit `activity_events.csv` to pre-load codes you haven't
seen yet. After editing: redeploy (`git pull && uv sync`, or ship a new
executable); the next run reloads the list. No reporting-model rebuild.

---

# 7. Long-Term Data Retention

The DuckDB file is a local working set — no replication or HA. Defaults:
audit events kept indefinitely (`auditRetentionYears: 0`); Model History
kept `retentionYears: 2`. A timestamped backup
(`<database>_backup_YYYYMMDD_HHMMSS.duckdb`) is written before every purge,
and only the most recent `maxBackupsToKeep` (default 7) are kept.

For compliance or multi-year trend needs beyond the retention window,
extract `model_history_normalized` / `model_history_list` to an external
SQL database or warehouse before the cutoff passes (use `captured_at` as
an incremental watermark). Prefer that over raising `retentionYears`
without bound — a very large local database and Anaplan model is the cost
of the alternative.

---

## Document control

- **Maintainer:** Jon Ferneau (OEG Data Integration)
- **Original v1 author credit:** Quin Eddy, Chris Stauffer (Anaplan OEG, 2023)
- **v4 release:** September 2026
- **Repository:** https://github.com/jferneau/anaplan-audit-with-history-v4
