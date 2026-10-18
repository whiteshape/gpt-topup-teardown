"""Order state machine - the "protocol direct top-up" pipeline.

Stage/error vocabulary is the platform's own (read off its bundle and its live API),
so an order produced here can be compared with theirs field by field:

    queued -> credential_check -> pricing -> checkout -> payment
           -> subscription -> reconcile -> (renewal) -> completed

Failure classes the platform distinguishes, and which we keep distinct:

    account_check_forbidden / subscription_unauthorized   credential dead
    gpt_risk_blocked        OpenAI "unusual activity"
    already_subscribed      same or higher plan already active
    store_subscription_processor  plan is billed by App Store / Google Play
    checkout_rate_limited   OpenAI throttling the account
    checkout_pricing_unavailable  no live quote -> refuse to charge
    card_unavailable / bank_declined / merchant_blocked
    cs_live_confirm_incomplete    Stripe confirm did not settle
    browser_adapter_unavailable   oaics session without Playwright
    checkout_blocked        generic upstream block
    activation_unconfirmed  paid, but the plan did not flip (not a payment failure)
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Optional

from . import adapters, cards, chatgpt, dpapi, stripe_pay
from .store import Store

ENGINE_PLAN = {
    "plus": "plus",
    "go": "plus",
    "pro_5x": "pro",
    "pro_20x": "pro",
    "pro_50x": "pro",
    "pro_5x_renew": "pro",
    "pro_20x_renew": "pro",
}

SERVICE_FEE_MINOR = 15  # $0.15, matches the platform's service_fee_usd_minor


class OrderEngine:
    def __init__(
        self,
        store: Store,
        proxy: Optional[str] = None,
        payment_proxy: Optional[str] = None,
        max_amount_minor: int = 0,
        dry_run: bool = False,
    ):
        self.store = store
        self.proxy = proxy
        self.payment_proxy = payment_proxy or proxy
        self.max_amount_minor = max_amount_minor
        self.dry_run = dry_run

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()[:32]

    def _fail(self, order_id: int, stage: str, code: str, message: str) -> Dict[str, Any]:
        self.store.update_order(
            order_id,
            stage=stage,
            purchase_state="FAILED",
            error_code=code,
            error_message=message[:500],
            session_enc="",
        )
        self.store.log(order_id, stage, message, level="error", data={"code": code})
        return {"ok": False, "order_id": order_id, "code": code, "message": message}

    # ---- read-only preflight -------------------------------------------
    def preflight(
        self,
        credential: str,
        plan: str = "plus",
        country: str = "PH",
        currency: str = "PHP",
        with_quote: bool = True,
    ) -> Dict[str, Any]:
        """Credential check + account state + optional live quote.

        Nothing is charged here; this is the platform's "檢查憑據" step.
        """
        sess = chatgpt.ChatGptSession(credential, proxy=self.proxy)
        sess.load_session()
        out: Dict[str, Any] = {"ok": True}
        try:
            me = sess.me()
        except chatgpt.ChatGptError as exc:
            me = {}
            out["me_error"] = {"code": exc.code, "message": exc.message}
        out["account"] = {
            "email": me.get("email", ""),
            "id": me.get("id", ""),
            "plan_type": me.get("plan_type", ""),
        }
        try:
            state = sess.current_plan()
        except chatgpt.ChatGptError as exc:
            state = {"plan": "unknown", "error": exc.code, "message": exc.message}
        out["subscription"] = state
        out["preflight_token"] = self.fingerprint(sess.session_token)
        out["quotes"] = {}

        if state.get("plan") not in ("free", "", None):
            out["already_subscribed"] = True
            out["can_purchase_at"] = state.get("active_until")

        if with_quote and not state.get("delinquent"):
            try:
                res = sess.create_checkout(
                    plan=ENGINE_PLAN.get(plan, "plus"),
                    country=country,
                    currency=currency,
                    ui_mode="hosted",
                )
                quote = sess.quoted_amount(res) or {}
                out["quotes"][plan] = {
                    "checkout_session_id": res.session_id,
                    "processor_entity": res.processor_entity,
                    "link": res.link,
                    "currency": quote.get("currency") or currency,
                    "amount_minor": quote.get("amount_minor"),
                }
                out["payment_path"] = adapters.route_for(res.session_id)
            except chatgpt.ChatGptError as exc:
                out["quotes"][plan] = {"error": exc.code, "message": exc.message}
        return out

    # ---- full pipeline --------------------------------------------------
    def run(
        self,
        credential: str,
        plan: str = "plus",
        user_id: int = 1,
        country: str = "PH",
        currency: str = "PHP",
        card_pipe: str = "",
        card_id: Optional[int] = None,
        cdk_code: str = "",
        cancel_renewal: bool = False,
        activate_timeout: int = 180,
        authorized_by: str = "",
        on_order_created=None,
    ) -> Dict[str, Any]:
        token = chatgpt.parse_credential(credential)
        fingerprint = self.fingerprint(token)
        order_id = self.store.create_order(
            user_id=user_id,
            plan=plan,
            session_fp=fingerprint,
            session_enc=dpapi.protect(token),
            authorized_by=authorized_by or f"user:{user_id}",
            payment_country=country,
            payment_currency=currency,
            client_request_id=f"local-{int(time.time())}",
        )
        if on_order_created:
            on_order_created(order_id)
        self.store.log(order_id, "queued", "order created", data={"plan": plan, "country": country})
        self.store.audit(user_id, "order.create", f"order={order_id} plan={plan} country={country}")

        # --- 1. credential ------------------------------------------------
        self.store.update_order(order_id, stage="credential_check", funding_state="NONE")
        try:
            sess = chatgpt.ChatGptSession(token, proxy=self.proxy)
            sess.load_session()
        except chatgpt.ChatGptError as exc:
            return self._fail(order_id, "credential_check", exc.code, exc.message)

        account_email = ""
        try:
            me = sess.me()
            account_email = me.get("email", "")
            self.store.update_order(order_id, account_email=account_email)
            self.store.log(order_id, "credential_check", "credential valid", data={"email": account_email})
        except chatgpt.ChatGptError as exc:
            return self._fail(order_id, "credential_check", exc.code, exc.message)

        try:
            state = sess.current_plan()
            self.store.log(order_id, "credential_check", f"plan={state.get('plan')}", data=state)
            if state.get("plan") not in ("free", "", None) and not plan.endswith("_renew"):
                return self._fail(
                    order_id,
                    "credential_check",
                    "already_subscribed",
                    f"account already on {state.get('plan')} until {state.get('active_until')}",
                )
            if state.get("processor") and "store" in str(state.get("processor", "")).lower():
                return self._fail(
                    order_id,
                    "credential_check",
                    "store_subscription_processor",
                    "subscription is managed by App Store / Google Play",
                )
        except chatgpt.ChatGptError as exc:
            self.store.log(order_id, "credential_check", f"plan read failed: {exc.code}", level="warn")

        # --- 2. pricing + checkout ---------------------------------------
        self.store.update_order(order_id, stage="pricing", purchase_state="SUBMITTED")
        engine_plan = ENGINE_PLAN.get(plan, "plus")
        try:
            try:
                result = sess.create_checkout(engine_plan, country, currency, ui_mode="custom")
            except chatgpt.ChatGptError as exc:
                if exc.code != "gpt_checkout_link_not_long":
                    raise
                result = sess.create_checkout(engine_plan, country, currency, ui_mode="hosted")
        except chatgpt.ChatGptError as exc:
            return self._fail(order_id, "checkout", exc.code, exc.message)

        path = adapters.route_for(result.session_id)
        quote = sess.quoted_amount(result) or {}
        amount = int(quote.get("amount_minor") or 0)
        self.store.update_order(
            order_id,
            stage="checkout",
            payment_path=path,
            checkout_session=result.session_id,
            checkout_link=result.link,
            amount_minor=amount,
            currency=quote.get("currency") or currency,
        )
        self.store.log(order_id, "checkout", f"{path} session {result.session_id}")

        if amount == 0:
            return self._fail(
                order_id, "pricing", "checkout_pricing_unavailable", "no live quote from upstream"
            )
        if self.max_amount_minor and amount > self.max_amount_minor:
            return self._fail(
                order_id,
                "pricing",
                "amount_range",
                f"quoted {amount} above plan ceiling {self.max_amount_minor}",
            )

        if self.dry_run:
            self.store.update_order(order_id, stage="completed", purchase_state="CONFIRMED", session_enc="")
            self.store.log(order_id, "reconcile", "dry-run: link returned, nothing charged")
            return {
                "ok": True,
                "dry_run": True,
                "order_id": order_id,
                "payment_path": path,
                "checkout_link": result.link,
                "amount_minor": amount,
                "currency": quote.get("currency") or currency,
            }

        # --- 3. card ------------------------------------------------------
        self.store.update_order(order_id, stage="payment", funding_state="FUNDING_REQUIRED")
        if card_pipe:
            card = stripe_pay.Card.from_pipe(card_pipe)
            card_row: Dict[str, Any] = {}
        else:
            card_row = cards.select(self.store, plan=plan, user_id=user_id, card_id=card_id) or {}
            if not card_row:
                return self._fail(order_id, "payment", "card_unavailable", "no usable card in pool")
            try:
                card = cards.row_to_card(card_row)
            except dpapi.DpapiError as exc:
                return self._fail(order_id, "payment", "card_unavailable", f"card decrypt failed: {exc}")
            if not card.cvc:
                return self._fail(order_id, "payment", "card_unavailable", "stored card has no CVC")
            card_id = card_row["id"]
            self.store.update_order(order_id, card_id=card_id)
        self.store.update_order(order_id, funding_state="FUNDED")
        self.store.log(order_id, "payment", f"card ending {card.last4} bin {card.number[:6]}")

        # --- 4. pay -------------------------------------------------------
        adapter = adapters.adapter_for(result.session_id, proxy=self.payment_proxy)
        try:
            outcome = adapter.pay(result.session_id, card, amount, quote.get("currency") or currency)
        except adapters.PaymentRouteUnsupported as exc:
            return self._fail(order_id, "payment", exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            self.store.record_bin(card.number[:6], plan, False, str(exc)[:120], amount, currency)
            return self._fail(order_id, "payment", "cs_live_confirm_incomplete", str(exc)[:300])

        ok = outcome.status == "paid"
        if card_row:
            cards.record(
                self.store, card_row, plan, ok,
                error="" if ok else (outcome.decline_code or outcome.status),
                amount_minor=amount, currency=quote.get("currency") or currency,
            )

        if outcome.status == "declined":
            self.store.update_order(
                order_id, stage="payment", purchase_state="FAILED",
                error_code="bank_declined", error_message=outcome.decline_code,
            )
            self.store.log(order_id, "payment", f"declined ({outcome.decline_code})", level="error")
            return {"ok": False, "order_id": order_id, "code": "bank_declined",
                    "decline_code": outcome.decline_code}
        if outcome.status == "requires_action":
            self.store.update_order(
                order_id, stage="payment", purchase_state="PENDING",
                error_code="cs_live_confirm_incomplete",
            )
            self.store.log(order_id, "payment", "3DS challenge required", level="warn", data=outcome.next_action)
            return {"ok": False, "order_id": order_id, "code": "requires_action",
                    "next_action": outcome.next_action,
                    "hint": "cardholder authentication must be completed in a browser"}
        if not ok:
            return self._fail(order_id, "payment", "cs_live_confirm_incomplete", f"status={outcome.status}")

        self.store.update_order(order_id, stage="subscription", purchase_state="PENDING")
        self.store.log(order_id, "payment", f"paid intent={outcome.payment_intent}")

        # --- 5. activation ------------------------------------------------
        activated = sess.wait_for_activation(timeout=activate_timeout)
        if activated.get("plan") in ("free", "", None) or activated.get("activation") == "unconfirmed":
            self.store.update_order(
                order_id, stage="reconcile", purchase_state="UNKNOWN",
                error_code="activation_unconfirmed",
            )
            self.store.log(order_id, "reconcile", "charged but plan not active yet",
                           level="warn", data=activated)
            return {"ok": True, "order_id": order_id, "code": "activation_unconfirmed",
                    "subscription": activated}

        self.store.update_order(order_id, stage="subscription", purchase_state="CONFIRMED")
        self.store.log(order_id, "subscription", f"plan active: {activated.get('plan')}", data=activated)

        # --- 6. renewal off / cdk / fee ------------------------------------
        if cancel_renewal:
            try:
                sess.cancel_renewal()
                self.store.log(order_id, "renewal", "auto-renew cancelled")
            except chatgpt.ChatGptError as exc:
                self.store.log(order_id, "renewal", f"cancel failed: {exc.code}", level="warn")

        if cdk_code:
            try:
                self.store.consume_cdk(cdk_code, order_id)
                self.store.log(order_id, "completed", f"cdk {cdk_code} consumed")
            except Exception as exc:  # noqa: BLE001
                self.store.log(order_id, "completed", f"cdk consume failed: {exc}", level="warn")

        if not cdk_code and user_id:
            self.store.adjust_balance(user_id, -SERVICE_FEE_MINOR, "service_fee", f"order={order_id}")

        self.store.update_order(
            order_id, stage="completed", purchase_state="CONFIRMED", session_enc=""
        )
        row = self.store.one("SELECT order_no FROM orders WHERE id=?", (order_id,))
        return {
            "ok": True,
            "order_id": order_id,
            "order_no": row["order_no"] if row else "",
            "payment_path": path,
            "amount_minor": amount,
            "currency": quote.get("currency") or currency,
            "subscription": activated,
        }
