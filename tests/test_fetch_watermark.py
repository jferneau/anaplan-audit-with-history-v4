"""Tests for the event-derived ``lastRun`` watermark.

``lastRun`` used to be stamped with ``time.time()`` after the upload, so any
event that occurred during the run (or surfaced late in the Audit API) fell
behind the watermark and was never fetched. It is now derived from the newest
event fetched, minus a re-fetch overlap.
"""

from __future__ import annotations

import pandas as pd
import pytest

from anaplan_audit.api.audit import FETCH_OVERLAP_SECONDS, next_fetch_watermark
from anaplan_audit.api.models import AuditEvent
from anaplan_audit.config import Settings
from tests.conftest import make_client

NEWEST_S = 1_792_085_697


def _events(*epoch_seconds: int) -> list[AuditEvent]:
    return [AuditEvent(id=str(i), eventDate=s * 1000) for i, s in enumerate(epoch_seconds)]


class TestNextFetchWatermark:
    def test_trails_newest_event_by_overlap(self) -> None:
        events = _events(NEWEST_S - 500, NEWEST_S, NEWEST_S - 90)
        assert next_fetch_watermark(0, events, capped=False) == NEWEST_S - FETCH_OVERLAP_SECONDS

    def test_never_moves_backwards(self) -> None:
        previous = NEWEST_S - 60
        assert next_fetch_watermark(previous, _events(NEWEST_S), capped=False) == previous

    def test_no_events_keeps_lastrun(self) -> None:
        assert next_fetch_watermark(123, [], capped=False) is None

    def test_capped_fetch_keeps_lastrun(self) -> None:
        assert next_fetch_watermark(0, _events(NEWEST_S), capped=True) is None

    def test_events_without_dates_keep_lastrun(self) -> None:
        assert next_fetch_watermark(0, [AuditEvent(id="1")], capped=False) is None


class TestUploadPersistsWatermark:
    def _run(self, monkeypatch: pytest.MonkeyPatch, watermark: int | None) -> list[int]:
        from anaplan_audit import upload as upload_mod

        persisted: list[int] = []
        monkeypatch.setattr(upload_mod, "list_files", lambda *a, **k: [])
        monkeypatch.setattr(upload_mod, "list_imports", lambda *a, **k: [])
        monkeypatch.setattr(upload_mod, "_audit_delta", lambda c, s, df, log, **k: df)
        monkeypatch.setattr(upload_mod, "_upload_single_file", lambda *a, **k: None)
        monkeypatch.setattr(upload_mod, "_upload_last_run_to_anaplan", lambda *a, **k: None)
        monkeypatch.setattr(upload_mod, "_write_refresh_log_transactional", lambda *a, **k: None)
        monkeypatch.setattr(upload_mod, "_sync_lists_transactional", lambda *a, **k: None)
        monkeypatch.setattr(upload_mod, "_update_last_run", lambda s, v: persisted.append(v))

        settings = Settings(anaplanTenantName="T")
        with make_client() as client:
            upload_mod.upload_audit_data(
                client, pd.DataFrame([{"AUDIT_ID": "1"}]), settings, watermark=watermark
            )
        return persisted

    def test_persists_event_watermark_not_wall_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._run(monkeypatch, watermark=NEWEST_S - FETCH_OVERLAP_SECONDS) == [
            NEWEST_S - FETCH_OVERLAP_SECONDS
        ]

    def test_no_watermark_leaves_lastrun_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._run(monkeypatch, watermark=None) == []
