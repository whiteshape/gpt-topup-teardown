"""Users, password hashing and bearer tokens.

Kept dependency-free: PBKDF2-HMAC-SHA256 from hashlib, random tokens from
secrets. Every token is stored hashed, so a database leak does not hand out
sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional, Tuple

from .store import Store

PBKDF2_ROUNDS = 210_000
TOKEN_TTL = 7 * 24 * 3600


def hash_password(password: str, salt: Optional[bytes] = None) -> Tuple[str, str]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return digest.hex(), salt.hex()


def verify_password(password: str, digest_hex: str, salt_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register(store: Store, email: str, password: str, invite_code: str = "") -> int:
    email = email.strip().lower()
    if store.one("SELECT id FROM users WHERE email=?", (email,)):
        raise ValueError("email_taken")
    digest, salt = hash_password(password)
    uid = store.ex(
        "INSERT INTO users(email,password_hash,created_at,invite_code) VALUES(?,?,?,?)",
        (email, f"pbkdf2${PBKDF2_ROUNDS}${salt}${digest}", int(time.time()),
         invite_code.strip().upper() or secrets.token_hex(4).upper()),
    )
    return uid


def authenticate(store: Store, email: str, password: str) -> Optional[int]:
    row = store.one("SELECT id, password_hash FROM users WHERE email=?", (email.strip().lower(),))
    if not row:
        return None
    parts = (row["password_hash"] or "").split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2":
        return None
    if not verify_password(password, parts[3], parts[2]):
        return None
    return int(row["id"])


def issue_token(store: Store, user_id: int, ttl: int = TOKEN_TTL) -> str:
    token = "zv_" + secrets.token_urlsafe(32)
    store.ex(
        "INSERT INTO tokens(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        (_token_hash(token), user_id, int(time.time()), int(time.time()) + ttl),
    )
    return token


def resolve_token(store: Store, token: str) -> Optional[int]:
    if not token:
        return None
    row = store.one(
        "SELECT user_id, expires_at FROM tokens WHERE token_hash=?", (_token_hash(token),)
    )
    if not row or int(row["expires_at"]) < int(time.time()):
        return None
    return int(row["user_id"])


def revoke_token(store: Store, token: str) -> None:
    store.ex("DELETE FROM tokens WHERE token_hash=?", (_token_hash(token),))


def public_user(store: Store, user_id: int) -> dict:
    row = store.one(
        "SELECT id,email,is_admin,balance_minor,invite_code,created_at FROM users WHERE id=?",
        (user_id,),
    )
    return dict(row) if row else {}
