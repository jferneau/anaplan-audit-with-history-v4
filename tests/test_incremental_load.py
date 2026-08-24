"""Incremental audit-fact load — upload only the delta, not full history.

Audit facts are immutable and keyed by ``AUDIT_ID``, so a row already in the
model never needs re-sending. ``_audit_delta`` diffs the transformed frame
against the model's ``AUDIT_ID`` list and returns only the new rows, degrading
to a full load whenever the delta can't be computed safely. These tests also
lock the pagination fix that keeps the existing-key set complete.
"""

from __future__ import annotations

import httpx
import pandas as pd
import respx
import structlog

from anaplan_audit.api.transactional import get_list_item_identifiers
from anaplan_audit.upload import _audit_delta
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
WS = "ws-1"
MODEL = "model-1"


def _log() -> object:
    return structlog.get_logger().bind(test=True)


def _settings(*, incremental: bool = True) -> object:
    from anaplan_audit.config import (
        AnaplanUris,
        Settings,
        TargetModelConfig,
        TargetModelObjects,
    )

    return Settings(
        anaplanTenantName="test",
        authenticationMode="basic",
        basic_username="u",
        basic_password="p",
        uris=AnaplanUris(integrationUri=BASE),
        targetAnaplanModel=TargetModelConfig(
            workspaceId=WS,
            modelId=MODEL,
            objects=TargetModelObjects(incrementalLoad=incremental),
        ),
    )


def _df(*audit_ids: str) -> pd.DataFrame:
    return pd.DataFrame({"AUDIT_ID": list(audit_ids), "message": [f"m-{a}" for a in audit_ids]})


def _mock_audit_list(existing: list[dict[str, str]]) -> None:
    respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
        return_value=httpx.Response(200, json={"lists": [{"id": "L1", "name": "AUDIT_ID"}]})
    )
    respx.get(
        f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
        params={"includeAll": "true"},
    ).mock(return_value=httpx.Response(200, json={"listItems": existing}))


class TestAuditDelta:
    def test_filters_rows_already_in_model(self) -> None:
        df = _df("e1", "e2", "e3", "e4")
        with respx.mock:
            # e1, e3 already loaded; e2, e4 are new.
            _mock_audit_list([{"id": "1", "code": "e1", "name": "e1"}, {"id": "3", "code": "e3"}])
            with make_client() as client:
                out = _audit_delta(client, _settings(), df, _log(), full=False)
        assert out["AUDIT_ID"].tolist() == ["e2", "e4"]

    def test_empty_model_loads_everything(self) -> None:
        with respx.mock:
            _mock_audit_list([])  # fresh model — nothing loaded yet
            with make_client() as client:
                out = _audit_delta(client, _settings(), _df("e1", "e2"), _log(), full=False)
        assert out["AUDIT_ID"].tolist() == ["e1", "e2"]

    def test_no_new_rows_returns_empty_frame_with_columns(self) -> None:
        with respx.mock:
            _mock_audit_list([{"id": "1", "code": "e1"}, {"id": "2", "code": "e2"}])
            with make_client() as client:
                out = _audit_delta(client, _settings(), _df("e1", "e2"), _log(), full=False)
        assert len(out) == 0
        assert list(out.columns) == ["AUDIT_ID", "message"]  # header survives for the CSV

    def test_full_flag_bypasses_delta(self) -> None:
        # No HTTP mock registered: a full load must not call the API at all.
        with respx.mock, make_client() as client:
            out = _audit_delta(client, _settings(), _df("e1", "e2"), _log(), full=True)
        assert out["AUDIT_ID"].tolist() == ["e1", "e2"]

    def test_incremental_disabled_bypasses_delta(self) -> None:
        with respx.mock, make_client() as client:
            out = _audit_delta(client, _settings(incremental=False), _df("e1"), _log(), full=False)
        assert out["AUDIT_ID"].tolist() == ["e1"]

    def test_missing_key_list_falls_back_to_full(self) -> None:
        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(200, json={"lists": [{"id": "X", "name": "OTHER"}]})
            )
            with make_client() as client:
                out = _audit_delta(client, _settings(), _df("e1", "e2"), _log(), full=False)
        assert out["AUDIT_ID"].tolist() == ["e1", "e2"]  # safe fallback: nothing dropped

    def test_fetch_error_falls_back_to_full(self) -> None:
        # 404 is non-retryable, so the client raises at once; _audit_delta must
        # catch it and load the full set rather than dropping rows.
        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(404)
            )
            with make_client() as client:
                out = _audit_delta(client, _settings(), _df("e1", "e2"), _log(), full=False)
        assert out["AUDIT_ID"].tolist() == ["e1", "e2"]


class TestPaginationCompleteness:
    def test_identifiers_span_every_page(self) -> None:
        # A partial set would make already-loaded facts look new. Follow the
        # cursor across pages so the union is complete.
        page2 = f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items?includeAll=true&cursor=2"
        with respx.mock:
            # One route, two sequential responses: page 1 hands back a nextUrl,
            # page 2 (same path) closes it out. (Distinct routes don't work here
            # because respx's subset param-matching lets page 1 also match the
            # page-2 request.)
            respx.get(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"includeAll": "true"},
            ).mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={
                            "listItems": [{"code": "e1"}, {"code": "e2"}],
                            "meta": {"paging": {"nextUrl": page2}},
                        },
                    ),
                    httpx.Response(200, json={"listItems": [{"code": "e3"}, {"code": "e4"}]}),
                ]
            )
            with make_client() as client:
                ids = get_list_item_identifiers(client, BASE, WS, MODEL, "L1")
        assert ids == {"e1", "e2", "e3", "e4"}
