"""Backfill the additionalAttributes named columns on historical events.

Spec assumption vs. reality
---------------------------
Spec Milestone 4 assumes a ``raw_event`` or raw ``additionalAttributes``
string is retained on the ``events`` table for backfill to reparse. v3
never stored either — but every ``additionalAttributes.*`` sub-key is
still on the table as its own dotted column, thanks to
:func:`pandas.json_normalize` at load time. That is *functionally
equivalent* to a raw archive: we reconstruct the parsed dict from the
dotted columns and re-run the extractor.

v4 note
-------
v4 databases start fresh (no v3 data migration), and every row written
by v4 populates ``additional_attributes_raw`` at load time — so on a v4
database this command normally finds zero candidates. It is kept for
operators who point v4 at a database produced by a future export path.

Unlike v3 (which streamed the SELECT and updated in place on the same
SQLite connection), candidate rows are materialized up front: DuckDB
invalidates an open streaming result when a write executes on the same
connection. Bound memory was the v3 concern; given the fresh-start
reality above, a full fetch is acceptable for this maintenance command
and is capped by ``--limit`` when needed.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import duckdb
import structlog

from anaplan_audit.transform.additional_attributes import (
    ADDITIONAL_ATTRIBUTES_COLUMNS,
    extract_from_dict,
)
from anaplan_audit.transform.loader import _connect, _table_exists

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


_EVENTS_TABLE = "events"
_ATTRS_PREFIX = "additionalAttributes."
_BATCH_SIZE = 1000


@dataclass(frozen=True)
class BackfillSummary:
    """One-line result of a backfill run.

    Returned by :func:`backfill_additional_attributes` so callers can
    log or assert on the counts without re-querying the database.
    """

    rows_scanned: int
    rows_updated: int
    rows_skipped_no_data: int
    dry_run: bool


def _dotted_attribute_columns(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Return every ``additionalAttributes.<field>`` column on events.

    The extractor cares about a fixed subset of these, but backfill
    walks *every* dotted column so any sub-field that made it into an
    older DB (including ones we don't have named columns for) rebuilds
    into the raw archive.
    """
    rows = conn.execute(f"PRAGMA table_info({_EVENTS_TABLE})").fetchall()
    return [row[1] for row in rows if row[1].startswith(_ATTRS_PREFIX)]


def _row_to_attrs_dict(
    row: tuple[object, ...],
    dotted_cols: list[str],
) -> dict[str, str] | None:
    """Reconstruct the parsed additionalAttributes dict from a row.

    ``row`` is ``(id, *dotted_values)`` — the dotted values start at
    position 1, in the same order as *dotted_cols* (the SELECT column
    order is controlled by the caller).

    Returns ``None`` when the row has no non-null dotted values — the
    "no raw available" case reported by the summary. Otherwise strips
    the ``additionalAttributes.`` prefix off each populated column and
    returns the resulting mini-dict.
    """
    reconstructed: dict[str, str] = {}
    for idx, col in enumerate(dotted_cols, start=1):
        value = row[idx]
        if value in (None, ""):
            continue
        sub_key = col[len(_ATTRS_PREFIX) :]
        reconstructed[sub_key] = str(value)
    return reconstructed or None


def backfill_additional_attributes(
    db_path: Path,
    *,
    since_epoch_ms: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    progress: bool = True,
    enabled_categories: set[str] | None = None,
    retain_raw: bool = True,
) -> BackfillSummary:
    """Reproject dotted additionalAttributes columns onto named columns.

    Selects rows where ``additional_attributes_raw IS NULL`` — treated
    as "not yet backfilled" — reconstructs a parsed attributes dict
    from every ``additionalAttributes.<field>`` column that has a
    non-null value, runs the shared extractor, and updates the row.
    Idempotent: a re-run finds no candidates.

    Args:
        db_path: DuckDB database file.
        since_epoch_ms: Optional ``eventDate`` lower bound in
            **milliseconds**, matching how the events table stores it.
            ``None`` scans every unbackfilled row.
        limit: Maximum rows to touch this run. ``None`` for unlimited.
        dry_run: When ``True``, skip the ``UPDATE`` and report what
            would have happened.
        progress: When ``True``, render a rich progress bar. Silenced
            in test / non-interactive contexts.
        enabled_categories: Passed through to the extractor to gate
            which named columns are populated.
        retain_raw: Passed through to the extractor to gate the raw
            archive column.
    """
    log = logger.bind(
        component="backfill",
        dry_run=dry_run,
        since_epoch_ms=since_epoch_ms,
        limit=limit,
    )
    log.info("backfill_started")

    scanned = 0
    updated = 0
    skipped = 0

    with closing(_connect(db_path)) as conn:
        # Table might not exist yet on a fresh install; treat as no-op.
        if not _table_exists(conn, _EVENTS_TABLE):
            log.warning("backfill_skipped_no_events_table")
            return BackfillSummary(0, 0, 0, dry_run)

        dotted = _dotted_attribute_columns(conn)
        if not dotted:
            log.warning("backfill_skipped_no_dotted_columns")
            return BackfillSummary(0, 0, 0, dry_run)

        where_clauses = ["additional_attributes_raw IS NULL"]
        params: list[object] = []
        if since_epoch_ms is not None:
            where_clauses.append("eventDate >= ?")
            params.append(since_epoch_ms)
        where_sql = " AND ".join(where_clauses)
        limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""

        select_cols = ["id", *dotted]
        quoted_select_cols = ", ".join(f'"{c}"' for c in select_cols)
        select_sql = (
            f"SELECT {quoted_select_cols} FROM {_EVENTS_TABLE} WHERE {where_sql}{limit_sql}"
        )

        # Materialize candidates before updating — DuckDB invalidates a
        # streaming result when a write runs on the same connection.
        candidate_rows = conn.execute(select_sql, params).fetchall()
        total_estimate = len(candidate_rows)

        progress_bar = _make_progress(total_estimate) if progress else None

        update_cols = ADDITIONAL_ATTRIBUTES_COLUMNS
        update_set = ", ".join(f'"{c}" = ?' for c in update_cols)
        update_sql = f"UPDATE {_EVENTS_TABLE} SET {update_set} WHERE id = ?"

        def _flush(batch: list[tuple[object, ...]]) -> None:
            if not batch or dry_run:
                return
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.executemany(update_sql, batch)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        batch: list[tuple[object, ...]] = []
        with progress_bar or _NullProgress():
            for row in candidate_rows:
                scanned += 1
                attrs = _row_to_attrs_dict(row, dotted)
                if attrs is None:
                    skipped += 1
                    if progress_bar:
                        progress_bar.advance()
                    continue

                extraction = extract_from_dict(
                    attrs,
                    enabled_categories=enabled_categories,
                    retain_raw=retain_raw,
                )
                values = (*(extraction[c] for c in update_cols), row[0])
                batch.append(values)
                updated += 1

                if len(batch) >= _BATCH_SIZE:
                    _flush(batch)
                    batch.clear()

                if progress_bar:
                    progress_bar.advance()

            _flush(batch)

    summary = BackfillSummary(
        rows_scanned=scanned,
        rows_updated=updated,
        rows_skipped_no_data=skipped,
        dry_run=dry_run,
    )
    log.info(
        "backfill_completed",
        rows_scanned=scanned,
        rows_updated=updated,
        rows_skipped_no_data=skipped,
    )
    return summary


class _NullProgress:
    """No-op context manager for the non-progress path."""

    def __enter__(self) -> _NullProgress:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None


def _make_progress(total: int) -> _ProgressBar:
    """Small wrapper so the rich dependency is imported lazily."""
    return _ProgressBar(total)


class _ProgressBar:
    """Rich-backed progress bar; degrades gracefully when rich is off."""

    def __init__(self, total: int) -> None:
        # Imported inside the method so tests can run without a TTY.
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            transient=True,
        )
        self._task_id = self._progress.add_task(
            "Backfilling additionalAttributes",
            total=max(total, 1),
        )

    def __enter__(self) -> _ProgressBar:
        self._progress.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._progress.__exit__(exc_type, exc_val, exc_tb)

    def advance(self) -> None:
        self._progress.update(self._task_id, advance=1)
