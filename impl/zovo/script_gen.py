"""Generator for the "F12 取链脚本" - the operator-facing browser snippet.

the platform hands its downstream sellers a script to paste into a *user's own*
logged-in chatgpt.com tab. The script reads the login state from the page and
calls the official checkout API, so nobody has to hand over a session token.
This module emits the same two variants (short link / long link) with the same
payload and the same fallback behaviour.

Differences from the original are cosmetic only:
  * strings are English instead of the site's zh-TW,
  * AUTO_OPEN is a parameter.
"""

from __future__ import annotations

import json

from .chatgpt import PLANS

SCRIPT_HEADER = "// ChatGPT {kind} link script - plan {plan} - {country}/{currency}"
BADGE = "// Run inside an already logged-in chatgpt.com tab."


def build_payload(plan: str, country: str, currency: str, ui_mode: str) -> dict:
    spec = PLANS[plan]
    long_mode = ui_mode == "custom" and "team_plan_data" not in spec
    payload = {
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


def build_script(
    plan: str = "plus",
    country: str = "PH",
    currency: str = "PHP",
    mode: str = "short",
    auto_open: bool = True,
) -> str:
    ui_mode = "custom" if mode == "long" else "hosted"
    payload_json = json.dumps(build_payload(plan, country, currency, ui_mode), indent=2)
    payload_json = "\n".join(
        line if i == 0 else "  " + line for i, line in enumerate(payload_json.splitlines())
    )

    if mode == "long":
        link_block = """
    const raw = String(data.url || data.checkout_url || data.openai_checkout_url || '').trim();
    let link = '';
    if (raw.indexOf('https://checkout.stripe.com/') === 0) link = raw;
    else if (sid.indexOf('cs_') === 0) link = 'https://checkout.stripe.com/c/pay/' + sid;
    if (!link) throw new Error('no Stripe long link this time (upstream returned a ChatGPT session)');"""
        extra_headers = ""
    else:
        link_block = "    const link = 'https://chatgpt.com/checkout/' + entity + '/' + sid;"
        extra_headers = """
    const did = (document.cookie.match(/(?:^|; )oai-did=([^;]+)/) || [])[1];
    if (did) headers['oai-device-id'] = decodeURIComponent(did);
    headers['oai-language'] = 'en-US';"""

    return f"""{SCRIPT_HEADER.format(kind=mode, plan=plan, country=country, currency=currency)}
{BADGE}
(async () => {{
  const AUTO_OPEN = {str(auto_open).lower()};
  const PAYLOAD = {payload_json};
  try {{
    const host = location.hostname;
    if (host !== 'chatgpt.com' && !host.endsWith('.chatgpt.com')) {{
      throw new Error('open and log in to https://chatgpt.com first');
    }}
    const session = await fetch('/api/auth/session', {{ credentials: 'include' }}).then((r) => r.json());
    if (!session || !session.accessToken) throw new Error('no accessToken - log in to ChatGPT first');

    const headers = {{
      Authorization: 'Bearer ' + session.accessToken,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    }};
    const accountId = session.account && session.account.id;
    if (accountId) headers['chatgpt-account-id'] = String(accountId);{extra_headers}

    const resp = await fetch('/backend-api/payments/checkout', {{
      method: 'POST',
      credentials: 'include',
      headers,
      body: JSON.stringify(PAYLOAD),
    }});
    const data = await resp.json().catch(() => ({{}}));
    if (!resp.ok) {{
      const detail = String((data.detail && (data.detail.message || data.detail)) || data.message || '');
      if (/unusual activity/i.test(detail)) {{
        throw new Error('unusual activity: click Upgrade once manually in this tab, then re-run');
      }}
      throw new Error('HTTP ' + resp.status + ' ' + detail);
    }}

    const sid = String(
      data.checkout_session_id || (data.checkout_session && data.checkout_session.id) || data.session_id || ''
    ).trim();
    if (!/^[A-Za-z0-9_-]{{1,256}}$/.test(sid)) throw new Error('response has no checkout_session_id');
    const entity = String(data.processor_entity || 'openai_llc').trim() || 'openai_llc';
{link_block}

    window.__gptPayLink = link;
    console.info('payment link ready:', link);
    try {{ await navigator.clipboard.writeText(link); }} catch (_) {{}}
    if (AUTO_OPEN) location.assign(link);
    return link;
  }} catch (e) {{
    console.error('checkout script failed:', (e && e.message) || e);
  }}
}})();
"""


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="plus", choices=sorted(PLANS))
    ap.add_argument("--country", default="PH")
    ap.add_argument("--currency", default="PHP")
    ap.add_argument("--mode", default="short", choices=["short", "long"])
    ap.add_argument("--no-auto-open", action="store_true")
    a = ap.parse_args()
    print(build_script(a.plan, a.country, a.currency, a.mode, auto_open=not a.no_auto_open))
