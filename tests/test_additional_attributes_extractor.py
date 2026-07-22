"""Milestone 1 tests — additionalAttributes extractor.

Covers every case listed in the spec's Acceptance criteria #1:

* happy path (dict already parsed)
* missing sub-fields
* malformed JSON string
* unicode values
* spaces in values ("Xperience 2025")
* pipe chars ("13 | G&A Expenses")
* empty object
* absent additionalAttributes key

Plus category-gating and raw-retention toggles from Milestone 5, and
the enrich_event_dicts convenience for the ETL boundary.
"""

from __future__ import annotations

import json

from anaplan_audit.transform.additional_attributes import (
    ADDITIONAL_ATTRIBUTES_COLUMNS,
    CATEGORY_TO_COLUMNS,
    enrich_event_dicts,
    extract_from_dict,
    parse_additional_attributes,
)


class TestParseAdditionalAttributes:
    def test_dict_input_passes_through(self) -> None:
        payload = {"appId": "abc", "pageId": "def"}
        assert parse_additional_attributes(payload) is payload

    def test_json_string_input_is_decoded(self) -> None:
        # Belt-and-suspenders for the "Anaplan sometimes returns
        # additionalAttributes as a stringified blob" case the spec's
        # brace-depth guidance targets.
        payload = '{"appId": "abc", "appName": "Xperience 2025"}'
        parsed = parse_additional_attributes(payload)
        assert parsed == {"appId": "abc", "appName": "Xperience 2025"}

    def test_malformed_json_returns_none_and_does_not_raise(self) -> None:
        # Bare identifier is a real Anaplan value for FRCST-* events;
        # a truncated blob likewise. Both must degrade gracefully.
        assert parse_additional_attributes("not-json") is None
        assert parse_additional_attributes('{"appId":') is None
        assert parse_additional_attributes("") is None
        assert parse_additional_attributes("   ") is None

    def test_none_input_returns_none(self) -> None:
        assert parse_additional_attributes(None) is None

    def test_non_dict_json_returns_none(self) -> None:
        # JSON that decodes to a list / scalar is not a valid attributes
        # payload; treat as absent rather than mis-classify.
        assert parse_additional_attributes("[1, 2, 3]") is None
        assert parse_additional_attributes('"just-a-string"') is None
        assert parse_additional_attributes("42") is None


class TestExtractFromDict:
    def test_happy_path_projects_every_known_field(self) -> None:
        attrs = {
            "appId": "app-uuid",
            "appName": "Xperience 2025",
            "pageId": "page-uuid",
            "pageName": "13 | G&A Expenses",
            "integrationId": "int-1",
            "integrationName": "Salesforce ↔ Anaplan",
            "integrationFlowId": "flow-9",
            "actionId": "act-1",
            "actionName": "Load Plan Data",
            "actionType": "Import",
            "processId": "proc-1",
            "processName": "Nightly Refresh",
            "roleId": "role-1",
            "roleName": "Model Builder",
            "targetUserId": "usr-99",
            "targetUserName": "quin.eddy@anaplan.com",
        }
        result = extract_from_dict(attrs)
        assert result["app_id"] == "app-uuid"
        assert result["app_name"] == "Xperience 2025"
        assert result["page_name"] == "13 | G&A Expenses"
        assert result["integration_name"] == "Salesforce ↔ Anaplan"
        assert result["target_user_name"] == "quin.eddy@anaplan.com"
        # Every declared column is present in the return, even ones that
        # would be null (there are none in this test but the shape must
        # be uniform).
        assert set(result.keys()) == set(ADDITIONAL_ATTRIBUTES_COLUMNS)

    def test_missing_sub_fields_yield_none_not_missing_keys(self) -> None:
        # Uniform column shape: every declared column present, missing
        # ones as None so downstream Pandas operations don't KeyError.
        result = extract_from_dict({"appId": "x"})
        assert result["app_id"] == "x"
        assert result["app_name"] is None
        assert result["integration_id"] is None
        assert set(result.keys()) == set(ADDITIONAL_ATTRIBUTES_COLUMNS)

    def test_empty_dict_returns_all_nones(self) -> None:
        result = extract_from_dict({})
        assert all(v is None for v in result.values())
        assert set(result.keys()) == set(ADDITIONAL_ATTRIBUTES_COLUMNS)

    def test_none_input_returns_all_nones(self) -> None:
        # Non-UX events, or events pre-dating the additionalAttributes
        # rollout, arrive with None here.
        result = extract_from_dict(None)
        assert all(v is None for v in result.values())

    def test_unicode_preserved_in_extraction(self) -> None:
        result = extract_from_dict({"pageName": "计划 · Q4"})
        assert result["page_name"] == "计划 · Q4"

    def test_spaces_and_pipes_preserved(self) -> None:
        # The two exact strings the spec calls out as the CEF parser
        # failure mode. We verify they survive unchanged.
        result = extract_from_dict(
            {
                "appName": "Xperience 2025",
                "pageName": "13 | G&A Expenses",
            }
        )
        assert result["app_name"] == "Xperience 2025"
        assert result["page_name"] == "13 | G&A Expenses"

    def test_non_string_scalar_coerced_to_string(self) -> None:
        # Anaplan sometimes emits numeric IDs; SQLite columns are TEXT.
        result = extract_from_dict({"appId": 12345, "actionType": True})
        assert result["app_id"] == "12345"
        assert result["action_type"] == "True"

    def test_nested_value_json_serialized(self) -> None:
        # A dict/list nested inside additionalAttributes shouldn't crash
        # the SQLite binder; it lands as JSON text.
        result = extract_from_dict({"actionName": {"unexpected": ["shape"]}})
        # Must be valid JSON that round-trips.
        assert json.loads(result["action_name"]) == {"unexpected": ["shape"]}

    def test_raw_archive_populated_when_retain_true(self) -> None:
        attrs = {"appId": "x", "pageName": "P"}
        result = extract_from_dict(attrs, retain_raw=True)
        assert result["additional_attributes_raw"] is not None
        # Round-trip preserves the input dict.
        assert json.loads(result["additional_attributes_raw"]) == attrs

    def test_raw_archive_stable_across_key_order(self) -> None:
        # sort_keys ensures backfill re-runs produce byte-identical
        # archive strings — important for the "no-op if raw already set"
        # branch and for downstream diffing.
        a = extract_from_dict({"appId": "x", "pageId": "p"})
        b = extract_from_dict({"pageId": "p", "appId": "x"})
        assert a["additional_attributes_raw"] == b["additional_attributes_raw"]

    def test_raw_archive_suppressed_when_retain_false(self) -> None:
        result = extract_from_dict({"appId": "x"}, retain_raw=False)
        assert result["additional_attributes_raw"] is None
        # Extraction still happens.
        assert result["app_id"] == "x"

    def test_disabled_category_zeroes_its_columns_only(self) -> None:
        # When uxAppPage is disabled, app_* / page_* stay None even
        # though the source dict has values. Other categories unaffected.
        attrs = {
            "appId": "app-x",
            "pageName": "Page X",
            "actionId": "act-1",
            "actionName": "Load",
        }
        result = extract_from_dict(
            attrs,
            enabled_categories={"action"},  # only 'action' enabled
        )
        assert result["app_id"] is None
        assert result["page_name"] is None
        assert result["action_id"] == "act-1"
        assert result["action_name"] == "Load"

    def test_category_to_columns_map_covers_every_extracted_column(self) -> None:
        # Contract: every non-raw column belongs to exactly one category.
        # Guards against a future extraction being added without updating
        # the category gating map (would make it uncontrollable via
        # settings.json).
        owned = set()
        for cols in CATEGORY_TO_COLUMNS.values():
            owned.update(cols)
        extractable = set(ADDITIONAL_ATTRIBUTES_COLUMNS) - {"additional_attributes_raw"}
        assert owned == extractable


class TestEnrichEventDicts:
    def test_adds_extracted_keys_in_place(self) -> None:
        events = [
            {
                "id": "1",
                "eventTypeId": "USR-1",
                "additionalAttributes": {"appId": "a", "appName": "N"},
            },
            {
                "id": "2",
                "eventTypeId": "AUTHZ-1",
                "additionalAttributes": None,
            },
        ]
        result = enrich_event_dicts(events)
        assert result is events  # mutated in place, returned for fluency
        assert events[0]["app_id"] == "a"
        assert events[0]["app_name"] == "N"
        # Second event still has every column key present with None.
        assert events[1]["app_id"] is None
        assert events[1]["additional_attributes_raw"] is None

    def test_handles_json_string_additional_attributes(self) -> None:
        # Belt-and-suspenders for the API-returns-string variant.
        events = [
            {
                "id": "1",
                "additionalAttributes": '{"appId":"x","appName":"Xperience 2025"}',
            }
        ]
        enrich_event_dicts(events)
        assert events[0]["app_id"] == "x"
        assert events[0]["app_name"] == "Xperience 2025"

    def test_absent_additional_attributes_key_treated_as_none(self) -> None:
        events = [{"id": "1", "eventTypeId": "USR-1"}]
        enrich_event_dicts(events)
        assert events[0]["app_id"] is None
        assert events[0]["additional_attributes_raw"] is None

    def test_disabled_categories_produce_all_null_extractions(self) -> None:
        events = [
            {"id": "1", "additionalAttributes": {"appId": "x"}},
        ]
        enrich_event_dicts(events, enabled_categories=set())
        assert events[0]["app_id"] is None
        # Raw archive still respects retain_raw independently.
        assert events[0]["additional_attributes_raw"] is not None

    def test_retain_raw_false_still_extracts_named_columns(self) -> None:
        events = [
            {"id": "1", "additionalAttributes": {"appId": "x"}},
        ]
        enrich_event_dicts(events, retain_raw=False)
        assert events[0]["app_id"] == "x"
        assert events[0]["additional_attributes_raw"] is None

    def test_empty_list_produces_no_error(self) -> None:
        # Zero-event batches happen on quiet windows; the enrich pass
        # must remain a no-op that still emits its summary log.
        result = enrich_event_dicts([])
        assert result == []
