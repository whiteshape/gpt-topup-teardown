"""HTTP surface - a working subset of the platform's own /api/v1 contract, plus auth.

Envelope: {"code":0,"data":...,"msg":"ok"}
Auth:     Authorization: Bearer <token>   (or ?token= for the built-in UI)

    python -m zovo.api --port 8850 [--proxy http://127.0.0.1:7897]

Customer
    POST /api/v1/auth/register        {email,password,invite_code?}
    POST /api/v1/auth/login           {email,password}
    GET  /api/v1/user/profile
    GET  /api/v1/user/balance-logs
    POST /api/v1/gpt/session-to-cookie
    POST /api/v1/gpt/check
    POST /api/v1/gpt/direct/preflight
    POST /api/v1/gpt/direct/orders        (sync or ?async=1)
    GET  /api/v1/gpt/direct/orders[/{id}]
    POST /api/v1/gpt/direct/orders/{id}/cancel
    GET  /api/v1/gpt/direct/plan-registry
    GET  /api/v1/gpt/direct/region-prices
    GET  /api/v1/gpt/direct/cdks
    POST /api/v1/gpt/direct/cdks/purchase
    POST /api/v1/gpt/direct/cdks/{id}/{freeze|unfreeze|disable}
    GET  /api/v1/card/list
    POST /api/v1/card/add
    GET  /api/v1/jobs/{job_id}

Admin
    POST /api/v1/admin/recharge           {user_id,amount_minor,note}
    GET  /api/v1/admin/users
    GET  /api/v1/admin/orders
    GET  /api/v1/admin/bin-stats?plan=plus
    POST /api/v1/admin/cdks/issue         {user_id,plan,count}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple

from . import auth, chatgpt, dpapi
from .orders import SERVICE_FEE_MINOR, OrderEngine
from .store import Store
from .worker import Worker

STATE: Dict[str, Any] = {}
ROUTES: Dict[Tuple[str, str], Tuple[Any, Callable]] = {}


def route(method: str, pattern: str, public: bool = False, admin: bool = False):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES[(method, pattern)] = (regex, fn, public, admin)
        return fn

    return deco


def ok(data: Any = None, msg: str = "ok") -> Tuple[int, dict]:
    return 200, {"code": 0, "data": data, "msg": msg}


def err(code: str, msg: str, http: int = 400) -> Tuple[int, dict]:
    return http, {"code": code, "data": None, "msg": msg}


# ------------------------------------------------------------------ auth
@route("POST", "/api/v1/auth/register", public=True)
def register(body: dict, _q, _p, _c) -> Tuple[int, dict]:
    try:
        uid = auth.register(STATE["store"], body.get("email", ""), body.get("password", ""),
                            body.get("invite_code", ""))
    except ValueError as exc:
        return err(str(exc), "email already registered")
    token = auth.issue_token(STATE["store"], uid)
    return ok({"token": token, "user": auth.public_user(STATE["store"], uid)})


@route("POST", "/api/v1/auth/login", public=True)
def login(body: dict, _q, _p, _c) -> Tuple[int, dict]:
    uid = auth.authenticate(STATE["store"], body.get("email", ""), body.get("password", ""))
    if not uid:
        return err("bad_credentials", "email or password is wrong", 401)
    token = auth.issue_token(STATE["store"], uid)
    return ok({"token": token, "user": auth.public_user(STATE["store"], uid)})


@route("GET", "/api/v1/user/profile")
def profile(_b, _q, _p, ctx) -> Tuple[int, dict]:
    return ok(auth.public_user(STATE["store"], ctx["user_id"]))


@route("GET", "/api/v1/user/balance-logs")
def balance_logs(_b, query, _p, ctx) -> Tuple[int, dict]:
    rows = STATE["store"].q(
        "SELECT * FROM recharges WHERE user_id=? ORDER BY id DESC LIMIT 100", (ctx["user_id"],)
    )
    return ok({"list": [dict(r) for r in rows]})


# ------------------------------------------------------------------ gpt
@route("POST", "/api/v1/gpt/session-to-cookie")
def session_to_cookie(body: dict, _q, _p, _c) -> Tuple[int, dict]:
    token = chatgpt.parse_credential(body.get("token_input", ""))
    sess = chatgpt.ChatGptSession(token, proxy=STATE["proxy"])
    return ok({"cookie": sess.cookie_header, "device_id": sess.device_id,
               "session_token_length": len(token)})


@route("POST", "/api/v1/gpt/check")
def gpt_check(body: dict, _q, _p, _c) -> Tuple[int, dict]:
    try:
        sess = chatgpt.ChatGptSession(body.get("token_input", ""), proxy=STATE["proxy"])
        sess.load_session()
        me = sess.me()
        state = sess.current_plan()
    except chatgpt.ChatGptError as exc:
        return err(exc.code, exc.message)
    return ok({"account": me.get("email", ""), "plan": state.get("plan"), "subscription": state})


@route("POST", "/api/v1/gpt/direct/preflight")
def preflight(body: dict, _q, _p, _c) -> Tuple[int, dict]:
    credential = body.get("session") or (body.get("credential") or {}).get("session") or ""
    try:
        data = STATE["engine"].preflight(
            credential,
            plan=body.get("plan") or "plus",
            country=body.get("payment_country") or "PH",
            currency=body.get("payment_currency") or "PHP",
        )
    except chatgpt.ChatGptError as exc:
        return err(exc.code, exc.message)
    return ok(data)


@route("POST", "/api/v1/gpt/direct/orders")
def create_order(body: dict, query, _p, ctx) -> Tuple[int, dict]:
    credential = body.get("session") or (body.get("credential") or {}).get("session")
    if not credential:
        return err("gpt_session_format_invalid", "session is required")
    if not body.get("authorized"):
        return err(
            "authorization_required",
            "the account holder must confirm they authorized this top-up (authorized=true)",
        )
    plan = body.get("plan") or "plus"
    cdk_code = body.get("cdk_code") or ""
    user_id = ctx["user_id"]

    if cdk_code:
        try:
            STATE["store"].redeem_cdk(cdk_code, user_id, plan)
        except ValueError as exc:
            return err("cdk_invalid", str(exc))

    kwargs = dict(
        credential=credential,
        plan=plan,
        user_id=user_id,
        country=body.get("payment_country") or "PH",
        currency=body.get("payment_currency") or "PHP",
        card_pipe=body.get("raw_card", ""),
        card_id=body.get("card_id"),
        cdk_code=cdk_code,
        cancel_renewal=bool(body.get("cancel_renewal")),
        authorized_by=body.get("authorized_by") or f"user:{user_id}",
    )

    if (query.get("async") or ["0"])[0] in ("1", "true", "yes"):
        job_id = STATE["worker"].submit(**kwargs)
        return ok({"job_id": job_id, "state": "queued"})

    try:
        return ok(STATE["engine"].run(**kwargs))
    except chatgpt.ChatGptError as exc:
        return err(exc.code, exc.message)


@route("GET", "/api/v1/jobs/{job_id}")
def job_status(_b, _q, p, _c) -> Tuple[int, dict]:
    job = STATE["worker"].status(p["job_id"])
    if not job:
        return err("not_found", "unknown job", 404)
    return ok(job)


@route("GET", "/api/v1/gpt/direct/orders")
def list_orders(_b, query, _p, ctx) -> Tuple[int, dict]:
    page = max(1, int(query.get("page", [1])[0]))
    size = max(1, min(100, int(query.get("page_size", [20])[0])))
    if ctx["is_admin"] and (query.get("all") or ["0"])[0] == "1":
        rows = STATE["store"].q("SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?",
                                (size, (page - 1) * size))
        total = STATE["store"].one("SELECT COUNT(*) c FROM orders")["c"]
    else:
        rows = STATE["store"].q(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (ctx["user_id"], size, (page - 1) * size))
        total = STATE["store"].one(
            "SELECT COUNT(*) c FROM orders WHERE user_id=?", (ctx["user_id"],))["c"]
    return ok({"total": total, "list": [dict(r) for r in rows]})


@route("GET", "/api/v1/gpt/direct/orders/{order_id}")
def get_order(_b, _q, p, ctx) -> Tuple[int, dict]:
    row = STATE["store"].one("SELECT * FROM orders WHERE id=?", (p["order_id"],))
    if not row:
        return err("not_found", "order not found", 404)
    if int(row["user_id"]) != ctx["user_id"] and not ctx["is_admin"]:
        return err("forbidden", "not your order", 403)
    data = dict(row)
    data.pop("session_enc", None)
    data["events"] = [dict(e) for e in STATE["store"].events(int(p["order_id"]))]
    return ok(data)


@route("POST", "/api/v1/gpt/direct/orders/{order_id}/cancel")
def cancel_order(_b, _q, p, ctx) -> Tuple[int, dict]:
    row = STATE["store"].one("SELECT * FROM orders WHERE id=?", (p["order_id"],))
    if not row:
        return err("not_found", "order not found", 404)
    if int(row["user_id"]) != ctx["user_id"] and not ctx["is_admin"]:
        return err("forbidden", "not your order", 403)
    STATE["store"].update_order(int(p["order_id"]), purchase_state="FAILED",
                                error_code="cancelled", session_enc="")
    STATE["store"].log(int(p["order_id"]), "completed", "cancelled by user", level="warn")
    return ok({"cancelled": 1})


@route("GET", "/api/v1/gpt/direct/plan-registry", public=True)
def plan_registry(_b, _q, _p, _c) -> Tuple[int, dict]:
    return ok({"payment_regions": PAYMENT_REGIONS, "plans": PLAN_REGISTRY})


@route("GET", "/api/v1/gpt/direct/region-prices", public=True)
def region_prices(_b, query, _p, _c) -> Tuple[int, dict]:
    country = (query.get("country") or ["PH"])[0]
    return ok({"country": country, "prices": REGION_PRICES.get(country, {})})


# ------------------------------------------------------------------ cdk
@route("GET", "/api/v1/gpt/direct/cdks")
def list_cdks(_b, _q, _p, ctx) -> Tuple[int, dict]:
    rows = STATE["store"].q(
        "SELECT * FROM cdks WHERE user_id=? ORDER BY id DESC LIMIT 200", (ctx["user_id"],))
    return ok({"list": [dict(r) for r in rows]})


@route("POST", "/api/v1/gpt/direct/cdks/purchase")
def purchase_cdks(body: dict, _q, _p, ctx) -> Tuple[int, dict]:
    count = max(1, min(int(body.get("count") or 1), 500))
    price = int(body.get("unit_price_minor") or 100)  # $1.00 default list price
    total = count * price
    user = STATE["store"].one("SELECT balance_minor FROM users WHERE id=?", (ctx["user_id"],))
    if int(user["balance_minor"]) < total:
        return err("insufficient_balance",
                   f"need {total} minor units, have {user['balance_minor']}")
    STATE["store"].adjust_balance(ctx["user_id"], -total, "cdk_purchase",
                                  f"{count} x {body.get('plan') or 'plus'}")
    codes = STATE["store"].issue_cdks(ctx["user_id"], body.get("plan") or "plus", count,
                                      prefix=body.get("brand_prefix") or "GPTD")
    return ok({"codes": codes, "charged_minor": total})


@route("POST", "/api/v1/gpt/direct/cdks/{cdk_id}/{action}")
def cdk_action(_b, _q, p, ctx) -> Tuple[int, dict]:
    status = {"freeze": "frozen", "unfreeze": "unused", "disable": "disabled"}.get(p["action"])
    if not status:
        return err("bad_action", "unknown action")
    STATE["store"].ex("UPDATE cdks SET status=? WHERE id=? AND user_id=?",
                      (status, p["cdk_id"], ctx["user_id"]))
    return ok({"id": int(p["cdk_id"]), "status": status})


# ------------------------------------------------------------------ cards
@route("GET", "/api/v1/card/list")
def card_list(_b, _q, _p, ctx) -> Tuple[int, dict]:
    rows = STATE["store"].q(
        "SELECT * FROM cards WHERE user_id=? OR user_id IS NULL ORDER BY id DESC LIMIT 200",
        (ctx["user_id"],))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["pan_mask"] = dpapi.mask_pan(dpapi.unprotect(d.pop("pan_cipher")))
        except dpapi.DpapiError:
            d["pan_mask"] = "decrypt-failed"
            d.pop("pan_cipher", None)
        d.pop("cvc_enc", None)
        d["bin"] = d.get("bin_stats_key", "")
        out.append(d)
    return ok({"list": out})


@route("POST", "/api/v1/card/add")
def card_add(body: dict, _q, _p, ctx) -> Tuple[int, dict]:
    pan = re.sub(r"\D", "", body.get("number", ""))
    if len(pan) < 13:
        return err("card_format", "card number too short")
    cid = STATE["store"].add_card(
        pan=pan,
        cvc=body.get("cvc", ""),
        exp_month=str(body.get("exp_month", "")).zfill(2),
        exp_year=str(body.get("exp_year", ""))[-2:],
        user_id=ctx["user_id"],
        holder=body.get("holder", ""),
        country=body.get("country", "US"),
        city=body.get("city", ""),
        address1=body.get("address1", ""),
        postal_code=body.get("postal_code", ""),
        label=body.get("label", ""),
    )
    return ok({"card_id": cid})


# ------------------------------------------------------------------ admin
def _admin(ctx) -> bool:
    return ctx.get("is_admin", False)


@route("POST", "/api/v1/admin/recharge", admin=True)
def admin_recharge(body: dict, _q, _p, ctx) -> Tuple[int, dict]:
    if not _admin(ctx):
        return err("forbidden", "admin only", 403)
    uid = int(body.get("user_id") or 0)
    amount = int(body.get("amount_minor") or 0)
    if not uid or not amount:
        return err("bad_request", "user_id and amount_minor are required")
    balance = STATE["store"].adjust_balance(uid, amount, "admin_recharge",
                                            body.get("note", ""))
    STATE["store"].audit(ctx["user_id"], "admin.recharge", f"user={uid} amount={amount}")
    return ok({"user_id": uid, "balance_minor": balance})


@route("GET", "/api/v1/admin/users", admin=True)
def admin_users(_b, _q, _p, ctx) -> Tuple[int, dict]:
    if not _admin(ctx):
        return err("forbidden", "admin only", 403)
    rows = STATE["store"].q(
        "SELECT id,email,is_admin,balance_minor,created_at FROM users ORDER BY id")
    return ok({"list": [dict(r) for r in rows]})


@route("GET", "/api/v1/admin/orders", admin=True)
def admin_orders(_b, query, _p, ctx) -> Tuple[int, dict]:
    if not _admin(ctx):
        return err("forbidden", "admin only", 403)
    page = max(1, int(query.get("page", [1])[0]))
    size = max(1, min(200, int(query.get("page_size", [50])[0])))
    rows = STATE["store"].q("SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?",
                            (size, (page - 1) * size))
    return ok({"list": [dict(r) for r in rows]})


@route("GET", "/api/v1/admin/bin-stats", admin=True)
def admin_bin_stats(_b, query, _p, ctx) -> Tuple[int, dict]:
    if not _admin(ctx):
        return err("forbidden", "admin only", 403)
    return ok({"list": STATE["store"].bin_ranking((query.get("plan") or [""])[0])})


@route("POST", "/api/v1/admin/cdks/issue", admin=True)
def admin_issue(_b, _q, p, ctx) -> Tuple[int, dict]:
    return err("use_purchase_endpoint", "issue CDKs through /gpt/direct/cdks/purchase")


# ------------------------------------------------------------------ fixtures
PAYMENT_REGIONS = [
    {"country": "PH", "currency": "PHP"},
    {"country": "CL", "currency": "CLP"},
    {"country": "EG", "currency": "EGP"},
    {"country": "JP", "currency": "JPY"},
    {"country": "US", "currency": "USD"},
]

PLAN_REGISTRY = [
    {"key": "plus", "product": "gpt", "funding": "bin_snapshot", "label": "Plus",
     "flow": "direct", "tier": 2, "checkout_currency": "PHP",
     "checkout_amount_minor": 98214, "max_amount_minor": 110000,
     "service_fee_usd_minor": SERVICE_FEE_MINOR, "purchasable": True},
    {"key": "pro_5x", "product": "gpt", "funding": "bin_snapshot", "label": "Pro 5x",
     "flow": "direct", "tier": 3, "checkout_currency": "PHP",
     "checkout_amount_minor": 579464, "max_amount_minor": 650000,
     "service_fee_usd_minor": SERVICE_FEE_MINOR, "purchasable": True},
    {"key": "pro_20x", "product": "gpt", "funding": "bin_snapshot", "label": "Pro 20x",
     "flow": "direct", "tier": 4, "checkout_currency": "PHP",
     "checkout_amount_minor": 891966, "max_amount_minor": 1050000,
     "service_fee_usd_minor": SERVICE_FEE_MINOR, "purchasable": True},
    {"key": "pro_5x_renew", "product": "gpt", "funding": "bin_snapshot", "label": "Pro 5x 续费",
     "flow": "card_attach", "tier": 3, "checkout_currency": "PHP",
     "checkout_amount_minor": 579464, "max_amount_minor": 650000,
     "service_fee_usd_minor": SERVICE_FEE_MINOR, "purchasable": True},
]

REGION_PRICES = {
    "PH": {
        "plus": {"currency": "PHP", "amount_minor": 98214, "tax_mode": "exclusive",
                 "source": "collected_checkout"},
        "pro_5x": {"currency": "PHP", "amount_minor": 579464, "tax_mode": "exclusive",
                   "source": "collected_checkout"},
        "pro_20x": {"currency": "PHP", "amount_minor": 891964, "tax_mode": "exclusive",
                    "source": "collected_checkout"},
    }
}


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZovoDirect/1.1"

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type,authorization")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS")
        self.end_headers()
        self.wfile.write(blob)

    def _send_html(self, html: str) -> None:
        blob = html.encode("utf-8")
        self.send_response_only(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _auth(self) -> Dict[str, Any]:
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            token = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                     .get("token", [""])[0])
        uid = auth.resolve_token(STATE["store"], token)
        if not uid:
            return {"user_id": 0, "is_admin": False, "token": token}
        row = STATE["store"].one("SELECT is_admin FROM users WHERE id=?", (uid,))
        return {"user_id": uid, "is_admin": bool(row and row["is_admin"]), "token": token}

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" and method == "GET":
            self._send_html(UI_HTML)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"code": "bad_json", "data": None, "msg": "invalid JSON body"})
            return

        ctx = self._auth()
        for (route_method, _pattern), (regex, fn, public, admin_only) in ROUTES.items():
            if route_method != method:
                continue
            m = regex.match(path)
            if not m:
                continue
            if not public and not ctx["user_id"]:
                self._send(401, {"code": "unauthorized", "data": None, "msg": "login required"})
                return
            if admin_only and not ctx["is_admin"]:
                self._send(403, {"code": "forbidden", "data": None, "msg": "admin only"})
                return
            try:
                status, payload = fn(body, query, m.groupdict(), ctx)
            except chatgpt.ChatGptError as exc:
                status, payload = 400, {"code": exc.code, "data": None, "msg": exc.message}
            except Exception as exc:  # noqa: BLE001
                status, payload = 500, {"code": "internal_error", "data": None, "msg": str(exc)}
            self._send(status, payload)
            return
        self._send(404, {"code": "not_found", "data": None, "msg": f"no route for {path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._send(204, {})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8850)
    ap.add_argument("--db", default="zovo.db")
    ap.add_argument("--proxy", default=os.environ.get("ZOVO_PROXY") or None)
    ap.add_argument("--payment-proxy", default=os.environ.get("ZOVO_PAYMENT_PROXY") or None)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--admin-email", default="admin@local")
    ap.add_argument("--admin-password", default="admin12345")
    args = ap.parse_args()

    store = Store(args.db)
    admin_id = store.ensure_user(args.admin_email, admin=True)
    # make sure the bootstrap admin can actually log in
    row = store.one("SELECT password_hash FROM users WHERE id=?", (admin_id,))
    if not row or not row["password_hash"]:
        digest, salt = auth.hash_password(args.admin_password)
        store.ex("UPDATE users SET password_hash=? WHERE id=?",
                 (f"pbkdf2${auth.PBKDF2_ROUNDS}${salt}${digest}", admin_id))

    engine = OrderEngine(store, proxy=args.proxy, payment_proxy=args.payment_proxy)
    STATE.update({
        "store": store,
        "engine": engine,
        "worker": Worker(engine, size=args.workers),
        "proxy": args.proxy,
        "payment_proxy": args.payment_proxy,
    })

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    print(f"zovo api on http://127.0.0.1:{args.port}  (ui at /)", flush=True)
    print(f"admin: {args.admin_email} / {args.admin_password}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE["worker"].shutdown()
    return 0


UI_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zovo Direct · 控制台</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,Segoe UI,Roboto,"Noto Sans SC",sans-serif;
     background:#f6f7fb;color:#12141a}
header{background:#12141a;color:#fff;padding:14px 22px;display:flex;gap:14px;align-items:center}
header b{font-size:16px}
main{max-width:1080px;margin:22px auto;padding:0 18px;display:grid;gap:18px}
.card{background:#fff;border:1px solid #e6e8ef;border-radius:12px;padding:18px}
h2{margin:0 0 12px;font-size:15px}
label{display:block;font-size:12px;color:#5b6172;margin:10px 0 4px}
input,textarea,select{width:100%;padding:9px 11px;border:1px solid #d7dae4;border-radius:8px;
  font:inherit;background:#fff}
textarea{min-height:110px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
button{padding:9px 16px;border:0;border-radius:8px;background:#12141a;color:#fff;font:inherit;
  cursor:pointer;margin-top:12px}
button.ghost{background:#eceef4;color:#12141a}
pre{background:#0e1015;color:#d7dae4;padding:12px;border-radius:8px;overflow:auto;
  font-size:12px;max-height:320px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#eceef4}
.ok{background:#e6f6ec;color:#12633a}.bad{background:#fdeaea;color:#8c1d1d}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #eef0f5}
th{color:#5b6172;font-weight:600}
.hint{color:#5b6172;font-size:12px}
</style></head><body>
<header><b>Zovo Direct</b><span class="hint" style="color:#9aa1b2">GPT 直充控制台</span>
<span style="flex:1"></span><span id="who" class="hint" style="color:#9aa1b2"></span></header>
<main>
  <section class="card" id="loginCard">
    <h2>登录 / 注册</h2>
    <div class="row">
      <div><label>邮箱</label><input id="email" value="admin@local"></div>
      <div><label>密码</label><input id="password" type="password" value="admin12345"></div>
    </div>
    <button onclick="doLogin()">登录</button>
    <button class="ghost" onclick="doRegister()">注册</button>
    <p class="hint">默认管理员：admin@local / admin12345（首次启动自动创建）</p>
  </section>

  <section class="card" id="orderCard" hidden>
    <h2>提交直充</h2>
    <label>ChatGPT Session（完整 JSON，含 sessionToken）</label>
    <textarea id="session" placeholder="粘贴 https://chatgpt.com/api/auth/session 的整段 JSON"></textarea>
    <div class="row">
      <div><label>套餐</label><select id="plan"><option>plus</option><option>pro_5x</option>
        <option>pro_20x</option></select></div>
      <div><label>地区</label><select id="country"><option value="PH|PHP">菲律宾 PHP</option>
        <option value="JP|JPY">日本 JPY</option><option value="US|USD">美国 USD</option></select></div>
    </div>
    <div class="row">
      <div><label>卡号|月|年|CVC（留空则用卡池）</label>
        <input id="card" placeholder="4111111111111111|12|30|123"></div>
      <div><label>CDK（可选）</label><input id="cdk" placeholder="GPTD-XXXX"></div>
    </div>
    <label style="display:flex;gap:8px;align-items:center;margin-top:12px">
      <input type="checkbox" id="authorized" style="width:auto">
      我确认该账号持有人已授权本次代充</label>
    <button onclick="submitOrder()">提交</button>
    <button class="ghost" onclick="preflight()">仅预检（不扣款）</button>
    <pre id="out">等待操作…</pre>
  </section>

  <section class="card" id="ordersCard" hidden>
    <h2>订单</h2>
    <button class="ghost" onclick="loadOrders()">刷新</button>
    <table><thead><tr><th>ID</th><th>套餐</th><th>账号</th><th>路径</th><th>阶段</th>
      <th>状态</th><th>金额</th><th>错误</th></tr></thead><tbody id="orders"></tbody></table>
  </section>
</main>
<script>
let TOKEN = localStorage.getItem('zovo_token') || '';
const $ = (id) => document.getElementById(id);
const fmt = (o) => JSON.stringify(o, null, 2);

function setAuth(t, who) {
  TOKEN = t || ''; localStorage.setItem('zovo_token', TOKEN);
  const on = !!TOKEN;
  $('loginCard').hidden = on; $('orderCard').hidden = !on; $('ordersCard').hidden = !on;
  $('who').textContent = who || '';
  if (on) loadOrders();
}
async function api(path, body, method) {
  const r = await fetch(path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN},
    body: body ? JSON.stringify(body) : undefined,
  });
  return r.json();
}
async function doLogin() {
  const d = await api('/api/v1/auth/login', {email: $('email').value, password: $('password').value});
  if (d.code === 0) setAuth(d.data.token, d.data.user.email + ' · 余额 ' + d.data.user.balance_minor);
  else alert(d.msg);
}
async function doRegister() {
  const d = await api('/api/v1/auth/register', {email: $('email').value, password: $('password').value});
  if (d.code === 0) setAuth(d.data.token, d.data.user.email); else alert(d.msg);
}
async function preflight() {
  $('out').textContent = '预检中…';
  const [c, cur] = $('country').value.split('|');
  const d = await api('/api/v1/gpt/direct/preflight', {
    session: $('session').value, plan: $('plan').value,
    payment_country: c, payment_currency: cur});
  $('out').textContent = fmt(d);
}
async function submitOrder() {
  if (!$('authorized').checked) { alert('请先勾选授权确认'); return; }
  $('out').textContent = '提交中…（最长 6 分钟）';
  const [c, cur] = $('country').value.split('|');
  const d = await api('/api/v1/gpt/direct/orders', {
    session: $('session').value, plan: $('plan').value,
    payment_country: c, payment_currency: cur,
    raw_card: $('card').value, cdk_code: $('cdk').value,
    authorized: true});
  $('out').textContent = fmt(d);
  loadOrders();
}
async function loadOrders() {
  const d = await api('/api/v1/gpt/direct/orders?page_size=30');
  if (d.code !== 0) return;
  $('orders').innerHTML = (d.data.list || []).map(o => `<tr>
    <td>${o.id}</td><td>${o.plan}</td><td>${o.account_email || ''}</td>
    <td>${o.payment_path}</td><td>${o.stage}</td>
    <td><span class="pill ${o.purchase_state === 'CONFIRMED' ? 'ok' : (o.purchase_state === 'FAILED' ? 'bad' : '')}">${o.purchase_state}</span></td>
    <td>${o.amount_minor}</td><td>${o.error_code || ''}</td></tr>`).join('');
}
if (TOKEN) setAuth(TOKEN, '');
</script></body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
