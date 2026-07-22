"""Tests for backup_database and backup rotation in transform/loader.py."""

from __future__ import annotations

from pathlib import Path

from anaplan_audit.transform.loader import backup_database


class TestBackupDatabase:
    def test_returns_none_when_db_does_not_exist(self, tmp_path: Path) -> None:
        """backup_database must return None if the database file is absent."""
        result = backup_database(tmp_path / "nonexistent.db")
        assert result is None

    def test_backup_file_created(self, tmp_path: Path) -> None:
        """A backup file must be created alongside the original database."""
        db_path = tmp_path / "anaplan_audit.db"
        db_path.write_bytes(b"SQLite database content")

        backup_path = backup_database(db_path)

        assert backup_path is not None
        assert backup_path.exists()

    def test_backup_name_contains_timestamp(self, tmp_path: Path) -> None:
        """Backup filename must embed a timestamp in YYYYMMDD_HHMMSS format."""
        db_path = tmp_path / "anaplan_audit.db"
        db_path.write_bytes(b"SQLite database content")

        backup_path = backup_database(db_path)

        assert backup_path is not None
        assert "_backup_" in backup_path.name
        # Stem should look like anaplan_audit_backup_20260412_143000
        parts = backup_path.stem.split("_backup_")
        assert len(parts) == 2
        timestamp_part = parts[1]
        assert len(timestamp_part) == 15  # YYYYMMDD_HHMMSS

    def test_backup_is_adjacent_to_db(self, tmp_path: Path) -> None:
        """Backup file must live in the same directory as the source database."""
        db_path = tmp_path / "anaplan_audit.db"
        db_path.write_bytes(b"content")

        backup_path = backup_database(db_path)

        assert backup_path is not None
        assert backup_path.parent == db_path.parent

    def test_backup_content_matches_original(self, tmp_path: Path) -> None:
        """Backup file content must be identical to the source."""
        db_path = tmp_path / "anaplan_audit.db"
        db_path.write_bytes(b"important data")

        backup_path = backup_database(db_path)

        assert backup_path is not None
        assert backup_path.read_bytes() == b"important data"

    def test_original_db_unchanged_after_backup(self, tmp_path: Path) -> None:
        """The source database must not be modified by the backup operation."""
        db_path = tmp_path / "anaplan_audit.db"
        db_path.write_bytes(b"original content")

        backup_database(db_path)

        assert db_path.read_bytes() == b"original content"


class TestBackupRotation:
    def _create_backup(self, db_path: Path, index: int) -> Path:
        """Create a synthetic backup file with a fake mtime offset."""
        # Write a named backup file; touch mtime so rotation order is deterministic.
        backup = db_path.with_name(
            f"{db_path.stem}_backup_202601{index:02d}_120000{db_path.suffix}"
        )
        backup.write_bytes(b"backup content")
        # Set mtime to a deterministic value so sorting is stable.
        mtime = 1_000_000 + index * 60
        import os

        os.utime(str(backup), (mtime, mtime))
        return backup

    def test_old_backups_removed_when_over_limit(self, tmp_path: Path) -> None:
        """When there are more than max_backups backups, the oldest are deleted."""
        db_path = tmp_path / "anaplan_audit.db"
        db_path.write_bytes(b"db content")

        # Pre-create 7 existing backups.
        for i in range(1, 8):
            self._create_backup(db_path, i)

        # Call backup with max_backups=5 — should keep 5, delete 2 oldest.
        backup_database(db_path, max_backups=5)

        all_backups = sorted(
            tmp_path.glob(f"{db_path.stem}_backup_*{db_path.suffix}"),
            key=lambda p: p.stat().st_mtime,
        )
        assert len(all_backups) == 5

    def test_backups_within_limit_not_deleted(self, tmp_path: Path) -> None:
        """When backup count is within max_backups, nothing is deleted."""
        db_path = tmp_path / "anaplan_audit.db"
        db_path.write_bytes(b"db content")

        # Pre-create 3 existing backups.
        for i in range(1, 4):
            self._create_backup(db_path, i)

        backup_database(db_path, max_backups=7)

        all_backups = list(tmp_path.glob(f"{db_path.stem}_backup_*{db_path.suffix}"))
        # 3 pre-existing + 1 new = 4, all within the 7-backup limit.
        assert len(all_backups) == 4

    def test_max_backups_zero_disables_rotation(self, tmp_path: Path) -> None:
        """max_backups=0 must skip rotation entirely, keeping all backups."""
        db_path = tmp_path / "anaplan_audit.db"
        db_path.write_bytes(b"db content")

        for i in range(1, 11):
            self._create_backup(db_path, i)

        backup_database(db_path, max_backups=0)

        all_backups = list(tmp_path.glob(f"{db_path.stem}_backup_*{db_path.suffix}"))
        # All 10 pre-existing + 1 new = 11.
        assert len(all_backups) == 11
