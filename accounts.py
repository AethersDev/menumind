"""Sign-in by username and PIN. The server derives user_id from the session cookie; the client
never supplies it (SPEC §4: user_id is server-issued)."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

USERNAME = re.compile(r"^[a-z0-9_.-]{3,32}$")
PIN = re.compile(r"^\d{4,12}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    pin_salt   BLOB NOT NULL,
    pin_hash   BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);
"""


class AccountError(ValueError):
    pass


def _hash(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Accounts:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def register(self, username: str, pin: str) -> str:
        username = username.strip().lower()
        if not USERNAME.match(username):
            raise AccountError("Usernames are 3-32 characters: letters, numbers, dot, dash or underscore.")
        if not PIN.match(pin):
            raise AccountError("PINs are 4-12 digits.")
        salt = secrets.token_bytes(16)
        user_id = str(uuid.uuid4())
        with self._lock:
            try:
                self._db.execute("INSERT INTO users VALUES (?,?,?,?,?)",
                                 (user_id, username, salt, _hash(pin, salt), _now()))
            except sqlite3.IntegrityError:
                raise AccountError("That username is taken. Sign in instead, or pick another.") from None
        return user_id

    def login(self, username: str, pin: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT user_id, pin_salt, pin_hash FROM users WHERE username = ?",
                                   (username.strip().lower(),)).fetchone()
        if row is None:
            _hash(pin, b"0" * 16)  # same work either way, so timing doesn't reveal usernames
            return None
        user_id, salt, stored = row
        return user_id if hmac.compare_digest(_hash(pin, salt), stored) else None

    def start_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._db.execute("INSERT INTO sessions VALUES (?,?,?)", (token, user_id, _now()))
        return token

    def session_user(self, token: Optional[str]) -> Optional[tuple[str, str]]:
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT u.user_id, u.username FROM sessions s JOIN users u USING (user_id) "
                "WHERE s.token = ?", (token,)).fetchone()
        return (row[0], row[1]) if row else None

    def end_session(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._db.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def close(self) -> None:
        self._db.close()
