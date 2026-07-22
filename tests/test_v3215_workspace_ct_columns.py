"""Tests for v3.2.15 — WORKSPACE_LIST.csv (and the other CT lists) now
carry the columns the reporting model's property-based imports expect.

Colleague's WORKSPACE_LIST.csv was missing the ``WS_CT`` counter column
plus the ``sizeAllowance`` / ``currentSize`` fields that Anaplan only
returns when you pass ``?tenantDetails=true``. The property-based
``Import into WS_CT`` couldn't proceed without WS_CT, and even after the
mapping was fixed the file lacked the size columns the module needed.

Also covers the bool -> int coercion Anaplan's Boolean line items want.
"""

from __future__ import annotations

import httpx
import pandas as pd
import respx

from anaplan_audit.api.integration import list_workspaces
from anaplan_audit.api.models import Workspace
from anaplan_audit.upload import _TABLE_TO_COUNTER_COLUMN, _prepare_metadata_csv
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"


class TestWorkspaceTenantDetails:
    def test_list_workspaces_sends_tenant_details_flag(self) -> None:
        with respx.mock:
            route = respx.get(f"{BASE}/workspaces", params={"tenantDetails": "true"}).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "workspaces": [
                            {
                                "id": "8a868cd9",
                                "name": "Workspace #1",
                                "active": True,
                                "sizeAllowance": 42949672960,
                                "currentSize": 217689238,
                            }
                        ]
                    },
                )
            )
            with make_client() as client:
                workspaces = list_workspaces(client, BASE)
        assert route.called
        assert workspaces[0].sizeAllowance == 42949672960
        assert workspaces[0].currentSize == 217689238

    def test_workspace_model_defaults_size_fields_to_zero(self) -> None:
        # Backward-compat with legacy fixtures / tenants that don't
        # populate the tenantDetails-only fields.
        ws = Workspace.model_validate({"id": "x", "name": "y"})
        assert ws.sizeAllowance == 0
        assert ws.currentSize == 0


class TestPrepareMetadataCsv:
    def test_workspace_csv_gets_ws_ct_counter_and_int_active(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "id": "8a868cd9",
                    "name": "Workspace #1",
                    "active": True,
                    "sizeAllowance": 42949672960,
                    "currentSize": 217689238,
                },
                {
                    "id": "aabbcc",
                    "name": "Workspace #2",
                    "active": False,
                    "sizeAllowance": 10,
                    "currentSize": 5,
                },
            ]
        )
        result = _prepare_metadata_csv("workspaces", df)
        assert next(iter(result.columns)) == "WS_CT"
        assert result["WS_CT"].tolist() == [1, 2]
        # Boolean coerced to Anaplan-friendly int form.
        assert result["active"].tolist() == [1, 0]
        # sizeAllowance and currentSize pass through untouched.
        assert result["sizeAllowance"].tolist() == [42949672960, 10]
        assert result["currentSize"].tolist() == [217689238, 5]
        assert list(result.columns) == [
            "WS_CT",
            "id",
            "name",
            "active",
            "sizeAllowance",
            "currentSize",
        ]

    def test_ct_counter_column_added_for_every_ct_table(self) -> None:
        # Each mapped table gets its counter prepended with 1-based rows.
        # v3.5.0 added ``files`` -> ``FILE_CT`` alongside the original five.
        expected = {
            "workspaces": "WS_CT",
            "users": "USR_CT",
            "models": "MOD_CT",
            "actions": "ACT_CT",
            "files": "FILE_CT",
            "cloudworks": "CW_CT",
        }
        assert expected == _TABLE_TO_COUNTER_COLUMN

        for table, counter in expected.items():
            df = pd.DataFrame({"id": ["a", "b", "c"], "name": ["A", "B", "C"]})
            result = _prepare_metadata_csv(table, df)
            assert next(iter(result.columns)) == counter
            assert result[counter].tolist() == [1, 2, 3]

    def test_users_uses_v1_short_form_counter_usr_ct(self) -> None:
        # v3.2.16 regression: previous release named this USER_CT, which
        # didn't match the OEG v1 reporting model's expected key column
        # and failed the property-based ``Import into USR_CT``.
        df = pd.DataFrame(
            [
                {
                    "id": "8a81b09e51984e9f015230df09xxxxxx",
                    "userName": "Quin.Eddy@anaplan.com",
                    "displayName": "Quin Eddy",
                }
            ]
        )
        result = _prepare_metadata_csv("users", df)
        assert list(result.columns) == ["USR_CT", "id", "userName", "displayName"]
        assert result["USR_CT"].tolist() == [1]

    def test_activity_codes_gets_no_counter_prepended(self) -> None:
        # act_codes carries its own natural key — no counter needed.
        df = pd.DataFrame({"code": ["USR001", "USR002"], "name": ["A", "B"]})
        result = _prepare_metadata_csv("act_codes", df)
        assert list(result.columns) == ["code", "name"]

    def test_empty_dataframe_stays_empty_but_gets_counter(self) -> None:
        # Empty metadata still gets the counter column so the reporting
        # model's import doesn't see missing columns on a fresh tenant.
        df = pd.DataFrame(columns=["id", "name"])
        result = _prepare_metadata_csv("workspaces", df)
        assert list(result.columns) == ["WS_CT", "id", "name"]
        assert len(result) == 0

    def test_original_dataframe_not_mutated(self) -> None:
        df = pd.DataFrame({"id": ["a"], "active": [True]})
        original_cols = list(df.columns)
        _prepare_metadata_csv("workspaces", df)
        assert list(df.columns) == original_cols
        # Original boolean untouched.
        assert df["active"].dtype == bool
