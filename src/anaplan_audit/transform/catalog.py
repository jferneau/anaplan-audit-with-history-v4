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
_MESSAGE_COL = "Event Message"
_NAME_COL = "Event Name"
_PARENT_CODE_COL = "Parent Code"
_PARENT_COL = "Parent"

# Anaplan rejects a list-item name longer than this (empirically verified: a
# 60-char name imports, 61 fails with "Invalid name").
MAX_EVENT_NAME_LEN = 60


# A synthetic stub the catalog carries for codes Anaplan emits but hasn't
# published a message for yet — "... event (message/description pending Anaplan
# documentation)". It carries no real information, so an event with one is named
# by its code rather than a truncated copy of the stub.
def _is_placeholder(message: str) -> bool:
    return "pending anaplan documentation" in message.lower()


def _fit(text: str, limit: int = MAX_EVENT_NAME_LEN) -> str:
    """Trim ``text`` to at most ``limit`` chars, preferring a word boundary.

    Cuts back to the last space so a name never ends mid-word, unless that
    would discard more than half the text (a single very long token), in which
    case it hard-cuts. No ellipsis — the result is a clean, valid list name.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip()


def _assign_event_names(codes: list[str], messages: list[str]) -> list[str]:
    """Compute a valid, unique EVENT_ID list-item name for each row.

    Anaplan caps list-item names at :data:`MAX_EVENT_NAME_LEN` and requires
    them unique, so a name can't always be the raw Event Message. The name is
    message-first, degrading only as far as each constraint forces:

    * **A genuine message that fits and is unused** → the message verbatim.
    * **Too long** → the message trimmed to the cap at a word boundary
      (:func:`_fit`), so the item still reads as its message.
    * **A real message shared by two codes** (e.g. ``DSM-DAO0071I`` mirrors
      ``DSM-071``) → the message plus the disambiguating code in brackets,
      trimmed to fit — e.g. ``Create key pair with key [DSM-DAO0071I]``.
    * **No message, or only a "pending Anaplan documentation" placeholder** →
      the code itself, which is shorter and more informative than a truncated
      stub (e.g. ``AUTHZ-17``, ``OAUTH-0``).

    The full message stays available in ``Event Message`` for the audit feed
    and an optional description property.
    """
    used: set[str] = set()
    names: list[str] = []
    for code, message in zip(codes, messages, strict=True):
        name = _event_name(code, (message or "").strip(), used)
        used.add(name)
        names.append(name)
    return names


def _event_name(code: str, message: str, used: set[str]) -> str:
    """Resolve one row's name given the names already taken (``used``)."""
    if not message or _is_placeholder(message):
        return code
    name = _fit(message)
    if name not in used:
        return name
    # The message duplicates an earlier row's — keep it readable by appending
    # the code that tells the two apart, trimming the message so it still fits.
    suffix = f" [{code}]"
    disambiguated = _fit(message, MAX_EVENT_NAME_LEN - len(suffix)) + suffix
    if disambiguated not in used and len(disambiguated) <= MAX_EVENT_NAME_LEN:
        return disambiguated
    # Pathological (duplicate code, or the disambiguated form still collides):
    # the code is always short and unique.
    return code


def add_catalog_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``Parent Code`` / ``Parent`` / ``Event Name`` to a catalog frame.

    Shared by the runtime augmentation and the example generator so the
    shipped ``ACTIVITY_CODES.csv`` and a live run produce identical columns.
    """
    out = df.copy()
    categories = [taxonomy.category_for_code(c) for c in out[_CODE_COL]]
    out[_PARENT_CODE_COL] = [cat[0] for cat in categories]
    out[_PARENT_COL] = [cat[1] for cat in categories]
    messages = out[_MESSAGE_COL].tolist() if _MESSAGE_COL in out.columns else [""] * len(out)
    out[_NAME_COL] = _assign_event_names(
        [str(c) for c in out[_CODE_COL]],
        ["" if pd.isna(m) else str(m) for m in messages],
    )
    return out


def augment_activity_catalog(db_path: Path) -> None:
    """Rebuild ``act_codes`` = static catalog plus observed codes, with parents.

    The audit stream is the ground truth for what Anaplan actually emits, so a
    code's ``Event Message`` is taken from the live ``events.message`` whenever
    one is present, falling back to the shipped catalog only for codes not seen
    live (or seen without a message). This keeps names/messages current as
    Anaplan renames events or ships new features — a brand-new code arrives
    parented *and* carrying its real message, with no catalog edit.

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
        live_msg: dict[str, str] = {}
        if "events" in tables:
            observed = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT eventTypeId FROM events "
                    "WHERE eventTypeId IS NOT NULL AND eventTypeId <> ''"
                ).fetchall()
            ]
            live_msg = _live_messages(conn)

        known = {str(c) for c in static_df[_CODE_COL].dropna()}
        new_codes = sorted(c for c in observed if c not in known)
        if new_codes:
            combined = pd.concat(
                [static_df, pd.DataFrame({_CODE_COL: new_codes})],
                ignore_index=True,
            )
        else:
            combined = static_df.copy()

        # Live message wins over the shipped catalog wherever the stream has
        # one; the catalog fills the gap for codes not seen live.
        if live_msg:
            if _MESSAGE_COL not in combined.columns:
                combined[_MESSAGE_COL] = ""
            combined[_MESSAGE_COL] = [
                live_msg.get(str(code), existing)
                for code, existing in zip(combined[_CODE_COL], combined[_MESSAGE_COL], strict=True)
            ]

        combined = add_catalog_columns(combined)

        conn.register("_catalog_df", combined)
        conn.execute("CREATE OR REPLACE TABLE act_codes AS SELECT * FROM _catalog_df")
        conn.unregister("_catalog_df")

    logger.info(
        "activity_catalog_augmented",
        total_codes=len(combined),
        new_from_events=len(new_codes),
        messages_from_stream=len(live_msg),
    )


def _live_messages(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Canonical live message per event code, or ``{}`` if unavailable.

    The audit stream is authoritative for event wording, so each code maps to
    its most frequent non-blank ``events.message``. A no-op when the ``events``
    table predates the ``message`` column (older DBs / minimal test fixtures).
    """
    event_cols = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'events'"
        ).fetchall()
    }
    if "message" not in event_cols:
        return {}
    return {
        str(code): msg
        for code, msg in conn.execute(
            "SELECT eventTypeId, arg_max(message, cnt) FROM ("
            "  SELECT eventTypeId, message, count(*) AS cnt FROM events"
            "  WHERE eventTypeId IS NOT NULL AND eventTypeId <> ''"
            "    AND message IS NOT NULL AND message <> ''"
            "  GROUP BY eventTypeId, message"
            ") GROUP BY eventTypeId"
        ).fetchall()
    }
