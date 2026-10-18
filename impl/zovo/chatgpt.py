"""ChatGPT checkout protocol engine.

Everything here is the mechanism that the platform's "GPT 直充 / 协议直充" module
uses, reconstructed from its shipped front-end bundle:

  verified   = the string/payload literally appears in the platform's index bundle
               or in the "F12 取链脚本" it generates for operators.
  inferred   = standard/community-known ChatGPT backend endpoints that the
               bundle implies but does not print.

Verified artefacts (evidence):
  * POST https://chatgpt.com/backend-api/payments/checkout
        body: {entry_point, plan_name, billing_details:{country,currency},
               checkout_ui_mode: "hosted"|"custom", price_interval,
               selected_cadence, promo_campaign, team_plan_data}
        headers: Authorization: Bearer <accessToken>,
                 chatgpt-account-id, oai-device-id, oai-language
        response: {checkout_session_id, processor_entity, url?}
        short link -> https://chatgpt.com/checkout/<processor_entity>/<id>
        long  link -> https://checkout.stripe.com/c/pay/<cs_...>
  * GET  https://chatgpt.com/api/auth/session   (source of sessionToken/accessToken)
  * the credential the platform accepts is the COMPLETE session JSON containing
    `sessionToken` (the __Secure-next-auth.session-token JWE). A bare access
    token (eyJ...) is explicitly rejected by the platform.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests

CHATGPT = "https://chatgpt.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ---- plan catalogue, exactly as the bundle defines it -------------------
# gptTool.planOptions / Pe() in GptToolView-Cyv-uZ_k.js
PLANS: Dict[str, Dict[str, Any]] = {
    "plus": {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "promo": {"promo_campaign_id": "plus-1-month", "is_coupon_from_query_param": False},
    },
    "pro": {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptpro",
    },
    "prolite": {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptprolite",
    },
    "team": {
        "entry_point": "team_workspace_purchase_modal",
        "plan_name": "chatgptteamplan",
        "team_plan_data": {
            "workspace_name": "MyWorkspace",
            "price_interval": "month",
            "seat_quantity": 5,
        },
    },
}

SESSION_TOKEN_COOKIE = "__Secure-next-auth.session-token"

_DEVICE_ID_CACHE: Optional[str] = None


def default_device_id() -> str:
    """One stable device id per machine.

    Rotating the device id on every request is itself a bot signal: a real
    browser keeps the same `oai-did` for a year. We persist one locally
    (override with $ZOVO_DEVICE_ID).
    """
    global _DEVICE_ID_CACHE
    if _DEVICE_ID_CACHE:
        return _DEVICE_ID_CACHE

    from_env = os.environ.get("ZOVO_DEVICE_ID")
    if from_env:
        _DEVICE_ID_CACHE = from_env.strip()
        return _DEVICE_ID_CACHE

    path = os.environ.get("ZOVO_DEVICE_ID_FILE") or os.path.join(
        os.path.expanduser("~"), ".zovo_device_id"
    )
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                stored = fh.read().strip()
            if stored:
                _DEVICE_ID_CACHE = stored
                return stored
    except OSError:
        pass

    generated = str(uuid.uuid4())
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(generated)
    except OSError:
        pass
    _DEVICE_ID_CACHE = generated
    return generated


class ChatGptError(RuntimeError):
    """Upstream error carrying the platform's own error vocabulary."""

    def __init__(self, code: str, message: str = "", status: int = 0, raw: Any = None):
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.status = status
        self.raw = raw


@dataclass
class CheckoutResult:
    session_id: str
    processor_entity: str
    mode: str            # "hosted" (short) | "custom" (long)
    link: str
    raw: Dict[str, Any] = field(default_factory=dict)


def parse_credential(token_input: str) -> str:
    """Accept the full /api/auth/session JSON, a bare JWE, or a cookie line.

    Returns the __Secure-next-auth.session-token value (a JWE, 5 dot-separated
    segments). The platform rejects everything else, and so do we.
    """
    text = (token_input or "").strip()
    if not text:
        raise ChatGptError("gpt_session_format_invalid", "empty credential")

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ChatGptError("gpt_session_format_invalid", f"invalid JSON: {exc}") from exc
        token = data.get("sessionToken")
        if not token:
            raise ChatGptError(
                "gpt_session_format_invalid",
                "JSON has no sessionToken field (access tokens are not accepted)",
            )
        return str(token).strip()

    # "cookie: a=b; __Secure-next-auth.session-token=...; c=d"
    if SESSION_TOKEN_COOKIE in text:
        m = re.search(re.escape(SESSION_TOKEN_COOKIE) + r"=([^;\s]+)", text)
        if m:
            text = m.group(1)

    text = text.strip().strip('"').strip("'")
    if re.search(r"[\u4e00-\u9fff\uFF00-\uFFEF\u2026]", text):
        raise ChatGptError(
            "gpt_session_invalid_chars",
            "credential contains CJK ideographs / full-width chars - copy was truncated",
        )
    if text.count(".") != 4:
        raise ChatGptError(
            "gpt_session_format_invalid",
            "not a 5-segment JWE session token",
        )
    return text


class ChatGptSession:
    """A ChatGPT login state, usable headlessly.

    The platform's /gpt/session-to-cookie endpoint does exactly this: it turns
    the sessionToken JWE into the cookie header a browser would send, and then
    talks to ChatGPT as that user.
    """

    def __init__(
        self,
        credential: str,
        proxy: Optional[str] = None,
        device_id: Optional[str] = None,
        timeout: int = 60,
    ):
        self.session_token = parse_credential(credential)
        self.device_id = device_id or default_device_id()
        self.timeout = timeout
        self.http = requests.Session()
        self.http.trust_env = False
        if proxy:
            self.http.proxies = {"http": proxy, "https": proxy}
        # Seed the cookie jar instead of overwriting the Cookie header: the
        # server also hands out __cf_bm / csrf cookies, and dropping them makes
        # every request look like a client with no browsing history.
        self.http.cookies.set(SESSION_TOKEN_COOKIE, self.session_token, domain="chatgpt.com", path="/")
        self.http.cookies.set("oai-did", self.device_id, domain="chatgpt.com", path="/")
        self.http.cookies.set("oai-language", "en-US", domain="chatgpt.com", path="/")
        self.http.headers.update(
            {
                "User-Agent": DEFAULT_UA,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": CHATGPT,
                "Referer": CHATGPT + "/",
            }
        )
        self.account_id: Optional[str] = None
        self.access_token: Optional[str] = None
        self._session_doc: Dict[str, Any] = {}

    # ---- plumbing -------------------------------------------------------
    @property
    def cookie_header(self) -> str:
        """Human-readable view of the cookies we would send (for /session-to-cookie)."""
        return (
            f"{SESSION_TOKEN_COOKIE}={self.session_token}; "
            f"oai-did={self.device_id}; "
            "oai-language=en-US; "
            "oai-client-version=prod-unknown"
        )

    def _headers(self, with_auth: bool = True) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if with_auth and self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        if self.account_id:
            h["chatgpt-account-id"] = str(self.account_id)
        h["oai-device-id"] = self.device_id
        h["oai-language"] = "en-US"
        return h

    def request(self, method: str, path: str, **kw) -> requests.Response:
        url = path if path.startswith("http") else CHATGPT + path
        kw.setdefault("timeout", self.timeout)
        resp = self.http.request(method, url, **kw)
        return resp

    def _json(self, method: str, path: str, **kw) -> Dict[str, Any]:
        resp = self.request(method, path, **kw)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code in (401, 403):
            raise ChatGptError(
                "account_check_forbidden",
                f"HTTP {resp.status_code} on {path}: credential rejected",
                resp.status_code,
                data,
            )
        if resp.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            if isinstance(detail, dict):
                detail = detail.get("message") or detail
            raise ChatGptError(
                "checkout_blocked",
                f"HTTP {resp.status_code} on {path}: {detail or data}",
                resp.status_code,
                data,
            )
        return data if isinstance(data, dict) else {}

    # ---- step 1: credential -------------------------------------------------
    def load_session(self) -> Dict[str, Any]:
        """GET /api/auth/session - the document the user is asked to paste."""
        data = self._json(
            "GET",
            "/api/auth/session",
            allow_redirects=False,
        )
        if not data.get("accessToken"):
            raise ChatGptError(
                "account_check_unauthorized",
                "session has no accessToken - credential expired or wrong token",
                raw=data,
            )
        self._session_doc = data
        self.access_token = data["accessToken"]
        account = data.get("account") or {}
        if account.get("id"):
            self.account_id = account["id"]
        return data

    def me(self) -> Dict[str, Any]:
        return self._json("GET", "/backend-api/me", headers=self._headers())

    def account_check(self) -> Dict[str, Any]:
        """Plan / subscription state (accounts check v4 is what the web app uses)."""
        return self._json(
            "GET",
            "/backend-api/accounts/check/v4-2023-04-27",
            headers=self._headers(),
        )

    @staticmethod
    def normalise_plan(raw: str) -> str:
        """`chatgptfreeplan` / `chatgptplusplan` / `chatgptproplan` -> free/plus/pro."""
        text = (raw or "").strip().lower()
        if not text:
            return "free"
        if text.startswith("chatgpt"):
            text = text[len("chatgpt"):]
        if text.endswith("plan"):
            text = text[: -len("plan")]
        return text or "free"

    def current_plan(self) -> Dict[str, Any]:
        """Normalise the plan info into {plan, active_until, will_renew, processor, delinquent}."""
        doc = self.account_check()
        out: Dict[str, Any] = {
            "plan": "free",
            "raw_plan": None,
            "active_until": None,
            "will_renew": None,
            "processor": None,
            "delinquent": None,
            "raw_keys": list(doc.keys()),
        }
        accounts = doc.get("accounts") or {}
        default = doc.get("account_ordering") or []
        acc = accounts.get(default[0]) if default and isinstance(accounts, dict) else None
        if isinstance(acc, dict):
            ent = acc.get("entitlement") or {}
            raw_plan = (ent.get("subscription_plan") or "").strip()
            out["raw_plan"] = raw_plan or None
            out["plan"] = self.normalise_plan(raw_plan)
            out["active_until"] = ent.get("expires_at") or ent.get("renews_at")
            out["will_renew"] = ent.get("renews_at") is not None
            out["processor"] = ent.get("billing_processor") or ent.get("processor")
            out["delinquent"] = ent.get("is_delinquent")
        return out

    # ---- step 2: checkout ---------------------------------------------------
    def build_checkout_payload(
        self,
        plan: str = "plus",
        country: str = "PH",
        currency: str = "PHP",
        ui_mode: str = "hosted",
    ) -> Dict[str, Any]:
        """Byte-for-byte copy of the platform's Pe() payload builder."""
        if plan not in PLANS:
            raise ChatGptError("plan_not_found", f"unknown plan {plan!r}")
        spec = PLANS[plan]
        long_mode = ui_mode == "custom" and "team_plan_data" not in spec

        payload: Dict[str, Any] = {
            "entry_point": "settings_upsell" if long_mode else spec["entry_point"],
            "plan_name": spec["plan_name"],
            "billing_details": {"country": country, "currency": currency},
            "checkout_ui_mode": "custom" if long_mode else "hosted",
        }
        if "team_plan_data" in spec:
            payload["team_plan_data"] = dict(spec["team_plan_data"])
        elif not long_mode:
            payload["price_interval"] = "month"
            payload["selected_cadence"] = "month"
        if spec["plan_name"] == "chatgptplusplan":
            payload["promo_campaign"] = dict(spec["promo"])
        return payload

    def create_checkout(
        self,
        plan: str = "plus",
        country: str = "PH",
        currency: str = "PHP",
        ui_mode: str = "hosted",
    ) -> CheckoutResult:
        if not self.access_token:
            self.load_session()
        payload = self.build_checkout_payload(plan, country, currency, ui_mode)
        resp = self.request(
            "POST",
            "/backend-api/payments/checkout",
            headers={**self._headers(), "Content-Type": "application/json"},
            data=json.dumps(payload),
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not resp.ok:
            detail = data.get("detail") if isinstance(data, dict) else None
            if isinstance(detail, dict):
                detail = detail.get("message") or detail
            detail = str(detail or data or "")
            if re.search(r"unusual activity", detail, re.I):
                raise ChatGptError(
                    "gpt_risk_blocked",
                    "ChatGPT flagged unusual activity for this account",
                    resp.status_code,
                    data,
                )
            if re.search(r"already|subscri", detail, re.I):
                raise ChatGptError(
                    "already_subscribed", detail, resp.status_code, data
                )
            raise ChatGptError(
                "checkout_created_timeout" if resp.status_code >= 500 else "checkout_blocked",
                f"HTTP {resp.status_code}: {detail}",
                resp.status_code,
                data,
            )

        sid = str(
            data.get("checkout_session_id")
            or (data.get("checkout_session") or {}).get("id")
            or data.get("session_id")
            or ""
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", sid or ""):
            raise ChatGptError("gpt_checkout_link_not_long", "response has no session id", raw=data)
        entity = str(data.get("processor_entity") or "openai_llc").strip() or "openai_llc"

        if ui_mode == "custom":
            raw_url = str(
                data.get("url") or data.get("checkout_url") or data.get("openai_checkout_url") or ""
            ).strip()
            if raw_url.startswith("https://checkout.stripe.com/"):
                link = raw_url
            elif sid.startswith("cs_"):
                link = "https://checkout.stripe.com/c/pay/" + sid
            else:
                # upstream answered with a ChatGPT session, not a Stripe one
                raise ChatGptError(
                    "gpt_checkout_link_not_long",
                    "upstream returned a ChatGPT session instead of a Stripe session",
                    raw=data,
                )
            mode = "custom"
        else:
            link = f"{CHATGPT}/checkout/{entity}/{sid}"
            mode = "hosted"

        return CheckoutResult(sid, entity, mode, link, data)

    def checkout_link(self, **kw) -> str:
        """Same as create_checkout but with the platform's long->short fallback."""
        try:
            return self.create_checkout(ui_mode="custom", **kw).link
        except ChatGptError as exc:
            if exc.code != "gpt_checkout_link_not_long":
                raise
            return self.create_checkout(ui_mode="hosted", **kw).link

    # ---- step 3: price / amount -------------------------------------------
    def quoted_amount(self, result: CheckoutResult) -> Optional[Dict[str, Any]]:
        """Amount for the freshly created session, taken from the create response.

        The platform shows "實時報價" before the card is charged and refuses to
        submit when the quote is missing ("報價異常會攔下不扣款").
        """
        data = result.raw or {}
        node = data.get("checkout_session") if isinstance(data.get("checkout_session"), dict) else data
        for key in ("amount_total", "amount", "amount_due", "total"):
            if isinstance(node, dict) and node.get(key) is not None:
                return {
                    "amount_minor": node.get(key),
                    "currency": node.get("currency") or result.raw.get("currency"),
                }
        return None

    # ---- step 4: activation -------------------------------------------------
    def wait_for_activation(self, timeout: int = 180, interval: int = 5) -> Dict[str, Any]:
        """Poll the subscription until it is active (or give up).

        the platform's own vocabulary for the two outcomes is
        `paid_success` / `activation_unconfirmed`.
        """
        deadline = time.time() + timeout
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            try:
                last = self.current_plan()
            except ChatGptError:
                last = {}
            if last.get("plan") not in (None, "", "free"):
                return last
            time.sleep(interval)
        return last or {"plan": "free", "activation": "unconfirmed"}

    def cancel_renewal(self, subscription_id: str = "") -> Dict[str, Any]:
        """Turn off auto-renew (the platform's '取消續費' step).

        NOTE: OpenAI has no public documentation for this route, so several
        candidate paths are tried in order and the one that answered is
        reported back. The platform's own pipeline treats this as a mandatory
        stage (`renewal`) precisely because a missed cancel charges the card
        again next month.
        """
        if not self.access_token:
            self.load_session()

        state = self.current_plan()
        sub_id = subscription_id or str(state.get("subscription_id") or "")

        candidates = [
            ("/backend-api/payments/subscription/cancel", {"subscription_id": sub_id}),
            ("/backend-api/subscriptions/cancel", {"subscription_id": sub_id}),
            ("/backend-api/payments/subscription/cancel", {}),
            ("/backend-api/subscriptions/cancel", {}),
        ]
        attempts = []
        for path, body in candidates:
            try:
                resp = self.request(
                    "POST",
                    path,
                    headers={**self._headers(), "Content-Type": "application/json"},
                    data=json.dumps(body),
                )
            except Exception as exc:  # noqa: BLE001
                attempts.append({"path": path, "error": str(exc)})
                continue
            attempts.append({"path": path, "status": resp.status_code,
                             "body": resp.text[:300]})
            if resp.ok:
                after = self.current_plan()
                return {
                    "ok": True,
                    "path": path,
                    "response": resp.text[:300],
                    "will_renew_before": state.get("will_renew"),
                    "will_renew_after": after.get("will_renew"),
                    "plan_after": after.get("plan"),
                    "attempts": attempts,
                }
        return {
            "ok": False,
            "will_renew_before": state.get("will_renew"),
            "plan": state.get("plan"),
            "attempts": attempts,
            "hint": "no candidate route accepted; cancel from Settings -> Subscription in a browser",
        }
