"""SQLite persistence: the card台 side of the business (cards, CDKs, orders).

Table/field vocabulary is taken from the platform's own API and i18n strings so the
model can be compared with theirs one-to-one:

  orders.payment_path        cs_live | oaics | none
  orders.stage               queued, credential_check, pricing, checkout,
                             payment, subscription, reconcile, renewal,
                             invoice, completed
  orders.purchase_state      NOT_STARTED, SUBMITTED, PENDING, CONFIRMED,
                             FAILED, UNKNOWN
  orders.funding_state       NONE, FUNDING_REQUIRED, FUNDED
  cdks.status                unused, frozen, reserved, review, consumed, disabled
  cdks.code                  prefixed, e.g. GPTD-XXXX... ("GPTD 開頭的 CDK")
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    is_admin      INTEGER NOT NULL DEFAULT 0,
    balance_minor INTEGER NOT NULL DEFAULT 0,
    invite_code   TEXT NOT NULL DEFAULT '',
    frozen_minor  INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recharges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    amount_minor INTEGER NOT NULL,
    method       TEXT NOT NULL DEFAULT 'manual',
    status       TEXT NOT NULL DEFAULT 'credited',
    note         TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bin_stats (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    bin                TEXT NOT NULL,
    plan               TEXT NOT NULL DEFAULT '',
    attempts           INTEGER NOT NULL DEFAULT 0,
    successes          INTEGER NOT NULL DEFAULT 0,
    declines           INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT NOT NULL DEFAULT '',
    last_amount_minor  INTEGER NOT NULL DEFAULT 0,
    last_currency      TEXT NOT NULL DEFAULT '',
    last_seen          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(bin, plan)
);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER,
    label          TEXT NOT NULL DEFAULT '',
    pan_cipher     TEXT NOT NULL,           -- DPAPI ciphertext (dpapi:...)
    cvc_enc        TEXT NOT NULL DEFAULT '',-- DPAPI ciphertext, optional
    exp_month      TEXT NOT NULL,
    exp_year       TEXT NOT NULL,
    holder         TEXT NOT NULL DEFAULT '',
    country        TEXT NOT NULL DEFAULT 'US',
    city           TEXT NOT NULL DEFAULT '',
    address1       TEXT NOT NULL DEFAULT '',
    postal_code    TEXT NOT NULL DEFAULT '',
    bin_head       TEXT NOT NULL DEFAULT '',
    issuer         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',   -- active|frozen|archived
    balance_minor  INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cdks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT UNIQUE NOT NULL,
    user_id        INTEGER NOT NULL,
    plan           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'unused',
    order_id       INTEGER,
    payment_country TEXT NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    used_at        INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no          TEXT UNIQUE NOT NULL,
    user_id           INTEGER NOT NULL,
    product           TEXT NOT NULL DEFAULT 'gpt',
    plan              TEXT NOT NULL,
    session_fp        TEXT NOT NULL,       -- sha256 of the session token
    account_email     TEXT NOT NULL DEFAULT '',
    payment_country   TEXT NOT NULL DEFAULT 'PH',
    payment_currency  TEXT NOT NULL DEFAULT 'PHP',
    payment_mode      TEXT NOT NULL DEFAULT 'direct',
    payment_path      TEXT NOT NULL DEFAULT 'none',
    stage             TEXT NOT NULL DEFAULT 'queued',
    purchase_state    TEXT NOT NULL DEFAULT 'NOT_STARTED',
    funding_state     TEXT NOT NULL DEFAULT 'NONE',
    card_id           INTEGER,
    checkout_session  TEXT NOT NULL DEFAULT '',
    checkout_link     TEXT NOT NULL DEFAULT '',
    amount_minor      INTEGER NOT NULL DEFAULT 0,
    currency          TEXT NOT NULL DEFAULT '',
    quote_json        TEXT NOT NULL DEFAULT '{}',
    error_code        TEXT NOT NULL DEFAULT '',
    error_message     TEXT NOT NULL DEFAULT '',
    client_request_id TEXT NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS order_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL,
    stage      TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL DEFAULT '',
    data_json  TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(purchase_state);
CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id);
"""


def now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: str = "zovo.db"):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns that older databases lack (idempotent)."""
        wanted = {
            "orders": {
                "session_enc": "TEXT NOT NULL DEFAULT ''",
                "bin": "TEXT NOT NULL DEFAULT ''",
                "authorized_by": "TEXT NOT NULL DEFAULT ''",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
            },
            "cards": {
                "bin_stats_key": "TEXT NOT NULL DEFAULT ''",
                "cvc_enc": "TEXT NOT NULL DEFAULT ''",
            },
            "users": {
                "invite_code": "TEXT NOT NULL DEFAULT ''",
                "frozen_minor": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in wanted.items():
            existing = {row["name"] for row in self.q(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    try:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    except sqlite3.OperationalError:
                        pass

    # ---- helpers --------------------------------------------------------
    def q(self, sql: str, args: Iterable = ()) -> List[sqlite3.Row]:
        return list(self.db.execute(sql, tuple(args)))

    def one(self, sql: str, args: Iterable = ()) -> Optional[sqlite3.Row]:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def ex(self, sql: str, args: Iterable = ()) -> int:
        cur = self.db.execute(sql, tuple(args))
        self.db.commit()
        return cur.lastrowid or cur.rowcount

    # ---- users ----------------------------------------------------------
    def ensure_user(self, email: str, password_hash: str = "", admin: bool = False) -> int:
        row = self.one("SELECT id FROM users WHERE email=?", (email,))
        if row:
            return row["id"]
        return self.ex(
            "INSERT INTO users(email,password_hash,is_admin,created_at) VALUES(?,?,?,?)",
            (email, password_hash, 1 if admin else 0, now()),
        )

    # ---- cards ----------------------------------------------------------
    def add_card(
        self,
        pan: str,
        exp_month: str,
        exp_year: str,
        user_id: Optional[int] = None,
        holder: str = "",
        country: str = "US",
        city: str = "",
        address1: str = "",
        postal_code: str = "",
        label: str = "",
        cvc: str = "",
    ) -> int:
        from . import dpapi

        digits = "".join(ch for ch in pan if ch.isdigit())
        return self.ex(
            """INSERT INTO cards(user_id,label,pan_cipher,cvc_enc,exp_month,exp_year,holder,country,
                                 city,address1,postal_code,bin_head,bin_stats_key,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                label,
                dpapi.protect(digits),
                dpapi.protect(cvc) if cvc else "",
                exp_month,
                exp_year,
                holder,
                country,
                city,
                address1,
                postal_code,
                digits[:6],
                digits[:6],
                now(),
            ),
        )

    def pick_card(self, user_id: Optional[int] = None) -> Optional[sqlite3.Row]:
        if user_id is None:
            return self.one("SELECT * FROM cards WHERE status='active' ORDER BY id LIMIT 1")
        return self.one(
            "SELECT * FROM cards WHERE status='active' AND (user_id=? OR user_id IS NULL) ORDER BY id LIMIT 1",
            (user_id,),
        )

    # ---- cdks -----------------------------------------------------------
    def issue_cdks(self, user_id: int, plan: str, count: int, prefix: str = "GPTD") -> List[str]:
        codes = []
        for _ in range(max(1, min(count, 500))):
            code = "%s-%s" % (prefix.upper(), secrets.token_hex(8).upper())
            self.ex(
                "INSERT INTO cdks(code,user_id,plan,created_at) VALUES(?,?,?,?)",
                (code, user_id, plan, now()),
            )
            codes.append(code)
        return codes

    def redeem_cdk(self, code: str, user_id: int, plan: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM cdks WHERE code=? AND user_id=?", (code.strip().upper(), user_id))
        if not row:
            raise ValueError("cdk_not_found")
        if row["status"] != "unused":
            raise ValueError("cdk_not_unused")
        if row["plan"] != plan:
            raise ValueError("cdk_plan_mismatch")
        self.ex("UPDATE cdks SET status='reserved' WHERE id=?", (row["id"],))
        return row

    def consume_cdk(self, code: str, order_id: int) -> None:
        self.ex(
            "UPDATE cdks SET status='consumed', used_at=?, order_id=? WHERE code=?",
            (now(), order_id, code.strip().upper()),
        )

    # ---- orders ---------------------------------------------------------
    def create_order(self, **kw) -> int:
        fields = {
            "order_no": "GP" + uuid.uuid4().hex[:16].upper(),
            "product": "gpt",
            "payment_mode": "direct",
            "stage": "queued",
            "purchase_state": "NOT_STARTED",
            "funding_state": "NONE",
            "quote_json": "{}",
            "created_at": now(),
            "updated_at": now(),
        }
        fields.update(kw)
        cols = ",".join(fields)
        marks = ",".join("?" * len(fields))
        return self.ex(f"INSERT INTO orders({cols}) VALUES({marks})", list(fields.values()))

    def update_order(self, order_id: int, **kw) -> None:
        if not kw:
            return
        kw["updated_at"] = now()
        sets = ",".join(f"{k}=?" for k in kw)
        self.ex(f"UPDATE orders SET {sets} WHERE id=?", [*kw.values(), order_id])

    def log(self, order_id: int, stage: str, message: str = "", level: str = "info", data: Any = None) -> None:
        self.ex(
            "INSERT INTO order_events(order_id,stage,level,message,data_json,created_at) VALUES(?,?,?,?,?,?)",
            (order_id, stage, level, message, json.dumps(data or {}, ensure_ascii=False, default=str), now()),
        )

    def events(self, order_id: int) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (order_id,))

    # ---- balance / audit -------------------------------------------------
    def adjust_balance(self, user_id: int, delta_minor: int, kind: str, note: str = "") -> int:
        self.ex(
            "UPDATE users SET balance_minor = balance_minor + ? WHERE id=?",
            (delta_minor, user_id),
        )
        self.ex(
            "INSERT INTO recharges(user_id,amount_minor,method,status,note,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, delta_minor, kind, "credited" if delta_minor >= 0 else "debited", note, now()),
        )
        row = self.one("SELECT balance_minor FROM users WHERE id=?", (user_id,))
        return int(row["balance_minor"]) if row else 0

    def audit(self, user_id: Optional[int], action: str, detail: str = "") -> None:
        self.ex(
            "INSERT INTO audit(user_id,action,detail,created_at) VALUES(?,?,?,?)",
            (user_id, action, detail[:2000], now()),
        )

    # ---- bin statistics --------------------------------------------------
    def record_bin(
        self,
        bin_code: str,
        plan: str,
        success: bool,
        error: str = "",
        amount_minor: int = 0,
        currency: str = "",
    ) -> None:
        if not bin_code:
            return
        row = self.one("SELECT id FROM bin_stats WHERE bin=? AND plan=?", (bin_code, plan))
        if row:
            self.ex(
                """UPDATE bin_stats SET attempts=attempts+1,
                       successes=successes+?,
                       declines=declines+?,
                       last_error=?, last_amount_minor=?, last_currency=?, last_seen=?
                   WHERE id=?""",
                (1 if success else 0, 0 if success else 1, error[:120], amount_minor,
                 currency, now(), row["id"]),
            )
        else:
            self.ex(
                """INSERT INTO bin_stats(bin,plan,attempts,successes,declines,last_error,
                                         last_amount_minor,last_currency,last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (bin_code, plan, 1, 1 if success else 0, 0 if success else 1,
                 error[:120], amount_minor, currency, now()),
            )

    def bin_ranking(self, plan: str = "") -> List[dict]:
        sql = """SELECT bin, plan, attempts, successes, declines, last_error,
                        last_amount_minor, last_currency, last_seen,
                        CASE WHEN attempts=0 THEN 0.0
                             ELSE CAST(successes AS REAL)/attempts END AS success_rate
                 FROM bin_stats"""
        args: list = []
        if plan:
            sql += " WHERE plan=?"
            args.append(plan)
        sql += " ORDER BY success_rate DESC, successes DESC, last_seen DESC"
        return [dict(r) for r in self.q(sql, args)]
