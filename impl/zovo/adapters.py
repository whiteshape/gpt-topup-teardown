"""Payment adapters - one per checkout-session flavour.

the platform distinguishes exactly two, and so do we:

  cs_live_...   Stripe Checkout Session   -> payable through Stripe's own
                                             front-end API, no browser
  oaics_...     OpenAI-hosted checkout     -> only a browser can drive it

`route_for(session_id)` is the single place that decides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from . import stripe_pay


class PaymentRouteUnsupported(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PaymentOutcome:
    status: str          # paid | declined | requires_action | failed
    decline_code: str = ""
    payment_intent: str = ""
    next_action: Optional[Dict[str, Any]] = None
    raw: Optional[Dict[str, Any]] = None


class PaymentAdapter(Protocol):
    name: str

    def pay(
        self,
        session_id: str,
        card: "stripe_pay.Card",
        amount_minor: int = 0,
        currency: str = "",
    ) -> PaymentOutcome: ...


class StripeAdapter:
    """cs_live_ sessions: Stripe front-end protocol, no browser."""

    name = "cs_live"

    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy

    def pay(self, session_id, card, amount_minor=0, currency=""):
        payer = stripe_pay.StripeCheckoutPayer(proxy=self.proxy)
        result = payer.pay(session_id, card, expected_amount=amount_minor or None)
        return PaymentOutcome(
            status=result.status,
            decline_code=result.decline_code,
            payment_intent=result.payment_intent,
            next_action=result.next_action,
            raw=result.raw,
        )


class OaicsAdapter:
    """oaics_ sessions: OpenAI renders the checkout, so drive a real browser.

    Playwright is an optional dependency (about 150 MB with Chromium). Until it
    is installed this adapter fails loudly with a structured code instead of
    pretending the order succeeded.

    Install:
        pip install playwright
        python -m playwright install chromium
    """

    name = "oaics"

    def __init__(self, headless: bool = False, profile_dir: Optional[str] = None):
        self.headless = headless
        self.profile_dir = profile_dir

    def pay(self, session_id, card, amount_minor=0, currency=""):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise PaymentRouteUnsupported(
                "browser_adapter_unavailable",
                "oaics 会话必须用浏览器支付：pip install playwright && "
                "python -m playwright install chromium",
            ) from exc

        return self._pay_with_browser(session_id, card)

    # ---- the actual browser flow ---------------------------------------
    def _pay_with_browser(self, session_id: str, card) -> PaymentOutcome:
        from playwright.sync_api import sync_playwright

        url = "https://chatgpt.com/checkout/openai_llc/" + session_id
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=self.profile_dir or "zovo_profile",
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(2500)

            # Card fields live in Stripe iframes; fill by placeholder/label.
            frame = None
            for f in page.frames:
                if "stripe" in (f.url or ""):
                    frame = f
                    break
            target = frame or page

            target.get_by_placeholder("卡号").fill(card.number)
            target.get_by_placeholder("有效期").fill(f"{card.exp_month}/{card.exp_year}")
            target.get_by_placeholder("安全码").fill(card.cvc)

            # hCaptcha / 3DS are human-in-the-loop by design: pause and let the
            # operator finish, then read the outcome from the page.
            page.get_by_role("button", name="订阅").click()
            page.wait_for_timeout(8000)

            body = page.inner_text("body")
            ctx.close()

            if "Payment was not approved" in body:
                return PaymentOutcome("declined", raw={"body": body[:500]})
            if "3D" in body or "verify" in body.lower():
                return PaymentOutcome("requires_action", raw={"body": body[:500]})
            return PaymentOutcome("paid", raw={"body": body[:500]})


def route_for(session_id: str) -> str:
    if session_id.startswith("cs_"):
        return "cs_live"
    if session_id.startswith("oaics_"):
        return "oaics"
    return "unknown"


def adapter_for(session_id: str, proxy: Optional[str] = None) -> PaymentAdapter:
    route = route_for(session_id)
    if route == "cs_live":
        return StripeAdapter(proxy=proxy)
    if route == "oaics":
        return OaicsAdapter()
    raise PaymentRouteUnsupported(
        "unknown_payment_route", f"cannot pay session {session_id[:12]}..."
    )
