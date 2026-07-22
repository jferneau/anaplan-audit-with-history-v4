"""Tests for model-history change_type / object_type classification (v3.8 port).

Behavior contract from V4_HANDOFF_MH_CLASSIFICATION.md — locked; these tests
pin it. The classifier runs in v4 as DuckDB UDFs, but the logic under test is
the pure-Python :mod:`anaplan_audit.model_history.classification`.
"""

from __future__ import annotations

import pytest

from anaplan_audit.model_history import classification as c

_OBJ = {"Line Item/Property", "Module/List", "User", "Other"}
_CHG = {
    "Add Line Item",
    "Add List Item",
    "Add User",
    "Other",
    "Model change (no details available)",
}


def _rules(text: str) -> list[c.Rule]:
    """Load rules from inline CSV text against a permissive test vocabulary."""
    return c.load_rules(rules_text=text, object_types=_OBJ, change_types=_CHG)


# --------------------------------------------------------------------------- #
# Rule loading / validation
# --------------------------------------------------------------------------- #


def test_bundled_rules_load_and_end_with_catchall() -> None:
    rules = c.load_rules()
    assert len(rules) >= 20
    assert rules[-1].priority == 999
    assert rules[-1].pattern.pattern == ".*"
    assert (rules[-1].object_type, rules[-1].change_type) == (
        c.CATCHALL_OBJECT_TYPE,
        c.CATCHALL_CHANGE_TYPE,
    )


def test_rules_sorted_ascending_priority_stable() -> None:
    text = (
        "priority,pattern,object_type,change_type\n"
        "20,^b$,Other,Other\n"
        "10,^a1$,Other,Other\n"
        "10,^a2$,Other,Other\n"
    )
    rules = _rules(text)
    assert [r.priority for r in rules] == [10, 10, 20]
    # Ties preserve CSV source order.
    assert [r.pattern.pattern for r in rules[:2]] == ["^a1$", "^a2$"]


def test_every_bundled_rule_cites_known_vocabulary() -> None:
    objs = c._read_vocabulary("mh_object_types.csv")
    chgs = c._read_vocabulary("mh_change_types.csv")
    for rule in c.load_rules():
        assert rule.object_type in objs, rule
        assert rule.change_type in chgs, rule


def test_unknown_object_type_skipped_with_warning() -> None:
    text = (
        "priority,pattern,object_type,change_type\n"
        "10,^x$,Bogus Object,Other\n"
        "999,.*,Other,Other\n"
    )
    rules = _rules(text)
    assert all(r.object_type != "Bogus Object" for r in rules)
    assert len(rules) == 1  # only the catchall survived


def test_unknown_change_type_skipped() -> None:
    text = (
        "priority,pattern,object_type,change_type\n"
        "10,^x$,Other,Bogus Change\n"
        "999,.*,Other,Other\n"
    )
    assert len(_rules(text)) == 1


def test_invalid_regex_skipped() -> None:
    text = (
        "priority,pattern,object_type,change_type\n"
        "10,^([,Other,Other\n"
        "999,.*,Other,Other\n"
    )
    assert len(_rules(text)) == 1


def test_bad_priority_skipped() -> None:
    text = (
        "priority,pattern,object_type,change_type\n"
        "notanint,^x$,Other,Other\n"
        "999,.*,Other,Other\n"
    )
    assert len(_rules(text)) == 1


def test_empty_required_field_skipped() -> None:
    text = (
        "priority,pattern,object_type,change_type\n"
        "10,,Other,Other\n"  # empty pattern
        "999,.*,Other,Other\n"
    )
    assert len(_rules(text)) == 1


# --------------------------------------------------------------------------- #
# classify()
# --------------------------------------------------------------------------- #


def test_catchall_for_nonsense() -> None:
    assert c.classify("zzz nothing matches this zzz") == (
        c.CATCHALL_OBJECT_TYPE,
        c.CATCHALL_CHANGE_TYPE,
    )


@pytest.mark.parametrize("desc", ["", "   "])
def test_blank_description_is_catchall(desc: str) -> None:
    # Empty string short-circuits to catchall; whitespace-only has no rule and
    # falls through to the '.*' catchall.
    assert c.classify(desc) == (c.CATCHALL_OBJECT_TYPE, c.CATCHALL_CHANGE_TYPE)


def test_first_match_wins_lower_priority() -> None:
    # Intentional conflict: a general and a specific rule both match, specific
    # given the lower (earlier-firing) priority number.
    text = (
        "priority,pattern,object_type,change_type\n"
        "20,^Added list .+$,Module/List,Add List\n"
        "15,^Added list item .+$,Module/List,Add List Item\n"
        "999,.*,Other,Other\n"
    )
    rules = c.load_rules(
        rules_text=text,
        object_types=_OBJ | {"Module/List"},
        change_types=_CHG | {"Add List"},
    )
    assert c.classify("Added list item Foo to list Bar", rules) == ("Module/List", "Add List Item")
    assert c.classify("Added list Regions", rules) == ("Module/List", "Add List")


def test_exact_match_rules_are_disjoint() -> None:
    # Anaplan descriptions are a short controlled vocabulary (no variable tail),
    # so rules are exact ^X$ and can't overlap: "Add List" must NOT swallow
    # "Add List Item" the way a verbose ^Added list .+$ would have.
    assert c.classify("Add Item") == ("Module/List", "Add Item")
    assert c.classify("Add List") == ("Module/List", "Add List")
    assert c.classify("Add List Item") == ("Module/List", "Add List Item")


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        # change_type is the description itself (identity) — Anaplan's
        # description vocabulary IS the MH_CHANGE_TYPES list; only object_type
        # is a real derivation.
        ("Add Item", ("Module/List", "Add Item")),
        ("Add Line Item", ("Line Item/Property", "Add Line Item")),
        ("Change Line Item", ("Line Item/Property", "Change Line Item")),
        ("Delete Line Item", ("Line Item/Property", "Delete Line Item")),
        ("Add List", ("Module/List", "Add List")),
        ("Change List", ("Module/List", "Change List")),
        ("Add Module", ("Module/List", "Add Module")),
        ("Add User", ("User", "Add User")),
        ("Import", ("Other", "Import")),
        ("Add Import", ("Other", "Add Import")),
        ("Change Import", ("Other", "Change Import")),
        ("Add Import Data Source", ("Other", "Add Import Data Source")),
        ("Add Export", ("Export", "Add Export")),
        ("Code Changed", ("Line Item/Property", "Code Changed")),
        ("Name Changed", ("Other", "Name Changed")),
        ("Bulk data change", ("Line Item/Property", "Bulk data change")),
        ("Bulk data change (add-in)", ("Line Item/Property", "Bulk data change (add-in)")),
        ("User Role Changed", ("Role", "User Role Changed")),
        ("Add Role", ("Role", "Add Role")),
        ("Add Version", ("Version", "Add Version")),
        ("Change Time Scale", ("Time Settings", "Change Time Scale")),
        ("Rename Action", ("Action", "Rename Action")),
        ("5 Item(s) Added", ("Module/List", "x Item(s) Added")),
        ("12 User(s) Deleted", ("User", "x User(s) Deleted")),
        (
            "Breakback data change affecting 500 cells",
            ("Line Item/Property", "Breakback data change affecting [x] cells"),
        ),
        # Not in Jon's 82-member list — deliberately unruled so the failsafe
        # report surfaces it for a vocab decision.
        ("Add Action", ("Other", "Model change (no details available)")),
        ("Totally novel Anaplan event", ("Other", "Model change (no details available)")),
    ],
)
def test_real_vocabulary_rules(description: str, expected: tuple[str, str]) -> None:
    assert c.classify(description) == expected


# --------------------------------------------------------------------------- #
# Unmatched summary
# --------------------------------------------------------------------------- #


def test_summarize_unmatched_ranks_by_frequency() -> None:
    descriptions = (
        ["Foo happened"] * 5
        + ["Bar occurred"] * 2
        + ["Add User"] * 100  # matches a rule — not counted
        + [""] * 3  # blank — excluded from the report
        + ["Model change (no details available)"] * 4  # catchall label — excluded
    )
    summary = c.summarize_unmatched(descriptions)
    assert summary.total == 7
    assert summary.unique == 2
    assert summary.top[0] == ("Foo happened", 5)
    assert summary.top[1] == ("Bar occurred", 2)


def test_unmatched_counts_from_classified_frame() -> None:
    # unmatched_counts reads the already-classified change_type; only genuine
    # catchall rows (excluding blank + the catchall label) are reported.
    descriptions = ["Add Item", "Weird thing", "Weird thing", "", "Add Import"]
    change_types = [
        "Add List Item",
        c.CATCHALL_CHANGE_TYPE,
        c.CATCHALL_CHANGE_TYPE,
        c.CATCHALL_CHANGE_TYPE,  # blank row still classifies to catchall
        c.CATCHALL_CHANGE_TYPE,
    ]
    counts = c.unmatched_counts(descriptions, change_types)
    assert counts == {"Weird thing": 2, "Add Import": 1}
