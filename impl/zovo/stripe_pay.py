"""Paying a checkout session with a card - the funding leg of a 代充.

Two observations drive this module:

1. `checkout_ui_mode` only changes *presentation*. Whether the caller asks for
   "hosted" (short link) or "custom" (long link), the object that actually has
   to be paid is a Stripe Checkout Session whose id starts with `cs_live_`.
   The long link just hands that session to Stripe's own hosted page; the short
   link wraps the same session in OpenAI's page.
2. A Stripe Checkout Session can be paid without a browser through Stripe's own
   front-end endpoints (`/v1/payment_pages/{id}/init` and `/confirm`). That is
   what makes "15 秒极速代充" possible, and it is why the platform needs a pool
   of cards plus residential egress instead of a headless browser farm.

Everything here is the documented Stripe front-end protocol. Two caveats are
real and are not papered over:

* Stripe rotates auxiliary fields (`client_attribution_metadata`, `guid`,
  `muid`, `sid`). When a field is missing the API answers with
  `error.param`, so `confirm()` echoes Stripe's own complaint instead of
  guessing.
* Cardholder authentication (3DS / `next_action`) cannot be completed headless
  without the issuer's challenge. `payment_status == "requires_action"` is
  returned as a first-class outcome, not swallowed.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests

STRIPE_API = "https://api.stripe.com/v1"
CHECKOUT_PAGE = "https://checkout.stripe.com/c/pay/"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class StripePaymentError(RuntimeError):
    def __init__(self, code: str, message: str = "", param: str = "", raw: Any = None):
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.param = param
        self.raw = raw


@dataclass
class Card:
    number: str
    exp_month: str
    exp_year: str
    cvc: str
    name: str = ""
    email: str = ""
    country: str = "US"
    line1: str = ""
    line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    phone: str = ""

    @classmethod
    def from_pipe(cls, raw: str, **overrides) -> "Card":
        """Parse `number|mm|yy|cvv` (the format the platform's raw-card field uses)."""
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 4:
            raise StripePaymentError("card_format", "expected number|mm|yy|cvv")
        card = cls(
            number=re.sub(r"\D", "", parts[0]),
            exp_month=parts[1].zfill(2),
            exp_year=parts[2][-2:] if len(parts[2]) == 4 else parts[2].zfill(2),
            cvc=parts[3],
        )
        for key, value in overrides.items():
            if value and hasattr(card, key):
                setattr(card, key, value)
        return card

    @property
    def last4(self) -> str:
        return self.number[-4:]

    @property
    def brand(self) -> str:
        n = self.number
        if n.startswith("4"):
            return "visa"
        if n[:2] in {"51", "52", "53", "54", "55"} or (n[:4].isdigit() and 2221 <= int(n[:4]) <= 2720):
            return "mastercard"
        if n[:2] in {"34", "37"}:
            return "amex"
        if n[:4] in {"6011"} or n[:2] == "65":
            return "discover"
        if n[:2] == "62":
            return "unionpay"
        return "unknown"


@dataclass
class PaymentResult:
    status: str                 # paid | requires_action | declined | failed
    payment_intent: str = ""
    next_action: Optional[Dict[str, Any]] = None
    decline_code: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def _uuid() -> str:
    return str(uuid.uuid4())


class StripeCheckoutPayer:
    """Pays a `cs_live_...` Checkout Session with a card, no browser."""

    def __init__(
        self,
        proxy: Optional[str] = None,
        timeout: int = 60,
        locale: str = "en-US",
    ):
        self.http = requests.Session()
        self.http.trust_env = False
        if proxy:
            self.http.proxies = {"http": proxy, "https": proxy}
        self.timeout = timeout
        self.locale = locale
        self.http.headers.update(
            {
                "User-Agent": DEFAULT_UA,
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://checkout.stripe.com",
            }
        )

    # ---- key discovery --------------------------------------------------
    def publishable_key(self, session_id: str) -> str:
        """Read the merchant publishable key off the hosted checkout page.

        Stripe embeds it in the page payload; it is per-session and is what
        every front-end call must be signed with.
        """
        resp = self.http.get(
            CHECKOUT_PAGE + session_id,
            timeout=self.timeout,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        body = resp.text
        for pattern in (
            r'"publishableKey"\s*:\s*"(pk_[A-Za-z0-9_]+)"',
            r'"publishable_key"\s*:\s*"(pk_[A-Za-z0-9_]+)"',
            r'(pk_live_[A-Za-z0-9]{20,})',
            r'(pk_test_[A-Za-z0-9]{20,})',
        ):
            m = re.search(pattern, body)
            if m:
                return m.group(1)
        raise StripePaymentError(
            "publishable_key_not_found",
            "could not read pk_ from the checkout page (session expired or bot-walled)",
        )

    # ---- step 1: init ---------------------------------------------------
    def init(self, session_id: str, key: str) -> Dict[str, Any]:
        params = {
            "key": key,
            "eid": "NA",
            "browser_locale": self.locale,
            "browser_timezone": "Asia/Shanghai",
            "redirect_type": "url",
        }
        resp = self.http.get(
            f"{STRIPE_API}/payment_pages/{session_id}/init",
            params=params,
            timeout=self.timeout,
            headers={"Accept": "application/json"},
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            err = (data.get("error") or {})
            raise StripePaymentError(
                err.get("code") or "init_failed",
                err.get("message") or f"HTTP {resp.status_code}",
                err.get("param", ""),
                data,
            )
        return data

    # ---- step 2: confirm ------------------------------------------------
    def confirm(
        self,
        session_id: str,
        key: str,
        card: Card,
        init_data: Optional[Dict[str, Any]] = None,
        expected_amount: Optional[int] = None,
        extra: Optional[Dict[str, str]] = None,
    ) -> PaymentResult:
        init_data = init_data or {}
        amount = expected_amount or init_data.get("amount") or init_data.get("total")
        client_session_id = _uuid()

        form: Dict[str, str] = {
            "key": key,
            "eid": "NA",
            "init_checksum": str(init_data.get("init_checksum") or ""),
            "expected_amount": str(amount or ""),
            "expected_payment_method_type": "card",
            "payment_method_data[type]": "card",
            "payment_method_data[card][number]": card.number,
            "payment_method_data[card][exp_month]": card.exp_month,
            "payment_method_data[card][exp_year]": card.exp_year,
            "payment_method_data[card][cvc]": card.cvc,
            "payment_method_data[allow_redisplay]": "always",
            "payment_method_data[billing_details][name]": card.name or "John Smith",
            "payment_method_data[billing_details][email]": card.email,
            "payment_method_data[billing_details][address][country]": card.country,
            "payment_method_data[billing_details][address][line1]": card.line1,
            "payment_method_data[billing_details][address][city]": card.city,
            "payment_method_data[billing_details][address][postal_code]": card.postal_code,
            "payment_method_data[guid]": _uuid(),
            "payment_method_data[muid]": _uuid(),
            "payment_method_data[sid]": _uuid(),
            "payment_method_data[time_on_page]": "42000",
            "client_attribution_metadata[client_session_id]": client_session_id,
            "client_attribution_metadata[checkout_session_id]": session_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_subtype]": "checkout_hosted",
            "client_attribution_metadata[merchant_integration_version]": "hosted",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "consent[terms_of_service]": "accepted",
        }
        if card.state:
            form["payment_method_data[billing_details][address][state]"] = card.state
        if card.line2:
            form["payment_method_data[billing_details][address][line2]"] = card.line2
        if card.phone:
            form["payment_method_data[billing_details][phone]"] = card.phone
        if init_data.get("config_id"):
            form["config_id"] = str(init_data["config_id"])
        if init_data.get("payment_method_types"):
            types = init_data["payment_method_types"]
            if isinstance(types, list) and types and "card" not in types:
                raise StripePaymentError(
                    "card_not_offered",
                    f"session does not accept card (offers: {','.join(map(str, types))})",
                )
        if extra:
            form.update({k: str(v) for k, v in extra.items()})

        resp = self.http.post(
            f"{STRIPE_API}/payment_pages/{session_id}/confirm",
            data=form,
            timeout=self.timeout,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": CHECKOUT_PAGE + session_id,
            },
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            err = data.get("error") or {}
            code = err.get("code") or "confirm_failed"
            message = err.get("message") or f"HTTP {resp.status_code}"
            if code == "card_declined":
                return PaymentResult("declined", decline_code=err.get("decline_code", ""), raw=data)
            raise StripePaymentError(code, message, err.get("param", ""), data)

        status = str(data.get("payment_status") or data.get("status") or "")
        if status in ("paid", "succeeded", "complete"):
            return PaymentResult("paid", payment_intent=str(data.get("payment_intent") or ""), raw=data)

        next_action = data.get("next_action")
        if next_action or status == "requires_action":
            return PaymentResult(
                "requires_action",
                payment_intent=str(data.get("payment_intent") or ""),
                next_action=next_action,
                raw=data,
            )
        err = data.get("error") or {}
        if err.get("code") == "card_declined":
            return PaymentResult("declined", decline_code=err.get("decline_code", ""), raw=data)
        return PaymentResult("failed", raw=data)

    # ---- one-shot -------------------------------------------------------
    def pay(self, session_id: str, card: Card, key: Optional[str] = None, **kw) -> PaymentResult:
        key = key or self.publishable_key(session_id)
        init_data = self.init(session_id, key)
        if str(init_data.get("status") or "") == "complete":
            return PaymentResult("paid", raw=init_data)
        time.sleep(1.5)  # let the session settle; keeps `init_checksum` valid
        return self.confirm(session_id, key, card, init_data, **kw)


def pay_hosted_session(
    session_id: str,
    card: Card,
    proxy: Optional[str] = None,
) -> PaymentResult:
    """Convenience wrapper: short link -> hosted page -> Stripe session -> paid."""
    return StripeCheckoutPayer(proxy=proxy).pay(session_id, card)
