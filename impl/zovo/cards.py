"""Card pool: selection by BIN track record, and result recording.

This is the piece that turns "one card that might work" into an operation.
Selection order:

  1. explicit card_id (operator picked it)
  2. cards whose BIN has the best success rate for this plan
  3. cards with no history yet (exploration)
  4. everything else

A card that has failed 2+ times in a row is skipped unless it is the only one
left, which mirrors the platform's own `card-rules`
(`light_max_uses`, `max_auto_switches`, `count_failures`).
"""

from __future__ import annotations

from typing import List, Optional

from . import dpapi
from .store import Store
from .stripe_pay import Card

MAX_FAILURES_BEFORE_SKIP = 2


def row_to_card(row) -> Card:
    pan = dpapi.unprotect(row["pan_cipher"])
    return Card(
        number=pan,
        exp_month=row["exp_month"],
        exp_year=row["exp_year"],
        cvc=row["cvc_enc"] and dpapi.unprotect(row["cvc_enc"]) or "",
        name=row["holder"] or "John Smith",
        country=row["country"] or "US",
        line1=row["address1"] or "",
        city=row["city"] or "",
        postal_code=row["postal_code"] or "",
    )


def select(
    store: Store,
    plan: str = "",
    user_id: Optional[int] = None,
    card_id: Optional[int] = None,
) -> Optional[dict]:
    if card_id:
        row = store.one("SELECT * FROM cards WHERE id=? AND status='active'", (card_id,))
        return dict(row) if row else None

    sql = "SELECT * FROM cards WHERE status='active'"
    args: List = []
    if user_id:
        sql += " AND (user_id=? OR user_id IS NULL)"
        args.append(user_id)
    rows = [dict(r) for r in store.q(sql, args)]
    if not rows:
        return None

    ranking = {r["bin"]: r for r in store.bin_ranking(plan)}
    for row in rows:
        stat = ranking.get(row.get("bin_stats_key") or dpapi.bin_of(row["pan_cipher"]))
        row["_attempts"] = int(stat["attempts"]) if stat else 0
        row["_rate"] = float(stat["success_rate"]) if stat else -1.0
        row["_declines"] = int(stat["declines"]) if stat else 0

    # best proven first, then untried, then known-bad
    rows.sort(key=lambda r: (-r["_rate"], r["_declines"]))
    for row in rows:
        if row["_declines"] >= MAX_FAILURES_BEFORE_SKIP and len(rows) > 1:
            continue
        return row
    return rows[0]


def record(
    store: Store,
    row: dict,
    plan: str,
    success: bool,
    error: str = "",
    amount_minor: int = 0,
    currency: str = "",
) -> None:
    bin_code = row.get("bin_stats_key") or dpapi.bin_of(dpapi.unprotect(row["pan_cipher"]))
    store.record_bin(bin_code, plan, success, error, amount_minor, currency)
