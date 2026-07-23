"""Activity-code catalog augmentation — the orphan fix at the data layer.

``augment_activity_catalog`` makes ``ACTIVITY_CODES.csv`` the complete,
self-parenting EVENT_ID source: it adds a prefix-derived ``Parent`` to the
static catalog AND unions in every code observed in the events table, so a code
new to the audit stream still arrives parented instead of orphaned.
"""

from __future__ import annotations

import importlib.resources
import io
from collections import Counter
from contextlib import closing
from pathlib import Path

import duckdb
import pandas as pd

from anaplan_audit.transform.catalog import (
    MAX_EVENT_NAME_LEN,
    add_catalog_columns,
    augment_activity_catalog,
)

from .conftest import seed_tables


def _shipped_catalog_df() -> pd.DataFrame:
    txt = (
        importlib.resources.files("anaplan_audit.data").joinpath("activity_events.csv").read_text()
    )
    return pd.read_csv(io.StringIO(txt), keep_default_na=False)


class TestShippedCatalogIntegrity:
    """The EVENT_ID list keys items on their name, and Anaplan rejects a name
    over 60 chars or shared by two items. The computed ``Event Name`` must
    therefore be unique and within the limit for every shipped code."""

    def test_event_codes_are_unique(self) -> None:
        codes = _shipped_catalog_df()["Event Code"].tolist()
        dupes = [c for c, n in Counter(codes).items() if n > 1]
        assert dupes == [], f"duplicate Event Codes: {dupes}"

    def test_event_names_are_unique_and_within_limit(self) -> None:
        # A too-long or duplicated name = "Invalid name" on import (the failure
        # that left CONN-4, USR-81, OAUTH-0, ... unparented).
        names = add_catalog_columns(_shipped_catalog_df())["Event Name"].tolist()
        too_long = [n for n in names if len(n) > MAX_EVENT_NAME_LEN]
        dupes = [n for n, c in Counter(names).items() if c > 1]
        assert too_long == [], f"Event Names over {MAX_EVENT_NAME_LEN} chars: {too_long}"
        assert dupes == [], f"duplicate Event Names (would collide on import): {dupes}"

    def test_long_or_duplicate_message_falls_back_to_code(self) -> None:
        out = add_catalog_columns(_shipped_catalog_df())
        by_code = dict(zip(out["Event Code"], out["Event Name"], strict=True))
        assert by_code["CONN-4"] == "CONN-4"  # documented message is 62 chars
        assert by_code["OAUTH-0"] == "OAUTH-0"  # message 69 chars
        assert by_code["DSM-DAO0501I"] == "DSM-DAO0501I"  # duplicate of DSM-501


def _static_catalog() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Event Code": ["USR-8", "AUTHZ-1", "DSM-071"],
            "Event Message": ["User login success", "Access granted", "Create key pair"],
            "Associated Object ID": ["uid", "uid", "cid"],
            "Notes": ["--", "--", "--"],
        }
    )


def _read_catalog(db_path: Path) -> pd.DataFrame:
    with closing(duckdb.connect(str(db_path))) as conn:
        return conn.execute('SELECT * FROM act_codes ORDER BY "Event Code"').df()


class TestAugmentActivityCatalog:
    def test_adds_parent_columns_to_static_catalog(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        seed_tables(db, {"act_codes": _static_catalog()})
        augment_activity_catalog(db)
        df = _read_catalog(db)
        assert "Parent" in df.columns and "Parent Code" in df.columns
        by_code = dict(zip(df["Event Code"], df["Parent"], strict=True))
        assert by_code["USR-8"] == "USER ACTIVITY"
        assert by_code["AUTHZ-1"] == "ACCESS CONTROL"
        assert by_code["DSM-071"] == "ENCRYPTION ACTIVITY"

    def test_produces_valid_event_name_column(self, tmp_path: Path) -> None:
        # A too-long message falls back to the code; observed codes with no
        # message are code-named. Every name is within the limit and unique.
        db = tmp_path / "t.db"
        static = pd.DataFrame(
            {
                "Event Code": ["USR-8", "CONN-4"],
                "Event Message": [
                    "User login success",
                    "Workspace associated with connection configuration successfully",  # 62
                ],
                "Associated Object ID": ["", ""],
                "Notes": ["", ""],
            }
        )
        events = pd.DataFrame({"id": ["1"], "eventTypeId": ["WF-112"]})  # no message
        seed_tables(db, {"act_codes": static, "events": events})
        augment_activity_catalog(db)
        df = _read_catalog(db)
        names = dict(zip(df["Event Code"], df["Event Name"], strict=True))
        assert names["USR-8"] == "User login success"  # short + unique -> message
        assert names["CONN-4"] == "CONN-4"  # 62 chars -> code fallback
        assert names["WF-112"] == "WF-112"  # observed, no message -> code
        assert (df["Event Name"].str.len() <= MAX_EVENT_NAME_LEN).all()

    def test_unions_observed_codes_not_in_static_catalog(self, tmp_path: Path) -> None:
        # WF-112 / AUTHZ-11 appear in the audit stream but not the shipped
        # catalog — exactly the codes that were orphaning. They must be pulled
        # in and parented.
        db = tmp_path / "t.db"
        events = pd.DataFrame(
            {"id": ["1", "2", "3"], "eventTypeId": ["USR-8", "WF-112", "AUTHZ-11"]}
        )
        seed_tables(db, {"act_codes": _static_catalog(), "events": events})
        augment_activity_catalog(db)
        df = _read_catalog(db)
        by_code = dict(zip(df["Event Code"], df["Parent"], strict=True))
        assert by_code["WF-112"] == "WORKFLOW"
        assert by_code["AUTHZ-11"] == "ACCESS CONTROL"

    def test_no_code_is_left_without_a_parent(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        events = pd.DataFrame({"id": ["1", "2"], "eventTypeId": ["USR-8", "MYSTERY-9"]})
        seed_tables(db, {"act_codes": _static_catalog(), "events": events})
        augment_activity_catalog(db)
        df = _read_catalog(db)
        assert df["Parent"].notna().all()
        assert (df["Parent"].astype(str).str.len() > 0).all()
        # An unmapped prefix still gets the catch-all, never a blank.
        assert dict(zip(df["Event Code"], df["Parent"], strict=True))["MYSTERY-9"] == (
            "UNCATEGORIZED"
        )

    def test_observed_code_already_in_catalog_is_not_duplicated(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        events = pd.DataFrame({"id": ["1", "2"], "eventTypeId": ["USR-8", "USR-8"]})
        seed_tables(db, {"act_codes": _static_catalog(), "events": events})
        augment_activity_catalog(db)
        df = _read_catalog(db)
        assert (df["Event Code"] == "USR-8").sum() == 1

    def test_first_run_without_events_table_still_parents_static(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        seed_tables(db, {"act_codes": _static_catalog()})  # no events table yet
        augment_activity_catalog(db)  # must not raise
        df = _read_catalog(db)
        assert df["Parent"].notna().all()
        assert len(df) == 3

    def test_missing_catalog_table_is_a_noop(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        with closing(duckdb.connect(str(db))):
            pass
        augment_activity_catalog(db)  # must not raise
        with closing(duckdb.connect(str(db))) as conn:
            tables = [
                r[0]
                for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
            ]
        assert "act_codes" not in tables
