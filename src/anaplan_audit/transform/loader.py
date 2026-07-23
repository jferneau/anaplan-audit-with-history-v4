"""Load JSON / DataFrame data into DuckDB tables.

v4: the storage engine is DuckDB (was SQLite in v1-v3).  The public
surface and semantics are unchanged — same tables, same columns, same
upsert/dedup behavior — but the I/O plumbing differs where DuckDB has
no SQLite-compatible shortcut:

* No journal/synchronous PRAGMAs — DuckDB manages its own WAL, and
  enforces declared constraints without opt-in pragmas.
* ``SET TimeZone = 'UTC'`` on every connection.  DuckDB's session
  timezone defaults to the *local machine's* timezone, and timestamp
  formatting silently renders in session time — without this, audit
  timestamps would shift by the host's UTC offset (Phase 0 finding).
* ``df.to_sql`` → ``register()`` + ``CREATE OR REPLACE TABLE … AS SELECT``.
* ``INSERT OR REPLACE / OR IGNORE`` → standard ``ON CONFLICT`` clauses.
* Schema versioning lives in a ``_schema_meta`` table (SQLite's
  ``PRAGMA user_version`` has no DuckDB equivalent).
* The model-history tables no longer declare FOREIGN KEYs: DuckDB
  executes UPDATEs as DELETE+INSERT, so re-upserting a parent row that
  has children raises a spurious FK violation (documented DuckDB
  limitation).  Referential integrity is maintained by insert order in
  :func:`upsert_model_history`, exactly as before — the FKs were
  belt-and-suspenders.
"""

from __future__ import annotations

import json
import math
import shutil
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import structlog

from anaplan_audit.exceptions import StorageLoadError
from anaplan_audit.transform.additional_attributes import ADDITIONAL_ATTRIBUTES_COLUMNS

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

# The audit events table name as referenced by audit_query.sql.
_EVENTS_TABLE = "events"

# Bumped whenever the events-table schema gains columns. Stored in the
# _schema_meta table so operators (and future migration branches) can
# tell what shape a given DB has.
_EVENTS_SCHEMA_VERSION = 2

# Backup file glob pattern, e.g. anaplan_audit_backup_20260412_143000.duckdb
_BACKUP_GLOB = "*_backup_*"

# Events-table columns with a non-VARCHAR type. Everything else —
# including every dynamically-discovered additionalAttributes.* column —
# is VARCHAR. DuckDB enforces column types (SQLite's dynamic typing
# tolerated anything), so the type policy must be explicit: the Pydantic
# AuditEvent model guarantees these four fields' types, and all other
# fields are strings or null at the API boundary.
_EVENT_TYPED_COLUMNS: dict[str, str] = {
    "eventDate": "BIGINT",
    "createdDate": "BIGINT",
    "index": "BIGINT",
    "success": "BOOLEAN",
}

# Dotted additionalAttributes columns that audit_query.sql references.
# pd.json_normalize only creates a dotted column when at least one event in
# the batch carries that nested key — so a batch of, say, only login events
# (whose additionalAttributes is null) would omit them and the SELECT would
# fail with "no such column". Pre-creating them when the events table is
# written guarantees the join always resolves, regardless of the batch mix.
_KNOWN_OPTIONAL_EVENT_COLUMNS: list[str] = [
    # Core attributes (present on most user-activity / access events).
    "additionalAttributes.workspaceId",
    "additionalAttributes.modelId",
    "additionalAttributes.actionId",
    "additionalAttributes.name",
    "additionalAttributes.type",
    "additionalAttributes.auth_id",
    "additionalAttributes.modelRoleName",
    "additionalAttributes.modelRoleId",
    "additionalAttributes.objectTypeId",
    "additionalAttributes.roleId",
    "additionalAttributes.roleName",
    "additionalAttributes.objectTenantId",
    "additionalAttributes.objectId",
    "additionalAttributes.active",
    # Newer event categories: UX pages, ADO, Workflow templates, Comments.
    "additionalAttributes.appId",
    "additionalAttributes.pageId",
    "additionalAttributes.pageName",
    "additionalAttributes.pipelineId",
    "additionalAttributes.dataspaceId",
    "additionalAttributes.scheduleId",
    "additionalAttributes.connectionId",
    "additionalAttributes.taskId",
    "additionalAttributes.workflowTemplateId",
    "additionalAttributes.commentId",
]


def _connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with the session timezone pinned to UTC.

    DuckDB's session ``TimeZone`` defaults to the host machine's local
    timezone, and ``strftime`` over a TIMESTAMPTZ renders in session
    time — so without this, every timestamp the pipeline formats would
    silently shift by the host's UTC offset (and differently on every
    customer's machine).  Every connection in this module goes through
    here so the setting can never drift between call sites.
    """
    conn = duckdb.connect(str(db_path))
    conn.execute("SET TimeZone = 'UTC'")
    return conn


def _table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    """Return ``True`` if *table* exists in the connected database."""
    row = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()
    return row is not None


def _is_null(value: object) -> bool:
    """True for None and float NaN (pandas' missing-value marker)."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _sanitize_for_storage(df: pd.DataFrame) -> pd.DataFrame:
    """Convert any column that contains dicts or lists to JSON strings.

    API models use ``extra="allow"``, so Anaplan can return nested fields
    (e.g. SCIM ``groups``, ``emails``) that survive into the DataFrame as
    Python objects.  Serialising them to JSON strings preserves the data
    without crashing the load.

    Only columns that actually contain complex values are touched; all-scalar
    columns are left unchanged.
    """
    df = df.copy()
    for col in df.columns:
        if df[col].apply(lambda v: isinstance(v, (dict, list))).any():
            df[col] = df[col].apply(lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v)
            logger.debug("column_serialised_to_json", column=col)
    return df


def load_to_duckdb(db_path: Path, datasets: dict[str, pd.DataFrame]) -> None:
    """Load DataFrames into DuckDB tables.

    Metadata tables are replaced on each run.  The ``events`` table uses
    upsert semantics to preserve historical data beyond Anaplan's 30-day
    retention window — this is a key v1 feature preserved through v4.

    Args:
        db_path: Path to the DuckDB database file.
        datasets: Mapping of table name to DataFrame.

    Raises:
        StorageLoadError: If any load operation fails.
    """
    current_table = "<unknown>"
    try:
        with closing(_connect(db_path)) as conn:
            for table_name, df in datasets.items():
                current_table = table_name
                if table_name == _EVENTS_TABLE:
                    _upsert_events(conn, df)
                else:
                    # A DataFrame with no columns cannot create a table.
                    # Callers should supply columns even for empty results;
                    # guard here so a stray column-less frame degrades to a
                    # skip with a clear warning rather than a cryptic crash.
                    if df.shape[1] == 0:
                        logger.warning(
                            "table_skipped_no_columns",
                            table=table_name,
                            note="empty result with no columns; table not created",
                        )
                        continue
                    df = _sanitize_for_storage(df)
                    # Declare an explicit type per column rather than let
                    # ``CREATE TABLE AS SELECT`` infer it. DuckDB types an
                    # empty or all-NULL column as INTEGER, so a metadata frame
                    # that is 0-row (e.g. a tenant with no CloudWorks
                    # integrations) or that omits a column entirely would land
                    # as INTEGER and break audit_query.sql's ``upper()`` / text
                    # joins ("No function matches upper(INTEGER)"). Same policy
                    # the events table already applies via _event_column_type.
                    col_defs = ", ".join(
                        f'"{c}" {_metadata_column_type(df[c])}' for c in df.columns
                    )
                    conn.register("_load_df", df)
                    conn.execute(f'CREATE OR REPLACE TABLE "{table_name}" ({col_defs})')
                    conn.execute(f'INSERT INTO "{table_name}" SELECT * FROM _load_df')
                    conn.unregister("_load_df")
                logger.info(
                    "table_loaded",
                    table=table_name,
                    row_count=len(df),
                )
            # Refresh the export view after every load so its column
            # list stays in sync with the current models / users schema
            # (spec Milestone 3). Idempotent, and creates cleanly even
            # if either underlying table is empty on this run.
            _ensure_models_export_view(conn)
    except StorageLoadError:
        raise
    except Exception as exc:
        raise StorageLoadError(
            f"Failed to load table '{current_table}' into DuckDB: {type(exc).__name__}: {exc}",
            context={"db_path": str(db_path), "table": current_table},
        ) from exc


def _event_column_type(column: str) -> str:
    """Return the declared DuckDB type for an events-table column."""
    return _EVENT_TYPED_COLUMNS.get(column, "VARCHAR")


def _metadata_column_type(series: pd.Series) -> str:
    """Return the declared DuckDB type for a metadata-table column.

    Metadata frames carry mostly string IDs/names (the API models are all
    string-coerced), so an empty or all-NULL column carries no type signal
    and DuckDB's inference defaults it to INTEGER — which then breaks the
    ``upper()`` and text-equality joins in audit_query.sql. Default such
    columns to VARCHAR and only keep a native type when the data actually
    proves one, mirroring the events table's explicit-type policy.
    """
    if series.isna().all():
        return "VARCHAR"
    kind = series.dtype.kind
    if kind == "b":
        return "BOOLEAN"
    if kind in ("i", "u"):
        return "BIGINT"
    if kind == "f":
        return "DOUBLE"
    if kind == "M":
        return "TIMESTAMP"
    # Object, string, and anything unexpected -> text.
    return "VARCHAR"


def _norm_event_value(value: object, column: str) -> object:
    """Normalise a DataFrame cell for binding into the typed events table.

    SQLite's dynamic typing accepted anything; DuckDB enforces the
    declared column type.  This reproduces the SQLite storage affinity
    the v3 output depended on:

    * ``None`` / ``NaN`` → SQL NULL.
    * BIGINT / BOOLEAN targets → native int / bool.
    * VARCHAR targets: strings pass through; booleans become ``"1"`` /
      ``"0"`` (SQLite stored them as integers, so that's what the v3
      CSVs carried); other scalars are stringified (``5`` → ``"5"``,
      ``5.0`` → ``"5.0"`` — matching SQLite's INTEGER/REAL round-trip).
    """
    if _is_null(value):
        return None
    target = _event_column_type(column)
    if target == "BIGINT":
        return int(value)  # type: ignore[call-overload]
    if target == "BOOLEAN":
        return bool(value)
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(int(value))
    return str(value)


def _upsert_events(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> None:
    """Upsert audit events to preserve historical data.

    Set-based upsert: the normalized batch DataFrame is registered as a
    DuckDB view and merged in a single ``INSERT … SELECT … ON CONFLICT``
    statement.  Per-row ``executemany`` (the v3 SQLite pattern) is
    catastrophically slow under DuckDB — benchmarked at 50k rows it
    exhausted >12 GB and OOM'd, because each row runs the full conflict
    machinery individually.  One vectorized statement is the engine's
    native fast path.

    In-batch duplicate ids are collapsed keeping the *last* occurrence
    before the merge — v3's sequential executemany had implicit
    last-wins semantics, and DuckDB's ``DO UPDATE`` raises if one
    statement touches the same target row twice.

    Schema evolution: ``pd.json_normalize`` produces a column per dotted key
    seen in the current batch. As Anaplan adds new event types (UX, ADO,
    Workflow templates, Comments, Forecaster), new ``additionalAttributes.*``
    columns appear in later runs. Any column present in the incoming
    DataFrame but missing from the existing table is added with
    ``ALTER TABLE ADD COLUMN IF NOT EXISTS`` before the merge runs.

    Args:
        conn: An open DuckDB connection.
        df: DataFrame of audit events with columns matching the v1 API schema.
    """
    if df.empty:
        return

    # Create the table with an explicit type per column if it doesn't
    # exist yet. DuckDB enforces types, so the policy must be declared
    # rather than inferred from whatever the first batch happened to hold
    # (a column that is all-NaN in batch 1 but strings in batch 2 would
    # otherwise be created as DOUBLE and reject the strings).
    if not _table_exists(conn, _EVENTS_TABLE):
        col_defs = ", ".join(f'"{c}" {_event_column_type(c)}' for c in df.columns)
        conn.execute(f"CREATE TABLE {_EVENTS_TABLE} ({col_defs})")

    # Add any columns that appear in this batch but not in the existing table,
    # plus well-known optional additionalAttributes columns referenced by
    # audit_query.sql (so the query never fails on a tenant that hasn't yet
    # produced UX, ADO, Workflow, or Comment events), plus the extracted
    # named columns owned by the additionalAttributes module (v3.3.0 schema
    # version 2) so backfill and view creation always have the shape they
    # expect regardless of what any given nightly batch happened to contain.
    _ensure_event_columns(
        conn,
        pd.Index(
            list(df.columns) + _KNOWN_OPTIONAL_EVENT_COLUMNS + ADDITIONAL_ATTRIBUTES_COLUMNS,
        ),
    )
    _set_schema_version(conn, _EVENTS_SCHEMA_VERSION)

    # Ensure a unique index on id for ON CONFLICT to work.
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{_EVENTS_TABLE}_id ON {_EVENTS_TABLE}(id)")

    columns = list(df.columns)

    # Normalize values Python-side so the registered frame carries exactly
    # the SQLite-affinity representations the v3 output depended on (bools
    # in VARCHAR columns as "1"/"0", NaN as NULL, scalars stringified) —
    # letting DuckDB implicit-cast instead would render booleans as
    # "true"/"false" and silently change the CSVs.
    norm_df = pd.DataFrame(
        {c: [_norm_event_value(v, c) for v in df[c]] for c in columns},
    )
    # Last-wins in-batch dedup (see docstring).
    norm_df = norm_df.drop_duplicates(subset=["id"], keep="last")

    # Quote every column name — required for dotted names like
    # "additionalAttributes.workspaceId".
    quoted = [f'"{c}"' for c in columns]
    col_names = ", ".join(quoted)
    update_clause = ", ".join(f'"{c}" = excluded."{c}"' for c in columns if c != "id")

    conn.register("_events_batch", norm_df)
    try:
        conn.execute(
            f"INSERT INTO {_EVENTS_TABLE} ({col_names}) "
            f"SELECT {col_names} FROM _events_batch "
            f"ON CONFLICT (id) DO UPDATE SET {update_clause}"
        )
    finally:
        conn.unregister("_events_batch")


def _set_schema_version(conn: duckdb.DuckDBPyConnection, version: int) -> None:
    """Record the events-table schema version in the ``_schema_meta`` table.

    Replaces SQLite's ``PRAGMA user_version``, which has no DuckDB
    equivalent.  A one-row-per-key table is more introspectable anyway.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS _schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    conn.execute(
        "INSERT INTO _schema_meta VALUES ('events_schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [str(version)],
    )


# SQL for the export view that resolves ``lastModifiedByUserGuid`` to
# the user's email and display name (spec Section 4). The column list
# is stable per spec Section 3.1 — new columns Anaplan adds land on the
# ``models`` table via CREATE OR REPLACE and can be added here in a
# follow-up if the reporting model needs them.
_MODELS_EXPORT_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS v_models_export AS
SELECT
    m.id,
    m.name,
    m.activeState,
    m.currentWorkspaceId,
    m.currentWorkspaceName,
    m.modelUrl,
    m.isoCreationDate,
    m.lastSavedSerialNumber,
    m.lastModifiedByUserGuid,
    u.userName    AS lastModifiedByEmail,
    u.displayName AS lastModifiedByDisplayName,
    m.memoryUsage,
    m.lastModified
FROM models m
LEFT JOIN users u ON m.lastModifiedByUserGuid = u.id
"""


def _ensure_models_export_view(conn: duckdb.DuckDBPyConnection) -> None:
    """(Re)create ``v_models_export`` so it matches the current schema.

    ``DROP VIEW IF EXISTS`` before ``CREATE`` because a schema change on
    either underlying table would otherwise leave a stale view definition
    around — ``CREATE VIEW IF NOT EXISTS`` is a no-op when a definition
    already exists, even if it references removed columns. Drop-then-create
    is idempotent and cheap.

    LEFT JOIN (not INNER) — spec Section 4: a model with an unknown
    ``lastModifiedByUserGuid`` (e.g. a deactivated user, a service
    account not in SCIM) must still export, with null email columns.

    Skipped when either underlying table is absent (partial loads —
    e.g. an events-only batch).  SQLite silently allowed creating a
    dangling view over missing tables; DuckDB validates references at
    CREATE time and would fail the whole load.
    """
    conn.execute("DROP VIEW IF EXISTS v_models_export")
    if not (_table_exists(conn, "models") and _table_exists(conn, "users")):
        logger.debug("models_export_view_skipped_missing_tables")
        return
    conn.execute(_MODELS_EXPORT_VIEW_SQL)
    logger.debug("models_export_view_ensured")


def _ensure_event_columns(
    conn: duckdb.DuckDBPyConnection,
    df_columns: pd.Index,
) -> None:
    """Add any DataFrame columns missing from the events table.

    The events table is first created from whatever columns appear in the
    initial batch. Later batches may carry new ``additionalAttributes.*``
    keys (added by Anaplan as new event categories ship — UX, ADO, Workflow
    templates, etc.). Without this migration, ``executemany`` would fail
    on the missing column.

    DuckDB supports ``ADD COLUMN IF NOT EXISTS`` natively, so no
    duplicate-column error handling is needed (v3 had to pattern-match
    SQLite's error text). The ``PRAGMA table_info`` check remains only so
    genuinely-new columns get logged.

    Args:
        conn: An open DuckDB connection.
        df_columns: Columns of the DataFrame being inserted this batch.
    """
    rows = conn.execute(f"PRAGMA table_info({_EVENTS_TABLE})").fetchall()
    existing = {row[1] for row in rows}
    for col in df_columns:
        if col in existing:
            continue
        col_type = _event_column_type(col)
        conn.execute(f'ALTER TABLE {_EVENTS_TABLE} ADD COLUMN IF NOT EXISTS "{col}" {col_type}')
        logger.info("events_schema_column_added", column=col)


# ---------------------------------------------------------------------------
# Staging views for additionalAttributes list sources (spec Milestone 3)
# ---------------------------------------------------------------------------


# One view per category, each producing DISTINCT (code, name) pairs from
# the events table. The reporting model's list imports read these as their
# source (spec Section 6.3). Non-null / non-empty filters guarantee spec
# Acceptance criterion #6 — no orphan rows land in Anaplan lists.
# category → (view name, id column, name column, parent id column | None).
# The optional 4th element makes the list HIERARCHICAL: the view emits an extra
# ``parent_code`` column so the reporting model can nest the list under its
# parent. Only UX pages use it — a page nests under its app — every other list
# is flat. When a parent is set, rows missing the parent id are also filtered
# out, so no child item lands without a parent (Anaplan rejects a parentless
# item in a child list).
_STAGING_VIEWS: dict[str, tuple[str, str, str, str | None]] = {
    "uxAppPage_app": ("v_ux_app", "app_id", "app_name", None),
    "uxAppPage_page": ("v_ux_page", "page_id", "page_name", "app_id"),
    "cwIntegration": ("v_cw_integration", "integration_id", "integration_name", None),
    "action": ("v_action", "action_id", "action_name", None),
    "process": ("v_process", "process_id", "process_name", None),
    "role": ("v_role", "role_id", "role_name", None),
    "targetUser": ("v_target_user", "target_user_id", "target_user_name", None),
}

# Category → the staging-view keys it owns (some categories, like
# uxAppPage, produce more than one view because they carry two logically
# distinct lists).
_CATEGORY_TO_VIEWS: dict[str, list[str]] = {
    "uxAppPage": ["uxAppPage_app", "uxAppPage_page"],
    "cwIntegration": ["cwIntegration"],
    "action": ["action"],
    "process": ["process"],
    "role": ["role"],
    "targetUser": ["targetUser"],
}


def ensure_staging_views(
    db_path: Path,
    *,
    view_categories: set[str] | None = None,
) -> None:
    """Create or refresh the additionalAttributes staging views.

    Views feed the Anaplan list imports described in spec Section 6.3.
    Every view emits distinct ``(code, name)`` pairs, filtered so no
    row has a null or empty code or name — Anaplan lists reject empty
    codes and would blow up the property-based imports downstream.

    Args:
        db_path: Path to the DuckDB database file.
        view_categories: When set, only views owned by these categories
            (per :data:`_CATEGORY_TO_VIEWS`) are created; the rest are
            dropped if they exist so a category being switched off in
            settings.json cleanly removes its stale view. ``None`` (the
            default) creates every view.
    """
    if view_categories is None:
        wanted_view_keys = set(_STAGING_VIEWS.keys())
    else:
        wanted_view_keys = set()
        for cat in view_categories:
            wanted_view_keys.update(_CATEGORY_TO_VIEWS.get(cat, []))

    with closing(_connect(db_path)) as conn:
        # Bail out cleanly if events doesn't exist yet — first-run
        # scenario before any batches have landed.
        if not _table_exists(conn, _EVENTS_TABLE):
            logger.debug("staging_views_skipped_no_events_table")
            return

        for view_key, (view_name, id_col, name_col, parent_col) in _STAGING_VIEWS.items():
            if view_key not in wanted_view_keys:
                # Drop unwanted views so a config toggle cleans up
                # after itself; DROP VIEW IF EXISTS is a no-op when the
                # view was never created.
                conn.execute(f"DROP VIEW IF EXISTS {view_name}")
                continue

            # Hierarchical lists (only UX pages today) emit an extra
            # parent_code column and drop rows with no parent id.
            parent_select = f', "{parent_col}" AS parent_code' if parent_col else ""
            parent_where = (
                f' AND "{parent_col}" IS NOT NULL AND "{parent_col}" != \'\'' if parent_col else ""
            )
            # CREATE OR REPLACE (not IF NOT EXISTS) so a definition change —
            # e.g. adding the hierarchical parent_code column — propagates to
            # an existing database on the next run. View creation is cheap.
            conn.execute(
                f"CREATE OR REPLACE VIEW {view_name} AS "
                f'SELECT DISTINCT "{id_col}" AS code, "{name_col}" AS name{parent_select} '
                f"FROM {_EVENTS_TABLE} "
                f'WHERE "{id_col}" IS NOT NULL AND "{id_col}" != \'\' '
                f'  AND "{name_col}" IS NOT NULL AND "{name_col}" != \'\'{parent_where}'
            )

    logger.info(
        "staging_views_ensured",
        wanted=sorted(wanted_view_keys),
        total_defined=len(_STAGING_VIEWS),
    )


# ---------------------------------------------------------------------------
# Model History tables
# ---------------------------------------------------------------------------


# v4 note: no FOREIGN KEY declarations (v3 had them, enforced via
# SQLite's foreign_keys pragma). DuckDB executes UPDATEs as
# DELETE+INSERT, so re-upserting a model_registry row that already has
# model_history rows referencing it would raise a spurious FK violation
# on every re-run (documented DuckDB limitation). Insert order in
# upsert_model_history() preserves the actual integrity invariant.
_MODEL_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS model_registry (
    model_id        VARCHAR PRIMARY KEY,
    model_name      VARCHAR NOT NULL,
    workspace_id    VARCHAR NOT NULL,
    workspace_name  VARCHAR NOT NULL,
    last_synced_at  VARCHAR NOT NULL
)
"""

_MODEL_HISTORY_LIST_DDL = """
CREATE TABLE IF NOT EXISTS model_history_list (
    record_id       VARCHAR PRIMARY KEY,
    model_id        VARCHAR NOT NULL,
    date_time_utc   VARCHAR NOT NULL
)
"""

_MODEL_HISTORY_NORMALIZED_DDL = """
CREATE TABLE IF NOT EXISTS model_history_normalized (
    record_id           VARCHAR PRIMARY KEY,
    anaplan_record_id   VARCHAR,
    model_id            VARCHAR NOT NULL,
    date_time_utc       VARCHAR NOT NULL,
    "user"              VARCHAR,
    description         VARCHAR,
    security_change     VARCHAR,
    previous_value      VARCHAR,
    new_value           VARCHAR,
    module_list         VARCHAR,
    line_item_property  VARCHAR,
    customer            VARCHAR,
    "export"            VARCHAR,
    import_action       VARCHAR,
    data_types          VARCHAR,
    table_name          VARCHAR,
    "object"            VARCHAR,
    target_user         VARCHAR,
    captured_at         VARCHAR NOT NULL,
    change_type         VARCHAR,
    object_type         VARCHAR
)
"""

# Columns added after initial release.  Applied via ALTER TABLE ADD COLUMN
# IF NOT EXISTS on existing databases each run — safe to call repeatedly.
_NORMALIZED_MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("import_action", "VARCHAR"),
    ("data_types", "VARCHAR"),
    ("table_name", "VARCHAR"),
    ("anaplan_record_id", "VARCHAR"),
    # v3.4.0 — role-change events attribute the affected user via a
    # "Target User" column in the Anaplan export CSV. Previously the
    # normalizer logged this as unmapped and dropped it.
    ("target_user", "VARCHAR"),
    # v3.8.0 — derived controlled-vocabulary classification columns.
    ("change_type", "VARCHAR"),
    ("object_type", "VARCHAR"),
]

# Failsafe report (v3.8): descriptions that no change-type rule matched, so
# colleagues have a durable working set for authoring rules. Occurrences are
# the most-recent run's count (model history re-exports full history each run,
# so replacing beats accumulating an ever-inflating total).
_MH_UNMATCHED_DDL = """
CREATE TABLE IF NOT EXISTS mh_unmatched_descriptions (
    description     VARCHAR PRIMARY KEY,
    occurrences     BIGINT NOT NULL,
    first_seen_at   VARCHAR NOT NULL,
    last_seen_at    VARCHAR NOT NULL
)
"""

# Indexes on the columns most commonly filtered/joined in analytics queries.
_MODEL_HISTORY_INDEXES: list[str] = [
    # model_history_normalized
    "CREATE INDEX IF NOT EXISTS idx_mhn_model_id ON model_history_normalized(model_id)",
    "CREATE INDEX IF NOT EXISTS idx_mhn_date ON model_history_normalized(date_time_utc)",
    'CREATE INDEX IF NOT EXISTS idx_mhn_user ON model_history_normalized("user")',
    "CREATE INDEX IF NOT EXISTS idx_mhn_captured_at ON model_history_normalized(captured_at)",
    # model_history_list
    "CREATE INDEX IF NOT EXISTS idx_mhl_model_id ON model_history_list(model_id)",
]


def ensure_model_history_tables(db_path: Path) -> None:
    """Create the three model history tables and their indexes if absent.

    This is idempotent and safe to call on every run.  The indexes on
    ``model_id``, ``date_time_utc``, ``user``, and ``captured_at`` prevent
    full-table scans on the normalized table which can grow to millions of
    rows across a large tenant.

    Args:
        db_path: Path to the DuckDB database file.
    """
    with closing(_connect(db_path)) as conn:
        conn.execute(_MODEL_REGISTRY_DDL)
        conn.execute(_MODEL_HISTORY_LIST_DDL)
        conn.execute(_MODEL_HISTORY_NORMALIZED_DDL)
        conn.execute(_MH_UNMATCHED_DDL)
        for idx_sql in _MODEL_HISTORY_INDEXES:
            conn.execute(idx_sql)
        # Migrate existing databases: add any columns that were introduced
        # after the table was first created. ADD COLUMN IF NOT EXISTS is a
        # native no-op when the column already exists (v3 had to swallow
        # SQLite's duplicate-column OperationalError instead).
        for col_name, col_type in _NORMALIZED_MIGRATION_COLUMNS:
            conn.execute(
                f"ALTER TABLE model_history_normalized "
                f'ADD COLUMN IF NOT EXISTS "{col_name}" {col_type}'
            )
    logger.info("model_history_tables_ensured", db_path=str(db_path))


def upsert_model_history(
    db_path: Path,
    model_registry_df: pd.DataFrame,
    model_history_list_df: pd.DataFrame,
    model_history_normalized_df: pd.DataFrame,
) -> None:
    """Upsert all three model history DataFrames into DuckDB.

    ``model_registry`` uses ``ON CONFLICT DO UPDATE`` so a model's
    ``last_synced_at`` is always current.  ``model_history_list`` uses
    ``ON CONFLICT DO NOTHING`` to avoid duplicating records from
    overlapping runs.  ``model_history_normalized`` also dedups on
    ``record_id`` but refreshes the two derived classification columns
    (``change_type`` / ``object_type``) on conflict, so re-runs backfill
    and re-classification propagate to existing rows while the immutable
    export columns are left untouched.

    The two bulk tables merge set-based (register + ``INSERT … SELECT``)
    rather than row-by-row — large tenants produce hundreds of thousands
    of history rows per run, and per-row ``executemany`` upserts are
    pathologically slow/memory-hungry under DuckDB (see
    :func:`_upsert_events`).

    Args:
        db_path: Path to the DuckDB database file.
        model_registry_df: One-row DataFrame from the transform service.
        model_history_list_df: Per-record list DataFrame.
        model_history_normalized_df: Normalized change detail DataFrame.

    Raises:
        StorageLoadError: If any upsert operation fails.
    """
    try:
        with closing(_connect(db_path)) as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                # model_registry — update on re-run to refresh last_synced_at
                for _, row in model_registry_df.iterrows():
                    conn.execute(
                        "INSERT INTO model_registry "
                        "(model_id, model_name, workspace_id, workspace_name, last_synced_at) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (model_id) DO UPDATE SET "
                        "model_name = excluded.model_name, "
                        "workspace_id = excluded.workspace_id, "
                        "workspace_name = excluded.workspace_name, "
                        "last_synced_at = excluded.last_synced_at",
                        (
                            row["model_id"],
                            row["model_name"],
                            row["workspace_id"],
                            row["workspace_name"],
                            row["last_synced_at"],
                        ),
                    )

                # model_history_list — ignore duplicates (same record_id)
                if len(model_history_list_df):
                    conn.register("_mhl_batch", model_history_list_df)
                    conn.execute(
                        "INSERT INTO model_history_list "
                        "(record_id, model_id, date_time_utc) "
                        "SELECT record_id, model_id, date_time_utc FROM _mhl_batch "
                        "ON CONFLICT (record_id) DO NOTHING"
                    )
                    conn.unregister("_mhl_batch")

                # model_history_normalized — ignore duplicates
                norm_cols = [
                    "record_id",
                    "anaplan_record_id",
                    "model_id",
                    "date_time_utc",
                    "user",
                    "description",
                    "security_change",
                    "previous_value",
                    "new_value",
                    "module_list",
                    "line_item_property",
                    "customer",
                    "export",
                    "import_action",
                    "data_types",
                    "table_name",
                    "object",
                    "target_user",
                    "captured_at",
                    "change_type",
                    "object_type",
                ]
                quoted_norm_cols = ", ".join(f'"{c}"' for c in norm_cols)
                if len(model_history_normalized_df):
                    conn.register("_mhn_batch", model_history_normalized_df[norm_cols])
                    # ON CONFLICT refreshes ONLY the two derived classification
                    # columns. record_id hashes the immutable source fields
                    # (model_id/date_time_utc/user/description/row_idx), so every
                    # raw export column is stable for a given record_id and must
                    # not be rewritten. change_type/object_type are derived from
                    # `description` by rules that evolve — DO NOTHING would freeze
                    # them at whatever an earlier run stored (including NULL when a
                    # record first landed before classification), so a re-run could
                    # never backfill or re-classify. DO UPDATE on just these two
                    # keeps dedup (still one row per record_id) while letting
                    # reclassification propagate.
                    conn.execute(
                        f"INSERT INTO model_history_normalized ({quoted_norm_cols}) "
                        f"SELECT {quoted_norm_cols} FROM _mhn_batch "
                        f"ON CONFLICT (record_id) DO UPDATE SET "
                        f"change_type = excluded.change_type, "
                        f"object_type = excluded.object_type"
                    )
                    conn.unregister("_mhn_batch")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        logger.info(
            "model_history_upserted",
            db_path=str(db_path),
            registry_rows=len(model_registry_df),
            list_rows=len(model_history_list_df),
            normalized_rows=len(model_history_normalized_df),
        )
    except StorageLoadError:
        raise
    except Exception as exc:
        raise StorageLoadError(
            f"Failed to upsert model history into DuckDB: {exc}",
            context={"db_path": str(db_path)},
        ) from exc


def record_unmatched_descriptions(
    db_path: Path,
    counts: dict[str, int],
    *,
    captured_at: str | None = None,
) -> None:
    """Persist the failsafe report of descriptions with no change-type rule.

    Upserts one row per description into ``mh_unmatched_descriptions``:
    ``occurrences`` is replaced with this run's count, ``last_seen_at`` is
    advanced, ``first_seen_at`` is preserved.  Colleagues query this table
    (DBeaver, or ``anaplan-audit mh-unmatched``) to author new rules.

    A no-op when *counts* is empty.  Never raises past a warning — the report
    is a diagnostic aid and must not crash the run.

    Args:
        db_path: Path to the DuckDB database file.
        counts: ``{description: occurrences}`` for this run.
        captured_at: ISO timestamp; defaults to now (UTC).
    """
    if not counts:
        return
    ts = captured_at or datetime.now(UTC).isoformat()
    with closing(_connect(db_path)) as conn:
        conn.execute(_MH_UNMATCHED_DDL)
        conn.execute("BEGIN TRANSACTION")
        try:
            for description, occurrences in counts.items():
                conn.execute(
                    "INSERT INTO mh_unmatched_descriptions "
                    "(description, occurrences, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (description) DO UPDATE SET "
                    "occurrences = excluded.occurrences, last_seen_at = excluded.last_seen_at",
                    (description, int(occurrences), ts, ts),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    logger.info("mh_unmatched_recorded", distinct=len(counts))


def read_unmatched_descriptions(db_path: Path) -> pd.DataFrame:
    """Return the failsafe report ordered by most-recent then most-frequent.

    Empty frame (with the right columns) if the table doesn't exist yet.
    """
    cols = ["description", "occurrences", "first_seen_at", "last_seen_at"]
    with closing(_connect(db_path)) as conn:
        if not _table_exists(conn, "mh_unmatched_descriptions"):
            return pd.DataFrame(columns=cols)
        return conn.execute(
            "SELECT description, occurrences, first_seen_at, last_seen_at "
            "FROM mh_unmatched_descriptions ORDER BY last_seen_at DESC, occurrences DESC"
        ).df()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def backup_database(db_path: Path, *, max_backups: int = 7) -> Path | None:
    """Create a timestamped copy of the DuckDB database.

    Backups are written alongside the source file, e.g.::

        anaplan_audit_backup_20260412_143000.duckdb

    A ``CHECKPOINT`` is issued first so any writes still sitting in
    DuckDB's WAL sidecar file are flushed into the main database file —
    without it, a plain file copy could silently miss recent data.

    After creating the new backup, any backups beyond *max_backups* (ordered
    oldest-first) are deleted to keep disk usage bounded.

    Args:
        db_path: Path to the live DuckDB database file.
        max_backups: Maximum number of backups to retain.  Set to ``0`` to
            disable rotation.

    Returns:
        The path of the new backup file, or ``None`` if the database does not
        exist yet (nothing to back up).
    """
    if not db_path.exists():
        logger.debug("backup_skipped_no_database", db_path=str(db_path))
        return None

    # Flush the WAL into the main file so the copy is complete. A failure
    # here (e.g. another connection mid-transaction) downgrades to a
    # warning — the copy still proceeds and DuckDB can replay/recover a
    # sidecar WAL, but the checkpoint makes the common case airtight.
    try:
        with closing(_connect(db_path)) as conn:
            conn.execute("CHECKPOINT")
    except Exception as exc:
        logger.warning("backup_checkpoint_failed", error=str(exc))

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}_backup_{timestamp}{db_path.suffix}")

    shutil.copy2(str(db_path), str(backup_path))
    logger.info(
        "database_backed_up",
        source=str(db_path),
        backup=str(backup_path),
    )

    if max_backups > 0:
        _cleanup_old_backups(db_path, max_backups=max_backups)

    return backup_path


def _cleanup_old_backups(db_path: Path, *, max_backups: int) -> None:
    """Remove the oldest backups, keeping only *max_backups* total.

    Args:
        db_path: Path to the live DuckDB database file (used to locate
            siblings with the backup naming convention).
        max_backups: Number of backups to retain.
    """
    stem = db_path.stem
    backups = sorted(
        db_path.parent.glob(f"{stem}_backup_*{db_path.suffix}"),
        key=lambda p: p.stat().st_mtime,
    )
    excess = backups[: max(0, len(backups) - max_backups)]
    for old in excess:
        try:
            old.unlink()
            logger.info("backup_removed", path=str(old))
        except OSError as exc:
            logger.warning("backup_removal_failed", path=str(old), error=str(exc))


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


def purge_old_history(db_path: Path, retention_years: int = 2) -> None:
    """Delete model history records older than the retention window.

    Long-term storage: customers who need history beyond the default 2-year
    retention window should export to an external SQL database or data
    warehouse before this cutoff is reached.  See the Operations Runbook
    (Section 7.4) for guidance.

    Args:
        db_path: Path to the DuckDB database file.
        retention_years: Number of years to retain.  Defaults to 2.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=retention_years * 365)).isoformat()

    with closing(_connect(db_path)) as conn:
        # DuckDB returns the deleted-row count as a result set rather than
        # exposing a cursor rowcount.
        norm_row = conn.execute(
            "DELETE FROM model_history_normalized WHERE date_time_utc < ?",
            (cutoff,),
        ).fetchone()
        list_row = conn.execute(
            "DELETE FROM model_history_list WHERE date_time_utc < ?",
            (cutoff,),
        ).fetchone()

    logger.info(
        "model_history_purged",
        cutoff=cutoff,
        normalized_deleted=norm_row[0] if norm_row else 0,
        list_deleted=list_row[0] if list_row else 0,
    )


def purge_old_audit_events(db_path: Path, retention_years: int) -> None:
    """Delete audit events older than the retention window.

    No-op when ``retention_years`` is 0 or the events table doesn't exist.
    ``eventDate`` is epoch milliseconds, so the cutoff is computed in ms.

    Args:
        db_path: Path to the DuckDB database file.
        retention_years: Number of years to retain.  ``0`` disables purging.
    """
    if retention_years <= 0:
        return

    cutoff_ms = int((datetime.now(UTC) - timedelta(days=retention_years * 365)).timestamp() * 1000)

    with closing(_connect(db_path)) as conn:
        if not _table_exists(conn, _EVENTS_TABLE):
            return
        deleted_row = conn.execute(
            f"DELETE FROM {_EVENTS_TABLE} WHERE eventDate < ?",
            (cutoff_ms,),
        ).fetchone()

    logger.info(
        "audit_events_purged",
        cutoff_ms=cutoff_ms,
        retention_years=retention_years,
        deleted=deleted_row[0] if deleted_row else 0,
    )
