"""Guardrails for workspace/model combo configuration.

* ``skip`` mode tolerates a workspace whose models can't be listed.
* ``skip`` entries match by ID or display name, like ``select`` entries.
* ``config_warnings`` flags valid-but-unintended setups.
"""

from __future__ import annotations

import httpx
import respx

from anaplan_audit.config import Settings, WorkspaceModelCombo, config_warnings
from anaplan_audit.orchestrator import _resolve_combos
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"


def _skip_settings(*combos: tuple[str, str]) -> Settings:
    return Settings(
        anaplanTenantName="T",
        uris={"integrationUri": BASE},  # type: ignore[arg-type]
        workspaceModelFilterApproach="skip",
        workspaceModelCombos=[WorkspaceModelCombo(workspaceId=w, modelId=m) for w, m in combos],
    )


def _mock_tenant() -> None:
    respx.get(f"{BASE}/workspaces").mock(
        return_value=httpx.Response(
            200,
            json={
                "workspaces": [
                    {"id": "ws-fin", "name": "Finance"},
                    {"id": "ws-locked", "name": "Locked"},
                ]
            },
        )
    )
    respx.get(f"{BASE}/workspaces/ws-fin/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"id": "m-rev", "name": "Revenue Model"},
                    {"id": "m-opex", "name": "Opex"},
                ]
            },
        )
    )
    respx.get(f"{BASE}/workspaces/ws-locked/models").mock(
        return_value=httpx.Response(403, json={"status": {"message": "Forbidden"}})
    )


def _resolve(settings: Settings) -> list[tuple[str, str]]:
    with make_client() as client:
        return [(c.workspaceId, c.modelId) for c in _resolve_combos(client, settings)]


class TestSkipMode:
    @respx.mock
    def test_unreadable_workspace_is_left_out_not_fatal(self) -> None:
        _mock_tenant()
        assert _resolve(_skip_settings()) == [("ws-fin", "m-rev"), ("ws-fin", "m-opex")]

    @respx.mock
    def test_skip_by_id(self) -> None:
        _mock_tenant()
        assert _resolve(_skip_settings(("ws-fin", "m-rev"))) == [("ws-fin", "m-opex")]

    @respx.mock
    def test_skip_by_name_case_insensitive(self) -> None:
        _mock_tenant()
        assert _resolve(_skip_settings(("finance", "REVENUE MODEL"))) == [("ws-fin", "m-opex")]

    @respx.mock
    def test_skip_mixing_id_and_name(self) -> None:
        _mock_tenant()
        assert _resolve(_skip_settings(("ws-fin", "Opex"))) == [("ws-fin", "m-rev")]

    @respx.mock
    def test_unmatched_skip_entry_skips_nothing(self) -> None:
        _mock_tenant()
        assert _resolve(_skip_settings(("Finance", "Nope"))) == [
            ("ws-fin", "m-rev"),
            ("ws-fin", "m-opex"),
        ]


class TestConfigWarnings:
    def test_select_with_no_combos_warns(self) -> None:
        found = config_warnings(Settings(anaplanTenantName="T"))
        assert len(found) == 1
        assert '"skip" with an empty list' in found[0]

    def test_select_with_combos_is_quiet(self) -> None:
        settings = Settings(
            anaplanTenantName="T",
            workspaceModelCombos=[WorkspaceModelCombo(workspaceId="w", modelId="m")],
        )
        assert config_warnings(settings) == []

    def test_skip_with_no_combos_is_quiet(self) -> None:
        settings = Settings(anaplanTenantName="T", workspaceModelFilterApproach="skip")
        assert config_warnings(settings) == []

    def test_blank_tenant_name_warns(self) -> None:
        settings = Settings(anaplanTenantName="  ", workspaceModelFilterApproach="skip")
        assert [w for w in config_warnings(settings) if "TENANT_NAME" in w]

    def test_blank_tenant_name_quiet_when_audit_disabled(self) -> None:
        settings = Settings(
            auditEnabled=False,
            workspaceModelFilterApproach="skip",
            modelHistory={  # type: ignore[arg-type]
                "enabled": True,
                "targetAnaplanModel": {"workspaceId": "w", "modelId": "m"},
            },
        )
        assert config_warnings(settings) == []
