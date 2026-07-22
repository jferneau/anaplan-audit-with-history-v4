"""Milestone 4 tests — backfill from dotted additionalAttributes columns.

Verifies:
* Pre-v3.3.0 rows (populated dotted columns, null named columns) rebuild
  cleanly and update every managed column.
* Rows with no dotted values are skipped (reported in summary).
* Rows already backfilled (raw archive set) are not scanned.
* --dry-run does not write.
* --since filters by eventDate.
* --limit caps the number of rows touched.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import duckdb
import pandas as pd

from anaplan_audit.backfill import (
    BackfillSummary,
    backfill_additional_attributes,
)
from anaplan_audit.transform.loader import load_to_duckdb


def _seed_events(db_path: Path, rows: list[dict[str, object]]) -> None:
    load_to_duckdb(db_path, {"events": pd.DataFrame(rows)})


def _row(db_path: Path, row_id: str) -> dict[str, object]:
    """Fetch one event row as a name→value dict.

    DuckDB has no ``sqlite3.Row`` row-factory analog; the dict is built
    from ``cursor.description`` instead so tests keep by-name access.
    """
    with closing(duckdb.connect(str(db_path))) as conn:
        cur = conn.execute("SELECT * FROM events WHERE id = ?", (row_id,))
        assert cur.description is not None
        names = [d[0] for d in cur.description]
        row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


class TestBackfill:
    def test_rebuilds_named_columns_from_dotted(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_events(
            db_path,
            [
                {
                    "id": "1",
                    "eventDate": 1_700_000_000_000,
                    "additionalAttributes.appId": "app-uuid-1",
                    "additionalAttributes.appName": "Xperience 2025",
                    "additionalAttributes.pageName": "13 | G&A Expenses",
                    "app_id": None,
                    "app_name": None,
                    "page_name": None,
                    "additional_attributes_raw": None,
                }
            ],
        )
        summary = backfill_additional_attributes(db_path, progress=False)
        assert summary == BackfillSummary(
            rows_scanned=1, rows_updated=1, rows_skipped_no_data=0, dry_run=False
        )
        row = _row(db_path, "1")
        assert row["app_id"] == "app-uuid-1"
        assert row["app_name"] == "Xperience 2025"
        assert row["page_name"] == "13 | G&A Expenses"
        # Raw archive round-trips to the reconstructed dict.
        assert json.loads(row["additional_attributes_raw"]) == {
            "appId": "app-uuid-1",
            "appName": "Xperience 2025",
            "pageName": "13 | G&A Expenses",
        }

    def test_row_with_no_dotted_values_is_skipped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_events(
            db_path,
            [
                {
                    "id": "1",
                    "eventDate": 1_700_000_000_000,
                    "additionalAttributes.appId": None,
                    "additionalAttributes.appName": None,
                    "additional_attributes_raw": None,
                }
            ],
        )
        summary = backfill_additional_attributes(db_path, progress=False)
        # Scanned but not updated — no source data to project.
        assert summary.rows_scanned == 1
        assert summary.rows_updated == 0
        assert summary.rows_skipped_no_data == 1

    def test_already_backfilled_row_is_not_scanned(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        # Row 1 already has raw populated; must be filtered out at SELECT.
        _seed_events(
            db_path,
            [
                {
                    "id": "1",
                    "eventDate": 1_700_000_000_000,
                    "additionalAttributes.appId": "x",
                    "additional_attributes_raw": '{"appId":"x"}',
                    "app_id": "x",
                },
                {
                    "id": "2",
                    "eventDate": 1_700_000_000_000,
                    "additionalAttributes.appId": "y",
                    "additional_attributes_raw": None,
                    "app_id": None,
                },
            ],
        )
        summary = backfill_additional_attributes(db_path, progress=False)
        assert summary.rows_scanned == 1
        assert summary.rows_updated == 1
        # Row 1 untouched, row 2 filled.
        assert _row(db_path, "2")["app_id"] == "y"

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_events(
            db_path,
            [
                {
                    "id": "1",
                    "eventDate": 1_700_000_000_000,
                    "additionalAttributes.appId": "x",
                    "additional_attributes_raw": None,
                    "app_id": None,
                }
            ],
        )
        summary = backfill_additional_attributes(db_path, dry_run=True, progress=False)
        assert summary.dry_run is True
        assert summary.rows_updated == 1  # counted but not written
        # Row still null on disk.
        assert _row(db_path, "1")["app_id"] is None
        assert _row(db_path, "1")["additional_attributes_raw"] is None

    def test_since_filters_by_event_date(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_events(
            db_path,
            [
                {
                    "id": "old",
                    "eventDate": 1_600_000_000_000,
                    "additionalAttributes.appId": "old-x",
                    "additional_attributes_raw": None,
                },
                {
                    "id": "new",
                    "eventDate": 1_700_000_000_000,
                    "additionalAttributes.appId": "new-x",
                    "additional_attributes_raw": None,
                },
            ],
        )
        summary = backfill_additional_attributes(
            db_path,
            since_epoch_ms=1_650_000_000_000,
            progress=False,
        )
        assert summary.rows_scanned == 1
        assert summary.rows_updated == 1
        assert _row(db_path, "old")["app_id"] is None
        assert _row(db_path, "new")["app_id"] == "new-x"

    def test_limit_caps_updates(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_events(
            db_path,
            [
                {
                    "id": str(i),
                    "eventDate": 1_700_000_000_000,
                    "additionalAttributes.appId": f"app-{i}",
                    "additional_attributes_raw": None,
                }
                for i in range(5)
            ],
        )
        summary = backfill_additional_attributes(db_path, limit=2, progress=False)
        assert summary.rows_updated == 2

    def test_idempotent_second_run_is_noop(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_events(
            db_path,
            [
                {
                    "id": "1",
                    "eventDate": 1_700_000_000_000,
                    "additionalAttributes.appId": "x",
                    "additional_attributes_raw": None,
                }
            ],
        )
        first = backfill_additional_attributes(db_path, progress=False)
        second = backfill_additional_attributes(db_path, progress=False)
        assert first.rows_updated == 1
        assert second.rows_updated == 0
        assert second.rows_scanned == 0

    def test_no_events_table_is_a_noop(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        with closing(duckdb.connect(str(db_path))):
            pass
        summary = backfill_additional_attributes(db_path, progress=False)
        assert summary == BackfillSummary(0, 0, 0, False)

    def test_disabled_category_leaves_that_column_null(self, tmp_path: Path) -> None:
        # Spec Milestone 5 semantics: category-disabled → column stays null.
        db_path = tmp_path / "test.db"
        _seed_events(
            db_path,
            [
                {
                    "id": "1",
                    "eventDate": 1_700_000_000_000,
                    "additionalAttributes.appId": "x",
                    "additionalAttributes.actionId": "act-1",
                    "additional_attributes_raw": None,
                }
            ],
        )
        # Only 'action' enabled — uxAppPage columns must stay null.
        backfill_additional_attributes(
            db_path,
            enabled_categories={"action"},
            progress=False,
        )
        row = _row(db_path, "1")
        assert row["action_id"] == "act-1"
        assert row["app_id"] is None
        # Raw archive still built from every reconstructed sub-key.
        assert json.loads(row["additional_attributes_raw"]) == {
            "appId": "x",
            "actionId": "act-1",
        }
