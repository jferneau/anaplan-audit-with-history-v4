"""Derive controlled-vocabulary ``change_type`` / ``object_type`` from the
free-text model-history ``description`` column.

Ported from v3.8 (scope: ``MODEL_HISTORY_CLASSIFICATION_SCOPE.md`` §6, handoff:
``V4_HANDOFF_MH_CLASSIFICATION.md``). Behavior is locked — do not redesign.

Contract
~~~~~~~~
* First-match-wins over rules ordered by ascending ``priority`` (stable — ties
  keep CSV source order).
* An explicit catchall (``priority=999, pattern='.*'``) guarantees every row
  classifies; a blank or unmatched description → ``("Other", "Model change
  (no details available)")``. Never NULL, never empty, never crash.
* Rules referencing an unknown vocabulary term, an un-compilable regex, an
  empty required field, or a bad ``priority`` are logged at WARNING and
  skipped — a malformed rules file never crashes the pipeline.

v4 note: classification runs as two DuckDB scalar UDFs registered on the
normalize connection (:func:`classify_change_type` / :func:`classify_object_type`),
projected as the last two columns. Results are memoized — a model-history
export has very few distinct descriptions, so the regex work runs once per
distinct string regardless of row count.
"""

from __future__ import annotations

import csv
import importlib.resources
import io
import re
import threading
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache

import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

_DATA_PKG = "anaplan_audit.model_history.data"

# (object_type, change_type) returned when a description is blank or no rule
# matches. Matches the terminal catchall rule; kept as an explicit constant so
# the contract holds even if a malformed rules file drops the catchall row.
CATCHALL_OBJECT_TYPE = "Other"
CATCHALL_CHANGE_TYPE = "Model change (no details available)"
_CATCHALL = (CATCHALL_OBJECT_TYPE, CATCHALL_CHANGE_TYPE)

# Descriptions that land on the catchall *legitimately* and are NOT rule gaps:
# an empty description, and a row whose description literally is the catchall
# label. These are excluded from the unmatched report so it stays a clean
# "these descriptions need a rule" working set.
_REPORT_EXCLUDED: frozenset[str] = frozenset({"", CATCHALL_CHANGE_TYPE})


@dataclass(frozen=True)
class Rule:
    """One compiled classification rule."""

    priority: int
    pattern: re.Pattern[str]
    object_type: str
    change_type: str


@dataclass(frozen=True)
class UnmatchedSummary:
    """End-of-run report of descriptions that fell through to the catchall."""

    total: int
    unique: int
    top: list[tuple[str, int]]  # (description, count), highest first


def _read_vocabulary(filename: str) -> set[str]:
    """Read a one-column vocabulary CSV (header + members) into a set."""
    text = importlib.resources.files(_DATA_PKG).joinpath(filename).read_text()
    rows = list(csv.reader(io.StringIO(text)))
    return {r[0].strip() for r in rows[1:] if r and r[0].strip()}


def load_rules(
    *,
    rules_text: str | None = None,
    object_types: set[str] | None = None,
    change_types: set[str] | None = None,
) -> list[Rule]:
    """Load, validate, and compile the classification rules.

    Args:
        rules_text: Raw rules CSV. Defaults to the bundled
            ``mh_classification_rules.csv``. (Injectable for tests.)
        object_types / change_types: Valid vocabularies. Default to the
            bundled vocab CSVs. (Injectable for tests.)

    Returns:
        Compiled rules sorted by ascending ``priority`` (stable). Invalid
        rows are logged and skipped, never raised.
    """
    if object_types is None:
        object_types = _read_vocabulary("mh_object_types.csv")
    if change_types is None:
        change_types = _read_vocabulary("mh_change_types.csv")
    if rules_text is None:
        rules_text = (
            importlib.resources.files(_DATA_PKG).joinpath("mh_classification_rules.csv").read_text()
        )

    rules: list[Rule] = []
    for i, row in enumerate(csv.DictReader(io.StringIO(rules_text))):
        pattern = (row.get("pattern") or "").strip()
        object_type = (row.get("object_type") or "").strip()
        change_type = (row.get("change_type") or "").strip()
        priority_raw = (row.get("priority") or "").strip()

        if not pattern or not object_type or not change_type:
            logger.warning("mh_rule_skipped_empty_field", row_index=i, row=row)
            continue
        try:
            priority = int(priority_raw)
        except ValueError:
            logger.warning("mh_rule_skipped_bad_priority", row_index=i, priority=priority_raw)
            continue
        if object_type not in object_types:
            logger.warning(
                "mh_rule_skipped_unknown_object_type", row_index=i, object_type=object_type
            )
            continue
        if change_type not in change_types:
            logger.warning(
                "mh_rule_skipped_unknown_change_type", row_index=i, change_type=change_type
            )
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            logger.warning(
                "mh_rule_skipped_bad_regex", row_index=i, pattern=pattern, error=str(exc)
            )
            continue
        rules.append(Rule(priority, compiled, object_type, change_type))

    # Stable sort: Python's sort preserves input order within equal keys, so
    # ties keep their CSV source order (contract §2).
    rules.sort(key=lambda r: r.priority)
    return rules


# --- Process-wide singleton -------------------------------------------------
_RULES: list[Rule] | None = None
_RULES_LOCK = threading.Lock()


def get_rules() -> list[Rule]:
    """Return the cached bundled rules, loading them on first call.

    Thread-safe: the model-history pipeline classifies from several worker
    threads concurrently (one in-memory DuckDB per model).
    """
    global _RULES
    if _RULES is None:
        with _RULES_LOCK:
            if _RULES is None:
                _RULES = load_rules()
    return _RULES


def reset_cache() -> None:
    """Clear the rules singleton and memoized results (tests only)."""
    global _RULES
    with _RULES_LOCK:
        _RULES = None
    _classify_cached.cache_clear()


def classify(description: str, rules: list[Rule] | None = None) -> tuple[str, str]:
    """Return ``(object_type, change_type)`` for a raw description.

    First-match-wins; blank or unmatched → the catchall pair.
    """
    if rules is None:
        rules = get_rules()
    if description:
        for rule in rules:
            if rule.pattern.fullmatch(description):
                return (rule.object_type, rule.change_type)
    return _CATCHALL


@cache
def _classify_cached(description: str) -> tuple[str, str]:
    """Memoized classify against the bundled singleton (UDF fast path)."""
    return classify(description)


def classify_object_type(description: str | None) -> str:
    """DuckDB UDF: raw description → object_type text."""
    return _classify_cached(description or "")[0]


def classify_change_type(description: str | None) -> str:
    """DuckDB UDF: raw description → change_type text."""
    return _classify_cached(description or "")[1]


def summarize_unmatched(
    descriptions: Iterable[str],
    rules: list[Rule] | None = None,
    *,
    top_n: int = 10,
) -> UnmatchedSummary:
    """Rank the descriptions that hit the catchall, for rule authoring.

    Args:
        descriptions: Raw descriptions from this run.
        rules: Rules to classify against (defaults to the bundled set).
        top_n: How many of the most frequent catchall descriptions to keep.
    """
    counter: Counter[str] = Counter()
    for description in descriptions:
        if description in _REPORT_EXCLUDED:
            continue
        if classify(description, rules) == _CATCHALL:
            counter[description] += 1
    return UnmatchedSummary(
        total=sum(counter.values()),
        unique=len(counter),
        top=counter.most_common(top_n),
    )


def unmatched_counts(descriptions: Iterable[str], change_types: Iterable[str]) -> dict[str, int]:
    """Count descriptions that fell through to the catchall, for the failsafe report.

    Takes the already-classified ``change_type`` alongside ``description`` (from
    a normalized frame) so it never re-runs the classifier. A row counts as
    unmatched when its ``change_type`` is the catchall and the description isn't
    an expected-catchall value (blank / the catchall label itself).

    Returns ``{description: occurrences}``.
    """
    counter: Counter[str] = Counter()
    for description, change_type in zip(descriptions, change_types, strict=True):
        if change_type == CATCHALL_CHANGE_TYPE and description not in _REPORT_EXCLUDED:
            counter[description] += 1
    return dict(counter)
