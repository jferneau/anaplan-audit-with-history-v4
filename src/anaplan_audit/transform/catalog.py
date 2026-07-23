"""Make the activity-code catalog the complete, self-parenting EVENT_ID source.

The reporting model's ``EVENT_ID`` list is fed from ``ACTIVITY_CODES.csv``. For
that import to place every code under a parent — and never leave one orphaned
under ``All Events`` — the underlying ``act_codes`` table needs two things the
shipped static catalog can't provide on its own:

1. **A parent column.** Derived from each code's prefix via
   :mod:`anaplan_audit.taxonomy`, so the import maps ``Parent`` directly.
2. **Every code that actually occurs.** The shipped catalog is a point-in-time
   snapshot of Anaplan's documented codes; the live tenant emits codes it
   doesn't list yet (new ``USR-``/``WF-``/``AUTHZ-`` numbers). Those are unioned
   in from the full ``events`` table so they, too, arrive parented.

Running this after :func:`~anaplan_audit.transform.loader.load_to_duckdb` makes
``ACTIVITY_CODES.csv`` the single writer of the ``EVENT_ID`` list: the audit
fact import then only references the list, and orphans become structurally
impossible.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import duckdb
import pandas as pd
import structlog

from anaplan_audit import taxonomy

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

_CODE_COL = "Event Code"
_PARENT_CODE_COL = "Parent Code"
_PARENT_COL = "Parent"


def augment_activity_catalog(db_path: Path) -> None:
    """Rebuild ``act_codes`` = static catalog plus observed codes, with parents.

    A no-op (logged) if the ``act_codes`` table isn't present. Safe on a first
    run with no ``events`` table yet — the static catalog is still parented.

    Args:
        db_path: Path to the DuckDB database file.
    """
    with closing(duckdb.connect(str(db_path))) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
        }
        if "act_codes" not in tables:
            logger.warning("activity_catalog_missing", note="act_codes table not loaded")
            return

        static_df = conn.execute("SELECT * FROM act_codes").df()

        observed: list[str] = []
        if "events" in tables:
            observed = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT eventTypeId FROM events "
                    "WHERE eventTypeId IS NOT NULL AND eventTypeId <> ''"
                ).fetchall()
            ]

        known = {str(c) for c in static_df[_CODE_COL].dropna()}
        new_codes = sorted(c for c in observed if c not in known)
        if new_codes:
            combined = pd.concat(
                [static_df, pd.DataFrame({_CODE_COL: new_codes})],
                ignore_index=True,
            )
        else:
            combined = static_df.copy()

        categories = [taxonomy.category_for_code(c) for c in combined[_CODE_COL]]
        combined[_PARENT_CODE_COL] = [cat[0] for cat in categories]
        combined[_PARENT_COL] = [cat[1] for cat in categories]

        conn.register("_catalog_df", combined)
        conn.execute("CREATE OR REPLACE TABLE act_codes AS SELECT * FROM _catalog_df")
        conn.unregister("_catalog_df")

    logger.info(
        "activity_catalog_augmented",
        total_codes=len(combined),
        new_from_events=len(new_codes),
    )
