"""Execute the audit SQL transform and return the result as a DataFrame."""

from __future__ import annotations

import importlib.resources
import time
from contextlib import closing
from pathlib import Path

import duckdb
import pandas as pd
import structlog

from anaplan_audit.exceptions import QueryExecutionError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def run_audit_query(db_path: Path, *, tenant_name: str = "") -> pd.DataFrame:
    """Execute ``audit_query.sql`` against the loaded DuckDB database.

    The SQL file is loaded via :mod:`importlib.resources` from the package
    data, preserving it as the canonical source of truth.  ``{{time_stamp}}``
    is substituted before execution (an integer computed here, safe to
    inline); the tenant name is bound as a real query parameter
    (``$tenant_name``) so a name containing a quote can never break the SQL.

    Args:
        db_path: Path to the DuckDB database file.
        tenant_name: Anaplan tenant name bound into the query as
            ``TENANT_NAME``.

    Returns:
        A :class:`~pandas.DataFrame` with the transformed audit data.

    Raises:
        QueryExecutionError: If the SQL execution fails.
    """
    try:
        sql = (
            importlib.resources.files("anaplan_audit.transform.queries")
            .joinpath("audit_query.sql")
            .read_text()
        )

        # Substitute the batch timestamp before execution.
        batch_ts = int(time.time() * 1000)  # milliseconds, consistent with eventDate
        sql = sql.replace("{{time_stamp}}", str(batch_ts))

        with closing(duckdb.connect(str(db_path))) as conn:
            # Session timezone must be UTC — DuckDB defaults to the host
            # machine's local timezone, and the query formats timestamps
            # via strftime, which renders in session time.
            conn.execute("SET TimeZone = 'UTC'")
            df: pd.DataFrame = conn.execute(sql, {"tenant_name": tenant_name}).df()

        logger.info("audit_query_executed", row_count=len(df), batch_ts=batch_ts)
        return df
    except QueryExecutionError:
        raise
    except Exception as exc:
        raise QueryExecutionError(
            f"Failed to execute audit_query.sql: {exc}",
            context={"db_path": str(db_path)},
        ) from exc
