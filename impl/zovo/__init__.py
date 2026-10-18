"""the platform GPT direct-topup reconstruction.

  chatgpt.py    ChatGPT session + official checkout protocol (the "协议")
  stripe_pay.py paying a cs_live_ Checkout Session with a card, headless
  store.py      SQLite: cards / cdks / orders, the platform's own vocabulary
  orders.py     order state machine (credential -> pricing -> checkout ->
                payment -> subscription -> reconcile)
  api.py        HTTP service exposing the same /api/v1 contract
  cli.py        manual, step-by-step driver
"""

__all__ = ["chatgpt", "stripe_pay", "store", "orders", "api", "cli"]
__version__ = "1.0.0"
