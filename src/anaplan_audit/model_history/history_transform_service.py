"""Model History Transform Service — normalize dynamic export CSV.

Anaplan model history exports have a dynamic column structure: the columns
present depend on what changed during the exported time window.  This module
accepts the raw CSV text and normalizes it into a fixed, predictable flat
schema suitable for database storage and Anaplan upload.

v4 design
~~~~~~~~~
Column-name resolution stays in Python — it's fuzzy substring matching over
~20 header strings and costs nothing.  The row-level work (padding, field
projection, record-ID hashing) moved from a per-row :mod:`csv` loop into a
single DuckDB SQL statement over ``read_csv``:

* the raw CSV text is spilled to a temporary file (DuckDB's reader takes a
  path, not an in-memory buffer — verified in the Phase 0 spike);
* ``all_varchar=true`` — every target column is text; letting the sniffer
  guess types per-tenant-export would add inconsistency for zero benefit;
* ``null_padding=true`` pads short/ragged rows (DuckDB pads with NULL; the
  projection COALESCEs to ``''`` to preserve the v3 pad-with-empty-string
  behavior exactly);
* ``strict_mode=false`` tolerates rows with *extra* trailing columns, which
  the v3 loop silently ignored via index-bounded access;
* the record ID is ``substring(sha256(...), 1, 16)`` in SQL — verified
  byte-identical to v3's ``hashlib.sha256(...).hexdigest()[:16]`` for the
  same input string (Phase 0 spike), so dedup keys are stable across the
  engine swap.

:func:`_generate_record_id` is retained as the reference implementation —
the parity harness asserts SQL output against it.

Three DataFrames are returned:

1. ``model_registry`` — one row identifying this workspace/model pair.
2. ``model_history_list`` — one row per change record (for the numbered list).
3. ``model_history_normalized`` — full normalized change data.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
import structlog

from anaplan_audit.model_history import classification

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

# Characters that Anaplan list items do not allow.
_INVALID_CHARS: re.Pattern[str] = re.compile(r'[/\\:*?"<>|]')

# Normalized column names for the output DataFrames.
NORMALIZED_COLUMNS: list[str] = [
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
    # v3.8 classification — derived from `description`, appended last so the
    # Anaplan property-based import binds them by name without disturbing the
    # position of any prior column. NOT imported from the export; see
    # anaplan_audit.model_history.classification.
    "change_type",
    "object_type",
]

# The two derived columns above are not sourced from the export CSV — they are
# computed from `description` by the classifier, so the import-projection loop
# skips them and a wrapping SELECT adds them via DuckDB UDFs.
_CLASSIFICATION_COLUMNS: list[str] = ["change_type", "object_type"]

# Mapping from normalized column name to known dynamic header patterns.
# Each value is a list of lowercase substrings to search for (case-insensitive).
# Order matters: earlier entries claim their header first; remaining headers
# are logged as unmapped.
_COLUMN_MAP: dict[str, list[str]] = {
    "description": ["description"],
    "previous_value": ["previous value", "before"],
    "new_value": ["new value", "after"],
    "security_change": ["security change", "security", "role"],
    "module_list": ["module/list", "module list", "module"],
    "line_item_property": ["line item/property", "line item", "property"],
    "customer": ["customer", "sku"],
    "export": ["export"],
    "import_action": ["import"],
    "data_types": ["data types", "data type"],
    "table_name": ["table name"],
    "object": ["object"],
    # v3.4.0 — role-change events carry a "Target User" column identifying
    # the user whose access was modified. Previously logged as unmapped
    # and dropped; now stored on model_history_normalized so the
    # reporting model can show attribution for security changes.
    "target_user": ["target user", "targetuser"],
}


def sanitize_model_name(name: str) -> str:
    """Strip characters that Anaplan list items do not allow.

    Removes ``/ \\ : * ? " < > |`` and collapses any resulting double spaces.

    Args:
        name: Raw model name string.

    Returns:
        Sanitized model name safe for Anaplan list import.
    """
    sanitized = _INVALID_CHARS.sub(" ", name)
    # Collapse multiple spaces that may result from substitution.
    sanitized = re.sub(r" {2,}", " ", sanitized).strip()
    return sanitized


def _find_column(headers: list[str], patterns: list[str]) -> str | None:
    """Return the first header that contains any of the given substrings.

    Args:
        headers: List of column headers from the dynamic CSV.
        patterns: Lowercase substrings to search for.

    Returns:
        The matching header, or ``None`` if no match is found.
    """
    for header in headers:
        lower = header.lower()
        if any(p in lower for p in patterns):
            return header
    return None


def _generate_record_id(
    model_id: str,
    date_time_utc: str,
    user: str,
    description: str,
    row_index: int,
) -> str:
    """Generate a stable, unique record ID from row content.

    Reference implementation — production hashing runs inside DuckDB SQL
    (``substring(sha256(...), 1, 16)`` over the identical input string),
    and the parity tests assert the two agree.  Content-based hashing
    ensures the same Anaplan history record always produces the same ID
    regardless of when the export runs — enabling conflict-ignoring
    upserts to correctly deduplicate on re-runs.

    ``row_index`` (the zero-based position of the row in the export) is
    included to distinguish rows with identical content (same user, timestamp,
    and description) that appear at different positions in the CSV.

    Note: Anaplan's ``ID`` column was tested and found to be a batch/
    transaction identifier rather than a per-row ID — one save operation
    groups many rows under the same ID, making it unsuitable as a unique key.
    It is still stored as ``anaplan_record_id`` for reference.

    Args:
        model_id: Anaplan model ID.
        date_time_utc: Change timestamp from the export row.
        user: User email from the export row.
        description: Change description from the export row.
        row_index: Zero-based position of this row in the export CSV.

    Returns:
        A 16-character hex string.
    """
    raw = f"{model_id}:{date_time_utc}:{user}:{description}:{row_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _build_column_mapping(
    headers: list[str],
    log: structlog.stdlib.BoundLogger,
) -> dict[str, str]:
    """Map normalized column names to dynamic CSV headers.

    Args:
        headers: Raw header row from the export CSV.
        log: Bound logger for unmapped-column warnings.

    Returns:
        Dict mapping ``normalized_col_name -> dynamic_header``.
    """
    assigned: dict[str, str] = {}
    remaining = list(headers)

    for norm_col, patterns in _COLUMN_MAP.items():
        match = _find_column(remaining, patterns)
        if match:
            assigned[norm_col] = match
            remaining.remove(match)

    # date_time_utc — Anaplan exports use "Date/Time (UTC)" (slash, not space)
    date_col = _find_column(remaining, ["date_time_utc", "date/time", "date time", "timestamp"])
    if date_col:
        assigned["date_time_utc"] = date_col
        remaining.remove(date_col)
    elif "date_time_utc" in headers:
        assigned["date_time_utc"] = "date_time_utc"
        if "date_time_utc" in remaining:
            remaining.remove("date_time_utc")

    # user
    user_col = _find_column(remaining, ["user"])
    if user_col:
        assigned["user"] = user_col
        remaining.remove(user_col)

    # Anaplan's own record ID — exact case-insensitive match only.
    # Substring matching (e.g. "id") would falsely match "Modified", "Grid",
    # etc., so we require the header to be exactly "ID" after stripping.
    id_col = next((h for h in remaining if h.strip().upper() == "ID"), None)
    if id_col:
        assigned["anaplan_record_id"] = id_col
        remaining.remove(id_col)

    # Any columns still in remaining could not be mapped.  If module_list was
    # not matched above (older export format without a "Module/List" header),
    # fall back to the first remaining column so the field is not silently
    # lost.  Columns beyond that are logged as unmapped.
    if remaining:
        if "module_list" not in assigned:
            assigned["module_list"] = remaining[0]
            unmapped = remaining[1:]
        else:
            unmapped = remaining
        if unmapped:
            log.warning(
                "model_history_unmapped_columns",
                unmapped=unmapped,
                note="These columns were not mapped to any normalized field",
            )

    return assigned


def _qi(name: str) -> str:
    """Quote a dynamic CSV header for use as a DuckDB identifier."""
    return '"' + name.replace('"', '""') + '"'


def _empty_result(
    model_id: str,
    safe_model_name: str,
    workspace_id: str,
    workspace_name: str,
    captured_at: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Registry row plus empty list/normalized frames — the no-data shape."""
    registry = pd.DataFrame(
        [
            {
                "model_id": model_id,
                "model_name": safe_model_name,
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "last_synced_at": captured_at,
            }
        ]
    )
    empty_list = pd.DataFrame(columns=["record_id", "model_id", "date_time_utc"])
    empty_norm = pd.DataFrame(columns=NORMALIZED_COLUMNS)
    return registry, empty_list, empty_norm


def normalize_model_history(
    csv_text: str,
    model_id: str,
    model_name: str,
    workspace_id: str,
    workspace_name: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Normalize a raw model history export CSV into three flat DataFrames.

    Row-level work runs inside DuckDB (see module docstring); this function
    resolves the dynamic column mapping, builds the projection SQL, and
    shapes the three output frames.

    Thread safety: called concurrently from the model-history worker pool.
    Each call opens its own in-memory DuckDB instance, so workers never
    share a connection.

    Args:
        csv_text: Raw CSV string from the Anaplan export download.
        model_id: Anaplan model ID.
        model_name: Raw model name (will be sanitized for Anaplan).
        workspace_id: Anaplan workspace ID.
        workspace_name: Human-readable workspace name.

    Returns:
        A three-tuple of ``(model_registry_df, model_history_list_df,
        model_history_normalized_df)``.  All string fields default to
        empty string — never ``None`` / NaN.
    """
    log = logger.bind(model_id=model_id, model_name=model_name)
    captured_at = datetime.now(UTC).isoformat()
    safe_model_name = sanitize_model_name(model_name)

    log.info("model_history_parse_start")

    # Anaplan model history exports are tab-delimited; detect automatically
    # so that the parser is resilient if Anaplan ever switches to commas.
    # Detection stays in Python (cheap, and strictly safer than trusting
    # the engine's sniffer to agree with historic behavior).
    first_line = csv_text.split("\n")[0] if csv_text else ""
    delimiter = "\t" if "\t" in first_line else ","

    # Read only the header row — csv.reader is lazy, so this consumes just
    # the first record even for a multi-hundred-MB export.
    reader = csv.reader(io.StringIO(csv_text), delimiter=delimiter)
    _sentinel: list[str] = []
    headers: list[str] = next(reader, _sentinel)
    if not headers:
        log.warning("model_history_empty_export")
        return _empty_result(model_id, safe_model_name, workspace_id, workspace_name, captured_at)

    log.info("model_history_parse_start", columns=headers)
    assigned = _build_column_mapping(headers, log)

    # --- Build the projection: one SELECT expression per normalized column.
    # Mapped columns COALESCE to '' (v3 padded short rows with ''); unmapped
    # columns are '' literals so every output row carries the full shape.
    def col_expr(norm_col: str) -> str:
        dyn = assigned.get(norm_col)
        return f"COALESCE({_qi(dyn)}, '')" if dyn else "''"

    hash_input = (
        "$model_id || ':' || "
        f"{col_expr('date_time_utc')} || ':' || "
        f"{col_expr('user')} || ':' || "
        f"{col_expr('description')} || ':' || "
        "CAST(__row_idx AS VARCHAR)"
    )

    select_parts: list[str] = []
    for norm_col in NORMALIZED_COLUMNS:
        if norm_col in _CLASSIFICATION_COLUMNS:
            continue  # derived, added by the wrapping SELECT below
        if norm_col == "record_id":
            select_parts.append(f"substring(sha256({hash_input}), 1, 16) AS record_id")
        elif norm_col == "model_id":
            select_parts.append('$model_id AS "model_id"')
        elif norm_col == "captured_at":
            select_parts.append('$captured_at AS "captured_at"')
        else:
            select_parts.append(f'{col_expr(norm_col)} AS "{norm_col}"')

    # Spill to a temp file — DuckDB's CSV reader takes a path, not a buffer.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
    ) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(csv_text)
    try:
        # Escape the (fully controlled) temp path for inlining — table
        # functions can't take bound parameters.
        path_literal = str(tmp_path).replace("'", "''")
        delim_literal = "\\t" if delimiter == "\t" else delimiter

        # Inner: import + normalize the 19 base columns. Outer: append the two
        # derived classification columns (change_type BEFORE object_type — the
        # locked v4 column order) via the registered UDFs on `description`.
        sql = (
            f"SELECT *, "
            f"classify_change_type(description) AS change_type, "
            f"classify_object_type(description) AS object_type "
            f"FROM ("
            f"  SELECT {', '.join(select_parts)} FROM ("
            f"    SELECT *, (row_number() OVER ()) - 1 AS __row_idx"
            f"    FROM read_csv('{path_literal}',"
            f"      delim='{delim_literal}', header=true, all_varchar=true,"
            f"      null_padding=true, strict_mode=false,"
            # strict_mode=false silently disables quote-escape handling
            # unless quote/escape are pinned explicitly — without these,
            # a field like "quote "" inside" keeps its doubled quotes and
            # record_id hashes diverge from v3 (caught by the parity suite).
            f"      quote='\"', escape='\"')"
            f"  )"
            f")"
        )

        with duckdb.connect(":memory:") as conn:
            # String type names ("VARCHAR") rather than duckdb.typing objects —
            # this DuckDB build (1.5.x wheel) has no `duckdb.typing` module but
            # accepts the string form.
            conn.create_function(
                "classify_change_type",
                classification.classify_change_type,
                ["VARCHAR"],
                "VARCHAR",
            )
            conn.create_function(
                "classify_object_type",
                classification.classify_object_type,
                ["VARCHAR"],
                "VARCHAR",
            )
            model_history_normalized_df = conn.execute(
                sql, {"model_id": model_id, "captured_at": captured_at}
            ).df()
    finally:
        tmp_path.unlink(missing_ok=True)

    # End-of-run classification signal: rank the descriptions that fell to the
    # catchall so colleagues have a working set for authoring new rules
    # (scope §5.5). Cheap — few distinct descriptions per export.
    if len(model_history_normalized_df):
        unmatched = classification.summarize_unmatched(
            model_history_normalized_df.loc[
                model_history_normalized_df["change_type"] == classification.CATCHALL_CHANGE_TYPE,
                "description",
            ].tolist()
        )
        if unmatched.total:
            log.info(
                "mh_classification_unmatched_summary",
                total=unmatched.total,
                unique_patterns=unmatched.unique,
                top=unmatched.top,
            )

    log.info(
        "model_history_parse_complete",
        row_count=len(model_history_normalized_df),
        columns=headers,
    )

    model_registry_df = pd.DataFrame(
        [
            {
                "model_id": model_id,
                "model_name": safe_model_name,
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "last_synced_at": captured_at,
            }
        ]
    )

    model_history_list_df = model_history_normalized_df[
        ["record_id", "model_id", "date_time_utc"]
    ].copy()

    log.info(
        "model_history_normalize_complete",
        normalized_rows=len(model_history_normalized_df),
        safe_model_name=safe_model_name,
    )

    return model_registry_df, model_history_list_df, model_history_normalized_df
