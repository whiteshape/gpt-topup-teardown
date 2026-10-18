"""CLI: run the direct top-up pipeline by hand, one step at a time.

    python -m zovo.cli check    --session <paste>
    python -m zovo.cli link     --session <paste> --plan plus --country PH --currency PHP --long
    python -m zovo.cli preflight --session <paste> --plan plus
    python -m zovo.cli pay      --link https://checkout.stripe.com/c/pay/cs_live_... --card 4111...|12|30|123
    python -m zovo.cli topup    --session <paste> --plan plus --card 4111...|12|30|123
    python -m zovo.cli session-to-cookie --session <paste>

Every command accepts --proxy (defaults to $ZOVO_PROXY) so the request egresses
from the country whose pricing you want ChatGPT to quote.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import chatgpt, stripe_pay
from .orders import OrderEngine
from .store import Store


def _session_arg(args) -> str:
    """Session can come from the argument, from a file, or from stdin.

    `@path` reads the file, `-` reads stdin. Keeping the credential in a file
    means it never has to travel through a chat log or shell history.
    """
    text = args.session or os.environ.get("ZOVO_SESSION", "")
    if text.startswith("@"):
        with open(text[1:], encoding="utf-8-sig") as fh:
            text = fh.read()
    elif text == "-":
        text = sys.stdin.read()
    return text.strip()


def cmd_check(args) -> int:
    sess = chatgpt.ChatGptSession(_session_arg(args), proxy=args.proxy)
    doc = sess.load_session()
    print(json.dumps({"session_keys": sorted(doc.keys()),
                      "account": doc.get("account", {}).get("id")}, indent=2))
    try:
        print(json.dumps(sess.current_plan(), indent=2, ensure_ascii=False))
    except chatgpt.ChatGptError as exc:
        print(json.dumps({"plan": "unknown", "error": exc.code, "message": exc.message}, indent=2))
    try:
        me = sess.me()
        print(json.dumps({"email": me.get("email", "")}, indent=2))
    except chatgpt.ChatGptError as exc:
        print(json.dumps({"me_error": exc.code, "message": exc.message}, indent=2))
    return 0


def cmd_session_to_cookie(args) -> int:
    sess = chatgpt.ChatGptSession(_session_arg(args), proxy=args.proxy)
    print(sess.cookie_header)
    return 0


def cmd_link(args) -> int:
    sess = chatgpt.ChatGptSession(_session_arg(args), proxy=args.proxy)
    sess.load_session()
    mode = "custom" if args.long else "hosted"
    result = None
    try:
        if args.long:
            try:
                result = sess.create_checkout(args.plan, args.country, args.currency, ui_mode="custom")
            except chatgpt.ChatGptError as exc:
                if exc.code != "gpt_checkout_link_not_long":
                    raise
                print("[long link unavailable] falling back to hosted", file=sys.stderr)
        if result is None:
            result = sess.create_checkout(args.plan, args.country, args.currency, ui_mode="hosted")
    except chatgpt.ChatGptError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": exc.message,
                          "upstream": exc.raw if isinstance(exc.raw, dict) else None}, indent=2,
                         ensure_ascii=False))
        return 1
    quote = sess.quoted_amount(result) or {}
    print(json.dumps({
        "mode": result.mode,
        "session_id": result.session_id,
        "processor_entity": result.processor_entity,
        "payable_with_stripe_api": result.session_id.startswith("cs_"),
        "link": result.link,
        "amount_minor": quote.get("amount_minor"),
        "currency": quote.get("currency") or args.currency,
    }, indent=2))
    return 0


def cmd_preflight(args) -> int:
    store = Store(args.db)
    engine = OrderEngine(store, proxy=args.proxy, payment_proxy=args.payment_proxy)
    data = engine.preflight(
        _session_arg(args), plan=args.plan, country=args.country, currency=args.currency
    )
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def cmd_pay(args) -> int:
    sid = args.link.rstrip("/").split("/")[-1]
    card = stripe_pay.Card.from_pipe(
        args.card,
        name=args.holder,
        email=args.email,
        country=args.billing_country,
        line1=args.address1,
        city=args.city,
        state=args.state,
        postal_code=args.postal_code,
    )
    payer = stripe_pay.StripeCheckoutPayer(proxy=args.proxy)
    result = payer.pay(sid, card)
    print(json.dumps({"status": result.status, "decline_code": result.decline_code,
                      "payment_intent": result.payment_intent,
                      "next_action": result.next_action}, indent=2, default=str))
    return 0 if result.status == "paid" else 1


def cmd_cancel_renewal(args) -> int:
    sess = chatgpt.ChatGptSession(_session_arg(args), proxy=args.proxy)
    sess.load_session()
    result = sess.cancel_renewal(args.subscription_id or "")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


def cmd_topup(args) -> int:
    store = Store(args.db)
    engine = OrderEngine(
        store,
        proxy=args.proxy,
        payment_proxy=args.payment_proxy,
        max_amount_minor=args.max_amount,
        dry_run=args.dry_run,
    )
    result = engine.run(
        _session_arg(args),
        plan=args.plan,
        country=args.country,
        currency=args.currency,
        card_pipe=args.card or "",
        cdk_code=args.cdk or "",
        cancel_renewal=args.cancel_renewal,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="zovo")
    ap.add_argument("--db", default="zovo.db")
    ap.add_argument("--proxy", default=os.environ.get("ZOVO_PROXY") or None)
    ap.add_argument("--payment-proxy", default=os.environ.get("ZOVO_PAYMENT_PROXY") or None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check")
    p.add_argument("--session", required=True)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("session-to-cookie")
    p.add_argument("--session", required=True)
    p.set_defaults(fn=cmd_session_to_cookie)

    p = sub.add_parser("link")
    p.add_argument("--session", required=True)
    p.add_argument("--plan", default="plus", choices=sorted(chatgpt.PLANS))
    p.add_argument("--country", default="PH")
    p.add_argument("--currency", default="PHP")
    p.add_argument("--long", action="store_true", help="checkout_ui_mode=custom (Stripe page)")
    p.set_defaults(fn=cmd_link)

    p = sub.add_parser("preflight")
    p.add_argument("--session", required=True)
    p.add_argument("--plan", default="plus")
    p.add_argument("--country", default="PH")
    p.add_argument("--currency", default="PHP")
    p.set_defaults(fn=cmd_preflight)

    p = sub.add_parser("pay")
    p.add_argument("--link", required=True)
    p.add_argument("--card", required=True, help="number|mm|yy|cvc")
    p.add_argument("--holder", default="John Smith")
    p.add_argument("--email", default="john.smith@example.com")
    p.add_argument("--billing-country", default="US")
    p.add_argument("--address1", default="350 5th Ave")
    p.add_argument("--city", default="New York")
    p.add_argument("--state", default="NY")
    p.add_argument("--postal-code", default="10118")
    p.set_defaults(fn=cmd_pay)

    p = sub.add_parser("cancel-renewal")
    p.add_argument("--session", required=True)
    p.add_argument("--subscription-id", default="")
    p.set_defaults(fn=cmd_cancel_renewal)

    p = sub.add_parser("topup")
    p.add_argument("--session", required=True)
    p.add_argument("--plan", default="plus")
    p.add_argument("--country", default="PH")
    p.add_argument("--currency", default="PHP")
    p.add_argument("--card", default="", help="number|mm|yy|cvc")
    p.add_argument("--cdk", default="")
    p.add_argument("--max-amount", type=int, default=0)
    p.add_argument("--cancel-renewal", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_topup)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
