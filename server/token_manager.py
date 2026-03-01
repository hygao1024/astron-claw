import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "tokens.db"


class TokenManager:
    """SQLite-backed token management with sk- prefix and optional expiry."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH, expiry_seconds: int = 86400):
        self._db_path = db_path
        self._expiry_seconds = expiry_seconds
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS tokens ("
            "  token TEXT PRIMARY KEY,"
            "  created_at REAL NOT NULL"
            ")"
        )
        self._conn.commit()

    def generate(self) -> str:
        token = "sk-" + secrets.token_hex(24)
        self._conn.execute(
            "INSERT INTO tokens (token, created_at) VALUES (?, ?)",
            (token, time.time()),
        )
        self._conn.commit()
        return token

    def validate(self, token: Optional[str]) -> bool:
        if not token:
            return False
        row = self._conn.execute(
            "SELECT created_at FROM tokens WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return False
        if time.time() - row[0] > self._expiry_seconds:
            self._conn.execute("DELETE FROM tokens WHERE token = ?", (token,))
            self._conn.commit()
            return False
        return True

    def remove(self, token: str) -> None:
        self._conn.execute("DELETE FROM tokens WHERE token = ?", (token,))
        self._conn.commit()

    def cleanup_expired(self) -> int:
        """Remove all expired tokens. Returns count of deleted rows."""
        cutoff = time.time() - self._expiry_seconds
        cur = self._conn.execute(
            "DELETE FROM tokens WHERE created_at < ?", (cutoff,)
        )
        self._conn.commit()
        return cur.rowcount
