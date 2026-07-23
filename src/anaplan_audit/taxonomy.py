"""Single source of truth for EVENT_ID list parentage.

Every Anaplan audit event carries an event-type code (``USR-8``, ``AUTHZ-11``,
``WF-112`` …). In the reporting model these become members of a hierarchical
``EVENT_ID`` list: ``All Events → CATEGORY → event code``. The category is
derived deterministically from the code's **prefix** — the segment before the
first hyphen — so any code, whether it's in the shipped catalog or brand new
from the live audit stream, always resolves to a parent and never lands
orphaned under ``All Events``.

Two-tier by design: each event code nests **directly** under its category. The
finer sub-groupings some models use (e.g. ``GUARDIAN ENDPOINT`` / ``HOST``
under ``ENCRYPTION ACTIVITY``) are intentionally flattened here — the tool owns
one flat prefix→category map, and the model can regroup downstream if it wants.

The parent *names* match the reporting model's category members exactly, so the
``EVENT_ID`` import binds by code without spawning duplicate parents. This
module is the ONLY place the taxonomy is defined: :func:`register_udfs`
projects it into SQL (the audit query's ``EVENT_CATEGORY`` and the activity-code
catalog augmentation) so the derivation is never duplicated.
"""

from __future__ import annotations

import duckdb

# Prefix (segment before the first hyphen, upper-cased) → (parent_code,
# parent_name). Keep the names aligned with the reporting model's EVENT_ID
# category members. Prefixes are mutually distinct — no prefix is a prefix of
# another — so a first-segment lookup is unambiguous.
_PREFIX_TO_CATEGORY: dict[str, tuple[str, str]] = {
    "USR": ("USR", "USER ACTIVITY"),
    "AUTHZ": ("AUTHZ", "ACCESS CONTROL"),
    "CONN": ("CONN", "SAML CONNECTION"),
    "INT": ("INT", "INTEGRATION"),
    "FRCST": ("FRCST", "FORECASTER"),
    "PIQ": ("PIQ", "PLANIQ"),
    "WF": ("WF", "WORKFLOW"),
    "DSM": ("DSM", "ENCRYPTION ACTIVITY"),
    "OAUTH": ("OAUTH", "OAUTH"),
    "COMMENT": ("COMMENT", "COMMENT"),
}

# Catch-all for an unknown prefix. Guarantees a code is never orphaned even if
# Anaplan introduces a new event family the map hasn't caught up with yet.
UNCATEGORIZED: tuple[str, str] = ("UNCAT", "UNCATEGORIZED")

# The category tier, in the order the model lists them. Uploaded/imported as
# ``EVENT_CATEGORIES.csv`` to seed the parents under ``All Events`` BEFORE
# ``ACTIVITY_CODES.csv`` imports the leaf event codes. (code, name)
CATEGORIES: list[tuple[str, str]] = [
    _PREFIX_TO_CATEGORY["AUTHZ"],
    _PREFIX_TO_CATEGORY["CONN"],
    _PREFIX_TO_CATEGORY["DSM"],
    _PREFIX_TO_CATEGORY["INT"],
    _PREFIX_TO_CATEGORY["PIQ"],
    _PREFIX_TO_CATEGORY["FRCST"],
    _PREFIX_TO_CATEGORY["USR"],
    _PREFIX_TO_CATEGORY["WF"],
    _PREFIX_TO_CATEGORY["OAUTH"],
    _PREFIX_TO_CATEGORY["COMMENT"],
    UNCATEGORIZED,
]


def category_for_code(code: str | None) -> tuple[str, str]:
    """Return ``(parent_code, parent_name)`` for an event-type code.

    The lookup is on the code's first hyphen-delimited segment, upper-cased —
    so ``DSM-DAO0071I`` and ``DSM-072`` both resolve to ``ENCRYPTION ACTIVITY``.
    A blank code, or one whose prefix isn't mapped, returns
    :data:`UNCATEGORIZED`.
    """
    if not code:
        return UNCATEGORIZED
    prefix = str(code).split("-", 1)[0].strip().upper()
    return _PREFIX_TO_CATEGORY.get(prefix, UNCATEGORIZED)


def event_parent_code(code: str | None) -> str:
    """Parent *code* for an event-type code (SQL UDF target)."""
    return category_for_code(code)[0]


def event_parent_name(code: str | None) -> str:
    """Parent *name* for an event-type code (SQL UDF target)."""
    return category_for_code(code)[1]


def register_udfs(conn: duckdb.DuckDBPyConnection) -> None:
    """Register ``event_parent_code`` / ``event_parent_name`` on *conn*.

    Lets SQL derive the category from the same Python map used everywhere else,
    so the taxonomy is never re-encoded in a ``CASE`` statement. String type
    names (``"VARCHAR"``) because this DuckDB build has no ``duckdb.typing``
    module but accepts the string form. Callers pass a coalesced (never-NULL)
    code — DuckDB skips a Python UDF on NULL input, and an empty string already
    resolves to the catch-all, so no event can dodge a category.
    """
    conn.create_function("event_parent_code", event_parent_code, ["VARCHAR"], "VARCHAR")
    conn.create_function("event_parent_name", event_parent_name, ["VARCHAR"], "VARCHAR")
