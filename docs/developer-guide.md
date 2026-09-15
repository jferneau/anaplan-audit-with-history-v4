---
title: "Anaplan Audit History v4 — Developer Guide"
author: "Jon Ferneau, Operational Excellence Group (OEG)"
date: "September 2026"
---

# 1. Development Setup

## 1.1 Prerequisites

| Item | Version |
|---|---|
| Operating system | Linux, macOS, or Windows |
| Python | 3.13 |
| `uv` | Latest |
| `git` | 2.40+ |

## 1.2 Setting up the environment

```bash
git clone https://github.com/jferneau/anaplan-audit-with-history-v4.git
cd anaplan-audit-with-history-v4
uv sync                          # runtime + dev deps
uv run pytest                    # ~464 tests passing
uv run mypy src/                 # no issues found
uv run ruff check src/ tests/    # all checks passed
```

Or use the Makefile: `make check` runs lint, type-check, and tests
together.

## 1.3 IDE configuration

VS Code: install the Python and Ruff extensions; the interpreter
auto-discovers the `.venv/` created by `uv sync`. `pyproject.toml` ships
the ruff and mypy configuration, so editors pick them up automatically.

---

# 2. Testing

## 2.1 Running tests

```bash
uv run pytest                                    # all tests
uv run pytest tests/test_config.py               # one file
uv run pytest -k "incremental"                   # by name pattern
uv run pytest -x --tb=short                       # stop on first failure, compact
uv run pytest --cov=src/anaplan_audit --cov-report=term-missing
```

## 2.2 Test layout

~40 files, ~464 tests. Representative coverage:

| Test file | What it covers |
|---|---|
| `test_config.py` | Config loading, precedence chain, validators |
| `test_auth_basic.py` / `test_auth_cert.py` / `test_auth_oauth.py` | The three auth flows |
| `test_api_client_retry.py` | tenacity retry, `RateLimitError` / `Retry-After` |
| `test_cli.py` | Typer command parsing, exit codes, version |
| `test_transform.py` | DuckDB load, event upsert, dedup, `audit_query.sql` round-trip |
| `test_catalog.py` | Activity-catalog augmentation, live-message preference, message-first `Event Name` |
| `test_taxonomy.py` | Prefix → category map and the SQL UDF projection |
| `test_incremental_load.py` | Delta filtering, `--full` bypass, safe fallbacks, list-item pagination |
| `test_additional_attributes_schema_views.py` | Extractor columns, staging views, `v_ux_app` parent-always-present |
| `test_run_lock.py` | Run-lock acquisition and exit-code-7 conflict (runs on the Windows CI job too) |
| `test_model_history_*.py` | Export trigger/poll/download, normalize schema, streaming CSV, classification |
| `test_v329_multi_file_upload.py` / `test_v3218_name_collision_skip.py` | Regression suites named by the release that introduced them |

## 2.3 Conventions

- **Mock all HTTP with `respx`** — never make live Anaplan calls in tests.
  Note `respx.mock`'s param matching is a **subset** match, so page-1 and
  page-2 routes for a paginated endpoint collide; sequence responses with
  `side_effect=[...]` on one route instead.
- **Build clients with `make_client()` / `make_token()`** from
  `tests/conftest.py`.
- **DuckDB tests use `tmp_path`** — never a shared on-disk database; the
  `seed_tables()` helper loads fixture frames.
- **Model History transform tests construct CSV strings directly** (tab-
  delimited, matching real exports) rather than file fixtures.
- **Watch for retryable statuses.** The client retries 429/5xx with
  backoff, so a test that wants an immediate error should return a
  non-retryable status (e.g. 404), not 500 — otherwise it hangs on retries.

## 2.4 Coverage

CI enforces the full suite plus `mypy --strict` and `ruff` (lint +
format) on both Ubuntu and Windows. Coverage is tracked via
`--cov=src/anaplan_audit`; keep new code paths covered — the interactive
CLI wizard and `logging_config.py` are the intentionally-thin areas.

---

# 3. Linting and Type Checking

```bash
uv run ruff check src/ tests/        # lint
uv run ruff check --fix src/ tests/  # auto-fix
uv run ruff format src/ tests/       # format (CI runs --check)
uv run mypy src/                     # strict mode
```

`N815` is ignored because the Anaplan API returns camelCase JSON keys
surfaced verbatim on Pydantic models. Mypy strict is enforced; annotation
errors block CI. **`ruff format --check` runs in CI** — run
`ruff format` before pushing or the Ubuntu job fails.

---

# 4. Building and Distribution

Two artifacts:

**Wheel / sdist** (for `pip`/`uv` installs and the CI smoke test):

```bash
make build          # rm -rf dist/ && uv build
# dist/anaplan_audit_history-4.0.0.dev0-py3-none-any.whl + .tar.gz
```

**Single executable** (the primary customer artifact — no Python
required), via PyInstaller using the spec in `packaging/`:

```bash
uv run pyinstaller packaging/anaplan-audit.spec
# dist/anaplan-audit  (or anaplan-audit.exe on Windows)
./dist/anaplan-audit version   # probes the bundled DuckDB engine + package data
```

`anaplan-audit version` deliberately opens an in-memory DuckDB and reads
the bundled `activity_events.csv` / `audit_query.sql` / model-history
rule CSVs — a broken bundle fails there rather than mid-run. GitHub
releases attach the executables (and the wheel/sdist) as assets.

Version lives in two places — keep them in sync: `pyproject.toml` and
`src/anaplan_audit/__init__.py`.

---

# 5. Extending the Activity-Code Catalog

The catalog ships at `src/anaplan_audit/data/activity_events.csv`,
bundled via `importlib.resources`. **v4 is largely self-maintaining:**
`transform.catalog.augment_activity_catalog` unions in every code seen in
the live stream (parented by prefix via `taxonomy.py`) and prefers the
live `events.message`, so new codes and updated wording flow through
without an edit.

You only edit the CSV to **pre-load codes not yet in your stream**:

1. Reconcile against Anaplan's published event-code tables; add rows for
   new codes with their messages. Keep legacy codes (e.g. `PIQ-*`
   superseded by `FRCST-*`) so historical events still resolve.
2. `uv run pytest tests/test_catalog.py tests/test_taxonomy.py` to
   confirm the catalog still parses and every `Event Name` stays ≤ 60
   chars and unique.
3. Bump the version, tag, release.

## 5.1 A new event *family* (new prefix)

Add the prefix → `(parent_code, parent_name)` entry to
`_PREFIX_TO_CATEGORY` (and `CATEGORIES`) in `taxonomy.py`. That single
change flows to both the `EVENT_ID` list parent and the SQL
`EVENT_CATEGORY` column via the registered UDFs — no `CASE` to edit.

## 5.2 A new `additionalAttributes` key

The DuckDB loader adds any new dotted column automatically, so the
pipeline never breaks. To *surface* the key:

1. Add it to `_KNOWN_OPTIONAL_EVENT_COLUMNS` in `transform/loader.py` so
   the column exists on first write, before data lands.
2. Add a SELECT alias in `transform/queries/audit_query.sql`.
3. If it should populate a staging list, extend
   `transform/additional_attributes.py` (extractor columns + `_STAGING_VIEWS`).
4. Add a test and document the new column in the Model Setup Guide.

---

# 6. Contributing

## 6.1 Branching and commits

`main` is always release-ready. Feature work happens on short-lived
branches, merged via squash PR once CI is green. Conventional-commit
prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`) are encouraged.

## 6.2 PR checklist

- [ ] `uv run pytest` green (all ~464).
- [ ] `uv run mypy src/` clean.
- [ ] `uv run ruff check src/ tests/` clean and `ruff format` produces no changes.
- [ ] New event code → updated `activity_events.csv` + a test.
- [ ] New `additionalAttributes` column → updated loader + `audit_query.sql` + a test.
- [ ] Docs touched? Regenerate the `.docx` (Section 6.3).
- [ ] Version bumped? Update **both** `pyproject.toml` and `src/anaplan_audit/__init__.py`.

## 6.3 Regenerating the customer docs

The four customer-facing docs live as Markdown in `docs/` and are
published as `.docx` (both are versioned in the repo). After editing any
`docs/*.md`:

```bash
make docs    # pandoc regenerates docs/*.docx from the .md sources
```

## 6.4 Release process

1. Bump the version in `pyproject.toml` and `src/anaplan_audit/__init__.py`.
2. Tag `vX.Y.Z` and push.
3. `make build`, then build the PyInstaller executables per platform.
4. `gh release create vX.Y.Z … dist/*` with release notes.

---

## Document control

- **Maintainer:** Jon Ferneau (OEG Data Integration)
- **Original v1 author credit:** Quin Eddy, Chris Stauffer (Anaplan OEG, 2023)
- **v4 release:** September 2026
- **Repository:** https://github.com/jferneau/anaplan-audit-with-history-v4
