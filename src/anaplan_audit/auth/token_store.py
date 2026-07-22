"""Encrypted OAuth token storage using Fernet (from the cryptography library).

Replaces v1's pycryptodome-based approach with a modern, actively maintained
primitive.  Tokens are stored in a local SQLite database, encrypted at rest.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet

# Version byte for future re-encryption migrations.
_TOKEN_VERSION = b"\x01"


class TokenStore:
    """Encrypted token store backed by a local SQLite file.

    A machine-local keyfile (generated on first use, ``0600`` permissions on
    POSIX) provides the Fernet encryption key.

    Args:
        db_path: Path to the SQLite database file.
        key_path: Path to the Fernet keyfile.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        key_path: Path | None = None,
    ) -> None:
        self._db_path = db_path or Path.home() / ".anaplan_audit" / "tokens.db"
        self._key_path = key_path or Path.home() / ".anaplan_audit" / "token.key"
        self._ensure_key()
        self._ensure_db()
        self._fernet = Fernet(self._key_path.read_bytes())

    def _ensure_key(self) -> None:
        """Generate a Fernet key if one does not exist."""
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._key_path.exists():
            key = Fernet.generate_key()
            self._key_path.write_bytes(key)
            if os.name != "nt":
                self._key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def _ensure_db(self) -> None:
        """Create the tokens table if it does not exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(self._db_path))) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tokens (client_id TEXT PRIMARY KEY, blob BLOB NOT NULL)"
            )
            conn.commit()

    def get(self, client_id: str) -> str | None:
        """Retrieve and decrypt a refresh token.

        Args:
            client_id: The OAuth client ID.

        Returns:
            The decrypted refresh token, or *None* if not found.
        """
        with closing(sqlite3.connect(str(self._db_path))) as conn:
            row = conn.execute(
                "SELECT blob FROM tokens WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        blob: bytes = row[0]
        # Strip version byte.
        return self._fernet.decrypt(blob[1:]).decode()

    def put(self, client_id: str, refresh_token: str) -> None:
        """Encrypt and store a refresh token.

        Args:
            client_id: The OAuth client ID.
            refresh_token: The plaintext refresh token to store.
        """
        encrypted = _TOKEN_VERSION + self._fernet.encrypt(refresh_token.encode())
        with closing(sqlite3.connect(str(self._db_path))) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tokens (client_id, blob) VALUES (?, ?)",
                (client_id, encrypted),
            )
            conn.commit()

    def delete(self, client_id: str) -> None:
        """Remove a stored token.

        Args:
            client_id: The OAuth client ID whose token should be deleted.
        """
        with closing(sqlite3.connect(str(self._db_path))) as conn:
            conn.execute("DELETE FROM tokens WHERE client_id = ?", (client_id,))
            conn.commit()
