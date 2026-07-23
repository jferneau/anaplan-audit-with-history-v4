"""EVENT_ID taxonomy — prefix→category derivation and its SQL projection.

The taxonomy is the single source of truth for list parentage: every event
code resolves to a parent so nothing lands orphaned under ``All Events``. These
tests lock the mapping, the catch-all, the DuckDB UDFs, and the sync between
the code and the shipped ``EVENT_CATEGORIES.csv`` seed.
"""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb
import pytest

from anaplan_audit import taxonomy

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class TestCategoryForCode:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("USR-8", ("USR", "USER ACTIVITY")),
            ("USR-81", ("USR", "USER ACTIVITY")),  # code new to the catalog
            ("AUTHZ-11", ("AUTHZ", "ACCESS CONTROL")),
            ("CONN-1", ("CONN", "SAML CONNECTION")),
            ("INT-01", ("INT", "INTEGRATION")),  # CloudWorks + ADO merged
            ("FRCST-30", ("FRCST", "FORECASTER")),
            ("PIQ-05", ("PIQ", "PLANIQ")),  # PlanIQ kept distinct from Forecaster
            ("WF-100", ("WF", "WORKFLOW")),  # Task + Template merged
            ("WF-1006", ("WF", "WORKFLOW")),
            ("DSM-071", ("DSM", "ENCRYPTION ACTIVITY")),
            ("DSM-DAO0071I", ("DSM", "ENCRYPTION ACTIVITY")),  # Guardian flattened in
            ("OAUTH-0", ("OAUTH", "OAUTH")),
            ("COMMENT-01", ("COMMENT", "COMMENT")),
        ],
    )
    def test_known_prefixes(self, code: str, expected: tuple[str, str]) -> None:
        assert taxonomy.category_for_code(code) == expected

    @pytest.mark.parametrize("code", ["", None, "XYZ-1", "NOPREFIX", "1234"])
    def test_unknown_or_blank_falls_back_to_uncategorized(self, code: str | None) -> None:
        # The whole point: an unmapped or empty code is still parented, never
        # dropped and never orphaned.
        assert taxonomy.category_for_code(code) == taxonomy.UNCATEGORIZED

    def test_lookup_is_case_insensitive_on_prefix(self) -> None:
        assert taxonomy.category_for_code("usr-8") == ("USR", "USER ACTIVITY")


class TestUdfProjection:
    def test_udfs_derive_parent_in_sql(self) -> None:
        with duckdb.connect(":memory:") as conn:
            taxonomy.register_udfs(conn)
            # Callers coalesce NULL → '' before the UDF (DuckDB skips a Python
            # UDF on NULL), and '' resolves to the catch-all.
            rows = conn.execute(
                "SELECT event_parent_code(c), event_parent_name(c) "
                "FROM (VALUES ('USR-8'), ('WF-112'), ('ZZZ-9'), ('')) t(c)"
            ).fetchall()
        assert rows == [
            ("USR", "USER ACTIVITY"),
            ("WF", "WORKFLOW"),
            ("UNCAT", "UNCATEGORIZED"),
            ("UNCAT", "UNCATEGORIZED"),
        ]


class TestSeedSync:
    def test_example_seed_matches_taxonomy(self) -> None:
        # examples/EVENT_CATEGORIES.csv is generated from taxonomy.CATEGORIES;
        # this guards against the two drifting apart.
        with (EXAMPLES / "EVENT_CATEGORIES.csv").open(newline="") as f:
            rows = list(csv.DictReader(f))
        seed = [(r["Code"], r["Name"]) for r in rows]
        assert seed == taxonomy.CATEGORIES
        assert all(r["Parent"] == "All Events" for r in rows)

    def test_uncategorized_is_present_as_catchall(self) -> None:
        assert taxonomy.UNCATEGORIZED in taxonomy.CATEGORIES
