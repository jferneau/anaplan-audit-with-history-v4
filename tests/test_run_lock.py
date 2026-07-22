"""Tests for the _RunLock process-level concurrency guard.

Platform-neutral: the "already locked" cases hold the lock with a second
_RunLock instance rather than raw fcntl calls, so the same suite passes
on Linux/macOS (fcntl.flock) and Windows (msvcrt.locking).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anaplan_audit.exceptions import RunLockError
from anaplan_audit.orchestrator import _RunLock


class TestRunLock:
    def test_lock_file_created_on_entry(self, tmp_path: Path) -> None:
        """The .lock file must exist while the context manager is active."""
        db_path = tmp_path / "anaplan_audit.db"
        lock_path = tmp_path / "anaplan_audit.lock"

        with _RunLock(db_path):
            assert lock_path.exists()

    def test_lock_released_on_clean_exit(self, tmp_path: Path) -> None:
        """After the context exits normally, the lock file can be re-acquired."""
        db_path = tmp_path / "anaplan_audit.db"

        with _RunLock(db_path):
            pass

        # If the lock was released we can acquire it again without error.
        with _RunLock(db_path):
            pass

    def test_lock_released_on_exception(self, tmp_path: Path) -> None:
        """Lock must be released even when the body raises."""
        db_path = tmp_path / "anaplan_audit.db"

        with pytest.raises(ValueError), _RunLock(db_path):
            raise ValueError("something went wrong")

        # Lock should be free now.
        with _RunLock(db_path):
            pass

    def test_run_lock_error_when_already_locked(self, tmp_path: Path) -> None:
        """Attempting to acquire a held lock raises RunLockError immediately."""
        db_path = tmp_path / "anaplan_audit.db"

        with _RunLock(db_path):
            with pytest.raises(RunLockError) as exc_info, _RunLock(db_path):
                pass
            assert exc_info.value.exit_code == 7
            assert "lock file" in str(exc_info.value).lower()

    def test_lock_path_adjacent_to_db(self, tmp_path: Path) -> None:
        """Lock file must be named <db_stem>.lock next to the database."""
        db_path = tmp_path / "my_data.db"
        expected_lock = tmp_path / "my_data.lock"

        with _RunLock(db_path):
            assert expected_lock.exists()

    def test_run_lock_error_context_contains_lock_path(self, tmp_path: Path) -> None:
        """RunLockError.context must include the lock_path key."""
        db_path = tmp_path / "anaplan_audit.db"

        with _RunLock(db_path):
            with pytest.raises(RunLockError) as exc_info, _RunLock(db_path):
                pass
            assert "lock_path" in exc_info.value.context
