"""Tests for csv.reader streaming edge cases in normalize_model_history."""

from __future__ import annotations

import textwrap

from anaplan_audit.model_history.history_transform_service import (
    NORMALIZED_COLUMNS,
    _build_column_mapping,
    normalize_model_history,
)

MODEL_ID = "m001"
MODEL_NAME = "Finance Model"
WS_ID = "ws001"
WS_NAME = "Corporate FP&A"


class TestEmptyExport:
    def test_completely_empty_csv_returns_empty_dataframes(self) -> None:
        """An export with no content at all (empty string) should produce empty output."""
        reg, lst, norm = normalize_model_history("", MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(reg) == 1  # Registry always has one row
        assert len(lst) == 0
        assert len(norm) == 0

    def test_empty_csv_normalized_has_correct_columns(self) -> None:
        """Empty-export DataFrames must still carry the full normalized schema."""
        _, _, norm = normalize_model_history("", MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        for col in NORMALIZED_COLUMNS:
            assert col in norm.columns, f"Missing column: {col}"

    def test_empty_csv_list_has_correct_columns(self) -> None:
        _, lst, _ = normalize_model_history("", MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert "record_id" in lst.columns
        assert "model_id" in lst.columns
        assert "date_time_utc" in lst.columns

    def test_header_only_csv_produces_zero_rows(self) -> None:
        """A CSV with a header row but no data rows returns empty output."""
        csv_text = "date_time_utc,user,description\n"
        _, lst, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(lst) == 0
        assert len(norm) == 0


class TestShortRowPadding:
    def test_short_row_does_not_raise(self) -> None:
        """A data row with fewer columns than the header must be padded, not crash."""
        csv_text = textwrap.dedent("""\
            date_time_utc,user,description,Previous Value,New Value
            2025-06-01T00:00:00Z,alice@example.com
        """)
        _reg, _lst, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(norm) == 1

    def test_short_row_missing_columns_become_empty_string(self) -> None:
        """Padded (missing) columns must appear as empty string, not NaN."""
        csv_text = textwrap.dedent("""\
            date_time_utc,user,description,Previous Value,New Value
            2025-06-01T00:00:00Z,alice@example.com
        """)
        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert not norm.isnull().any().any()
        assert norm.iloc[0]["previous_value"] == ""
        assert norm.iloc[0]["new_value"] == ""

    def test_short_row_populated_columns_still_mapped(self) -> None:
        """Columns that ARE present in a short row must still be captured."""
        csv_text = textwrap.dedent("""\
            date_time_utc,user,description
            2025-06-01T00:00:00Z,alice@example.com
        """)
        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert norm.iloc[0]["date_time_utc"] == "2025-06-01T00:00:00Z"
        assert norm.iloc[0]["user"] == "alice@example.com"


class TestColumnMapping:
    def test_build_column_mapping_description(self) -> None:
        import structlog

        log = structlog.get_logger()
        headers = ["date_time_utc", "user", "description", "Previous Value", "New Value"]
        mapping = _build_column_mapping(headers, log)
        assert mapping["description"] == "description"
        assert mapping["previous_value"] == "Previous Value"
        assert mapping["new_value"] == "New Value"
        assert mapping["date_time_utc"] == "date_time_utc"
        assert mapping["user"] == "user"

    def test_unmapped_header_becomes_module_list(self) -> None:
        """A header that doesn't match any known pattern is mapped to module_list."""
        import structlog

        log = structlog.get_logger()
        headers = ["date_time_utc", "user", "Weird Column"]
        mapping = _build_column_mapping(headers, log)
        assert mapping.get("module_list") == "Weird Column"

    def test_dynamic_column_names_matched_case_insensitively(self) -> None:
        """Column matching must be case-insensitive."""
        import structlog

        log = structlog.get_logger()
        headers = ["DATE TIME UTC", "USER", "DESCRIPTION"]
        mapping = _build_column_mapping(headers, log)
        # "DATE TIME UTC" contains "date time" pattern
        assert mapping.get("date_time_utc") == "DATE TIME UTC"


class TestTabDelimitedAnaplanExport:
    """Tests for real Anaplan tab-delimited model history exports.

    Anaplan exports model history as TSV (tab-separated), not CSV.  Without
    explicit delimiter detection the entire header row is read as a single
    column, causing only ``model_id`` and ``description`` to populate while
    all other line items stay blank.
    """

    # Mirrors the exact header seen in live Anaplan exports (full column set).
    _HEADER = "\t".join(
        [
            "ID",
            "Date/Time (UTC)",
            "User",
            "Description",
            "Security Change",
            "Previous Value",
            "New Value",
            "Module/List",
            "Line Item/Property",
            "SKU",
            "Target User",
            "Export",
            "Import",
            "Data Types",
            "Table Name",
            "Object",
        ]
    )
    _ROW = "\t".join(
        [
            "1",
            "2025-06-01T10:00:00Z",
            "alice@example.com",
            "Changed formula",
            "",
            "Old Value",
            "New Value",
            "Revenue Module",
            "Net Revenue",
            "SKU-001",
            "",
            "Revenue Export",
            "Revenue Import",
            "Numeric",
            "Revenue Table",
            "Revenue Model",
        ]
    )

    def test_tab_export_produces_one_row(self) -> None:
        """Tab-delimited Anaplan export parses exactly one data row."""
        csv_text = f"{self._HEADER}\n{self._ROW}\n"
        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(norm) == 1

    def test_tab_export_maps_all_known_columns(self) -> None:
        """All mapped columns contain clean scalar values, not tab-joined rows."""
        csv_text = f"{self._HEADER}\n{self._ROW}\n"
        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        row = norm.iloc[0]

        assert row["date_time_utc"] == "2025-06-01T10:00:00Z"
        assert row["user"] == "alice@example.com"
        assert row["description"] == "Changed formula"
        assert row["previous_value"] == "Old Value"
        assert row["new_value"] == "New Value"
        assert row["module_list"] == "Revenue Module"
        assert row["line_item_property"] == "Net Revenue"
        assert row["export"] == "Revenue Export"
        assert row["import_action"] == "Revenue Import"
        assert row["data_types"] == "Numeric"
        assert row["table_name"] == "Revenue Table"
        assert row["object"] == "Revenue Model"

    def test_tab_export_no_embedded_tabs_in_values(self) -> None:
        """Each column value must be a clean scalar — no embedded tab characters."""
        csv_text = f"{self._HEADER}\n{self._ROW}\n"
        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        row = norm.iloc[0]

        # The old bug: entire row was stored as description with embedded tabs.
        assert "\t" not in row["description"]
        assert "\t" not in row["user"]

    def test_tab_export_module_list_is_not_record_id(self) -> None:
        """module_list must map to 'Module/List', not the 'ID' column."""
        csv_text = f"{self._HEADER}\n{self._ROW}\n"
        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        # 'ID' from Anaplan is the first column; it must NOT appear as module_list.
        assert norm.iloc[0]["module_list"] != "1"
        assert norm.iloc[0]["module_list"] == "Revenue Module"

    def test_comma_delimited_csv_still_works(self) -> None:
        """Comma-delimited input (older format or test fixtures) still parses correctly."""
        csv_text = "date_time_utc,user,description\n2025-06-01T00:00:00Z,alice@example.com,Test\n"
        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(norm) == 1
        assert norm.iloc[0]["user"] == "alice@example.com"
        assert norm.iloc[0]["description"] == "Test"


class TestStableRecordIds:
    """Record IDs must be deterministic across runs (content-based hashing)."""

    def test_same_export_twice_produces_same_record_ids(self) -> None:
        """Re-processing the same export CSV produces identical record_ids."""
        header = "ID\tDate/Time (UTC)\tUser\tDescription"
        rows = "42\t2025-06-01T10:00:00Z\talice@example.com\tChanged formula"
        csv_text = f"{header}\n{rows}\n"

        _, _, norm1 = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        _, _, norm2 = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)

        assert norm1.iloc[0]["record_id"] == norm2.iloc[0]["record_id"]

    def test_anaplan_record_id_stored_in_output(self) -> None:
        """Anaplan's own ID value is stored in the anaplan_record_id column."""
        header = "ID\tDate/Time (UTC)\tUser\tDescription"
        rows = "99\t2025-06-01T10:00:00Z\talice@example.com\tChange"
        csv_text = f"{header}\n{rows}\n"

        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)

        assert norm.iloc[0]["anaplan_record_id"] == "99"

    def test_without_id_column_record_ids_still_unique(self) -> None:
        """Fallback (no ID column) still produces unique record_ids per row."""
        csv_text = (
            "Date/Time (UTC)\tUser\tDescription\n"
            "2025-06-01T10:00:00Z\talice@example.com\tChange A\n"
            "2025-06-01T11:00:00Z\tbob@example.com\tChange B\n"
        )
        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)

        assert norm["record_id"].nunique() == 2
        assert (norm["anaplan_record_id"] == "").all()

    def test_record_id_does_not_depend_on_run_time(self) -> None:
        """record_id must be the same regardless of when the export is processed."""
        # Two rows with different content to ensure both IDs are stable.
        header = "Date/Time (UTC)\tUser\tDescription"
        csv_text = (
            f"{header}\n"
            "2025-06-01T10:00:00Z\talice@example.com\tChanged formula\n"
            "2025-06-01T11:00:00Z\tbob@example.com\tAdded list item\n"
        )

        _, _, norm1 = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        _, _, norm2 = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)

        # Both runs must produce identical record_ids even though captured_at differs.
        assert list(norm1["record_id"]) == list(norm2["record_id"])

    def test_duplicate_content_rows_get_unique_ids_via_row_index(self) -> None:
        """Two rows with identical content still get distinct IDs (row_index tiebreak)."""
        header = "Date/Time (UTC)\tUser\tDescription"
        csv_text = (
            f"{header}\n"
            "2025-06-01T10:00:00Z\talice@example.com\tSave\n"
            "2025-06-01T10:00:00Z\talice@example.com\tSave\n"
        )

        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)

        assert norm["record_id"].nunique() == 2


class TestRowCount:
    def test_row_count_matches_csv_data_rows(self) -> None:
        csv_text = textwrap.dedent("""\
            date_time_utc,user,description
            2025-06-01T00:00:00Z,alice@example.com,Change A
            2025-06-02T00:00:00Z,bob@example.com,Change B
            2025-06-03T00:00:00Z,carol@example.com,Change C
        """)
        _, lst, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(norm) == 3
        assert len(lst) == 3

    def test_list_and_normalized_row_counts_match(self) -> None:
        csv_text = textwrap.dedent("""\
            date_time_utc,user,description
            2025-06-01T00:00:00Z,alice@example.com,Change A
            2025-06-02T00:00:00Z,bob@example.com,Change B
        """)
        _, lst, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(lst) == len(norm)

    def test_all_record_ids_unique_across_many_rows(self) -> None:
        rows = "\n".join(
            f"2025-06-{i:02d}T00:00:00Z,user{i}@example.com,Change {i}" for i in range(1, 21)
        )
        csv_text = f"date_time_utc,user,description\n{rows}\n"
        _, _, norm = normalize_model_history(csv_text, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(norm) == 20
        assert norm["record_id"].nunique() == 20
