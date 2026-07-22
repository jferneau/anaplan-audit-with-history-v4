"""Tests for the Model History transform service."""

from __future__ import annotations

import textwrap
from contextlib import closing
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from anaplan_audit.model_history.history_transform_service import (
    normalize_model_history,
    sanitize_model_name,
)
from anaplan_audit.transform.loader import (
    ensure_model_history_tables,
    purge_old_history,
    upsert_model_history,
)

MODEL_ID = "m001"
MODEL_NAME = "Finance Model"
WS_ID = "ws001"
WS_NAME = "Corporate FP&A"

_SAMPLE_CSV = textwrap.dedent("""\
    date_time_utc,user,description,Previous Value,New Value,Security,Object,Customer,Export,Module A
    2025-06-15T10:00:00Z,alice@example.com,Changed formula,100,200,FALSE,Line Item,,,"Module A"
    2025-06-15T11:00:00Z,bob@example.com,Created list item,,New Item,FALSE,List,,,"Module A"
""")


class TestSanitizeModelName:
    def test_strips_invalid_characters(self) -> None:
        assert sanitize_model_name("Finance/Model:2024") == "Finance Model 2024"

    def test_strips_all_invalid_chars(self) -> None:
        result = sanitize_model_name(r'a/b\c:d*e?f"g<h>i|j')
        assert "/" not in result
        assert "\\" not in result
        assert ":" not in result
        assert "*" not in result
        assert "?" not in result
        assert '"' not in result
        assert "<" not in result
        assert ">" not in result
        assert "|" not in result

    def test_collapses_extra_spaces(self) -> None:
        result = sanitize_model_name("Finance//Model")
        assert "  " not in result  # No double spaces

    def test_clean_name_unchanged(self) -> None:
        assert sanitize_model_name("Finance Model 2024") == "Finance Model 2024"

    def test_strips_and_trims(self) -> None:
        result = sanitize_model_name("/Leading slash")
        assert not result.startswith(" ")


class TestNormalizeModelHistory:
    def test_returns_three_dataframes(self) -> None:
        reg, lst, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert isinstance(reg, pd.DataFrame)
        assert isinstance(lst, pd.DataFrame)
        assert isinstance(norm, pd.DataFrame)

    def test_model_registry_has_one_row(self) -> None:
        reg, _, _ = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(reg) == 1
        assert reg.iloc[0]["model_id"] == MODEL_ID
        assert reg.iloc[0]["workspace_id"] == WS_ID
        assert reg.iloc[0]["workspace_name"] == WS_NAME

    def test_model_registry_sanitizes_name(self) -> None:
        reg, _, _ = normalize_model_history(
            _SAMPLE_CSV, MODEL_ID, "Finance/Model:v2", WS_ID, WS_NAME
        )
        assert "/" not in reg.iloc[0]["model_name"]
        assert ":" not in reg.iloc[0]["model_name"]

    def test_history_list_row_count_matches_csv(self) -> None:
        _, lst, _ = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(lst) == 2

    def test_history_list_has_required_columns(self) -> None:
        _, lst, _ = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert "record_id" in lst.columns
        assert "model_id" in lst.columns
        assert "date_time_utc" in lst.columns

    def test_normalized_has_all_columns(self) -> None:
        from anaplan_audit.model_history.history_transform_service import NORMALIZED_COLUMNS

        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        for col in NORMALIZED_COLUMNS:
            assert col in norm.columns, f"Missing column: {col}"

    def test_no_nulls_in_normalized(self) -> None:
        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        # All string columns should be empty string, not NaN/None.
        assert not norm.isnull().any().any()


class TestV380ClassificationColumns:
    """change_type / object_type are the last two columns, and every row
    classifies (v3.8 handoff — locked position + zero-regression contract)."""

    def test_classification_columns_are_last_two(self) -> None:
        from anaplan_audit.model_history.history_transform_service import NORMALIZED_COLUMNS

        assert NORMALIZED_COLUMNS[-2:] == ["change_type", "object_type"]

    def test_prior_columns_keep_name_and_position(self) -> None:
        # Snapshot of the v3.7 column order — the two new columns append only.
        from anaplan_audit.model_history.history_transform_service import NORMALIZED_COLUMNS

        assert NORMALIZED_COLUMNS[:-2] == [
            "record_id", "anaplan_record_id", "model_id", "date_time_utc", "user",
            "description", "security_change", "previous_value", "new_value",
            "module_list", "line_item_property", "customer", "export",
            "import_action", "data_types", "table_name", "object", "target_user",
            "captured_at",
        ]

    def test_normalized_output_columns_end_with_classification(self) -> None:
        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert list(norm.columns)[-2:] == ["change_type", "object_type"]

    def test_every_row_classifies_never_blank(self) -> None:
        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert (norm["change_type"] != "").all()
        assert (norm["object_type"] != "").all()


class TestUnmatchedReport:
    """The failsafe report of descriptions with no change-type rule."""

    def test_record_and_read_round_trip(self, tmp_path: Path) -> None:
        from anaplan_audit.transform.loader import (
            ensure_model_history_tables,
            read_unmatched_descriptions,
            record_unmatched_descriptions,
        )

        db = tmp_path / "mh.duckdb"
        ensure_model_history_tables(db)
        record_unmatched_descriptions(db, {"Add Import": 2, "Add Action": 1})
        report = read_unmatched_descriptions(db)
        rows = {r["description"]: int(r["occurrences"]) for _, r in report.iterrows()}
        assert rows == {"Add Import": 2, "Add Action": 1}

    def test_second_run_replaces_count_keeps_first_seen(self, tmp_path: Path) -> None:
        from anaplan_audit.transform.loader import (
            ensure_model_history_tables,
            read_unmatched_descriptions,
            record_unmatched_descriptions,
        )

        db = tmp_path / "mh.duckdb"
        ensure_model_history_tables(db)
        record_unmatched_descriptions(
            db, {"Add Import": 5}, captured_at="2026-01-01T00:00:00+00:00"
        )
        record_unmatched_descriptions(
            db, {"Add Import": 9}, captured_at="2026-02-01T00:00:00+00:00"
        )
        report = read_unmatched_descriptions(db)
        row = report.iloc[0]
        assert int(row["occurrences"]) == 9  # replaced, not summed
        assert row["first_seen_at"].startswith("2026-01-01")  # preserved
        assert row["last_seen_at"].startswith("2026-02-01")  # advanced

    def test_read_missing_table_returns_empty(self, tmp_path: Path) -> None:
        from anaplan_audit.transform.loader import read_unmatched_descriptions

        assert read_unmatched_descriptions(tmp_path / "nope.duckdb").empty

    def test_known_columns_mapped(self) -> None:
        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        # 'description' should be mapped from "description" column
        assert norm.iloc[0]["description"] == "Changed formula"
        # 'previous_value' from "Previous Value"
        assert norm.iloc[0]["previous_value"] == "100"
        # 'new_value' from "New Value"
        assert norm.iloc[0]["new_value"] == "200"
        # 'user' mapped
        assert norm.iloc[0]["user"] == "alice@example.com"

    def test_record_ids_are_unique(self) -> None:
        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert norm["record_id"].nunique() == len(norm)

    def test_empty_csv_produces_empty_dataframes(self) -> None:
        empty_csv = "date_time_utc,user,description\n"
        reg, lst, norm = normalize_model_history(empty_csv, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(reg) == 1  # Registry always has one row
        assert len(lst) == 0
        assert len(norm) == 0


class TestModelHistorySQLite:
    """Integration tests for the SQLite model history functions."""

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "test_history.db"
        ensure_model_history_tables(path)
        return path

    def test_tables_created(self, db_path: Path) -> None:
        with closing(duckdb.connect(str(db_path))) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_type='BASE TABLE'"
                ).fetchall()
            }
        assert "model_registry" in tables
        assert "model_history_list" in tables
        assert "model_history_normalized" in tables

    def test_ensure_tables_idempotent(self, db_path: Path) -> None:
        """Calling ensure_model_history_tables twice should not raise."""
        ensure_model_history_tables(db_path)  # Second call

    def test_upsert_and_query(self, db_path: Path) -> None:
        reg, lst, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        upsert_model_history(db_path, reg, lst, norm)

        with closing(duckdb.connect(str(db_path))) as conn:
            reg_count = conn.execute("SELECT COUNT(*) FROM model_registry").fetchone()[0]
            list_count = conn.execute("SELECT COUNT(*) FROM model_history_list").fetchone()[0]
            norm_count = conn.execute("SELECT COUNT(*) FROM model_history_normalized").fetchone()[0]

        assert reg_count == 1
        assert list_count == 2
        assert norm_count == 2

    def test_upsert_is_idempotent(self, db_path: Path) -> None:
        """Re-upserting the same records should not duplicate them."""
        reg, lst, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        upsert_model_history(db_path, reg, lst, norm)
        upsert_model_history(db_path, reg, lst, norm)

        with closing(duckdb.connect(str(db_path))) as conn:
            norm_count = conn.execute("SELECT COUNT(*) FROM model_history_normalized").fetchone()[0]

        assert norm_count == 2

    def test_reupsert_refreshes_derived_classification(self, db_path: Path) -> None:
        """A record first stored with stale/blank change_type/object_type is
        backfilled on re-run — DO NOTHING would freeze the old value forever.

        Reproduces the live-run bug: rows inserted before classification was
        wired carried NULL change_type/object_type, and every subsequent run
        (same record_id) left them NULL, so the uploaded CSV — and Anaplan —
        got blank derived columns. The upsert must refresh these two columns.
        """
        reg, lst, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        real_change_types = set(norm["change_type"])
        real_object_types = set(norm["object_type"])

        # First run stores the record with blank derived columns (the stale state).
        stale = norm.copy()
        stale["change_type"] = ""
        stale["object_type"] = ""
        upsert_model_history(db_path, reg, lst, stale)

        # Second run carries the real classification — it must overwrite.
        upsert_model_history(db_path, reg, lst, norm)

        with closing(duckdb.connect(str(db_path))) as conn:
            rows = conn.execute(
                "SELECT change_type, object_type FROM model_history_normalized"
            ).fetchall()

        assert len(rows) == 2  # still deduped, not duplicated
        assert {r[0] for r in rows} == real_change_types
        assert {r[1] for r in rows} == real_object_types
        assert "" not in {r[0] for r in rows}  # no stale blanks survived

    def test_purge_removes_old_records(self, db_path: Path) -> None:
        """Records with date_time_utc beyond the retention window are deleted."""
        # Insert a record with a very old date.
        old_csv = textwrap.dedent("""\
            date_time_utc,user,description
            2000-01-01T00:00:00Z,old@example.com,Old record
        """)
        reg, lst, norm = normalize_model_history(old_csv, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        upsert_model_history(db_path, reg, lst, norm)

        # Purge with 2-year retention — 2000 records should be deleted.
        purge_old_history(db_path, retention_years=2)

        with closing(duckdb.connect(str(db_path))) as conn:
            norm_count = conn.execute("SELECT COUNT(*) FROM model_history_normalized").fetchone()[0]

        assert norm_count == 0

    def test_purge_keeps_recent_records(self, db_path: Path) -> None:
        """Records within the retention window are not deleted."""
        reg, lst, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        upsert_model_history(db_path, reg, lst, norm)
        purge_old_history(db_path, retention_years=2)

        with closing(duckdb.connect(str(db_path))) as conn:
            norm_count = conn.execute("SELECT COUNT(*) FROM model_history_normalized").fetchone()[0]

        # 2024 records are within 2 years of 2026 — should be kept.
        assert norm_count == 2

    def test_schema_migration_adds_new_columns(self, tmp_path: Path) -> None:
        """ensure_model_history_tables adds import_action/data_types/table_name to old DBs."""
        db_path = tmp_path / "legacy.db"
        # Simulate a pre-migration database: create the table WITHOUT the new columns.
        with closing(duckdb.connect(str(db_path))) as conn:
            conn.execute("""
                CREATE TABLE model_history_normalized (
                    record_id   TEXT PRIMARY KEY,
                    model_id    TEXT NOT NULL,
                    date_time_utc TEXT NOT NULL,
                    user        TEXT,
                    description TEXT,
                    export      TEXT,
                    object      TEXT,
                    captured_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE model_registry (
                    model_id       TEXT PRIMARY KEY,
                    model_name     TEXT NOT NULL,
                    workspace_id   TEXT NOT NULL,
                    workspace_name TEXT NOT NULL,
                    last_synced_at TEXT NOT NULL
                )
            """)
            conn.commit()

        # Running ensure_model_history_tables should add the missing columns.
        ensure_model_history_tables(db_path)

        with closing(duckdb.connect(str(db_path))) as conn:
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(model_history_normalized)"
                ).fetchall()
            }

        assert "import_action" in cols
        assert "data_types" in cols
        assert "table_name" in cols
        # v3.4.0 — Target User is picked up from role-change export rows.
        assert "target_user" in cols

    def test_schema_migration_is_idempotent(self, db_path: Path) -> None:
        """Calling ensure_model_history_tables on a current-schema DB does not raise."""
        # db_path fixture already has the full schema from the first call.
        # A second call must be safe (duplicate column errors swallowed).
        ensure_model_history_tables(db_path)  # should not raise


class TestTargetUserColumnRestoration:
    """v3.4.0 — Anaplan's model history export carries a ``Target User``
    column on role-change events (the user whose access was modified).
    Previously the normalizer treated it as unknown, logged it as an
    unmapped column, and dropped the value entirely. Now it lands on
    ``model_history_normalized.target_user``.
    """

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "test_target_user.db"
        ensure_model_history_tables(path)
        return path

    _ROLE_CHANGE_CSV = (
        "Date/Time (UTC),User,Description,Previous Value,New Value,Target User\n"
        "2025-08-01T09:30:00Z,admin@example.com,Role changed,Model Builder,"
        "Workspace Admin,charlie@example.com\n"
        "2025-08-01T09:45:00Z,admin@example.com,Role changed,Read Only,"
        "Model Builder,dana@example.com\n"
    )

    def test_target_user_column_is_populated(self) -> None:
        _reg, _lst, norm = normalize_model_history(
            self._ROLE_CHANGE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        assert "target_user" in norm.columns
        assert norm["target_user"].tolist() == ["charlie@example.com", "dana@example.com"]

    def test_target_user_is_empty_when_not_present(self) -> None:
        # Non-role-change exports have no Target User column — the field
        # must be an empty string, not NaN or missing.
        _reg, _lst, norm = normalize_model_history(
            _SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        assert "target_user" in norm.columns
        assert (norm["target_user"] == "").all()

    def test_target_user_matches_case_insensitively(self) -> None:
        # Anaplan's header casing has varied over time. Match on lowercase
        # substring "target user" (or "targetuser") like every other
        # dynamic header pattern in _COLUMN_MAP.
        csv = textwrap.dedent("""\
            date_time_utc,user,description,TargetUser
            2025-08-01T09:30:00Z,admin@example.com,Access granted,zoe@example.com
        """)
        _reg, _lst, norm = normalize_model_history(csv, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert norm["target_user"].tolist() == ["zoe@example.com"]

    def test_target_user_persists_through_upsert(self, db_path: Path) -> None:
        reg, lst, norm = normalize_model_history(
            self._ROLE_CHANGE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        upsert_model_history(db_path, reg, lst, norm)
        with closing(duckdb.connect(str(db_path))) as conn:
            # Both fixture rows share the same "user" — ORDER BY user was a
            # tie that SQLite happened to break in insertion order and DuckDB
            # doesn't. Sort on the column under test for determinism.
            rows = conn.execute(
                "SELECT target_user FROM model_history_normalized ORDER BY target_user"
            ).fetchall()
        assert [r[0] for r in rows] == ["charlie@example.com", "dana@example.com"]
