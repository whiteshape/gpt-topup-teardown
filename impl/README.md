# impl · 复现实现

```
zovo/
  chatgpt.py      ChatGPT 结账协议引擎（Session→Cookie→校验→报价→取链→激活→取消续费）
  stripe_pay.py   用卡支付 cs_live_ Checkout Session（Stripe 前端协议）
  script_gen.py   生成同款「F12 取链脚本」
  store.py        SQLite：users / cards / cdks / orders / order_events
  orders.py       订单状态机
  api.py          HTTP 服务（同款 /api/v1 契约，stdlib）
  cli.py          命令行分步执行
```

## 常用命令

```powershell
cd "./impl"
$env:ZOVO_PROXY="http://127.0.0.1:7897"          # 出口国家决定报价
$env:ZOVO_PAYMENT_PROXY="http://127.0.0.1:7897"  # 可单独指定支付出口

python -m zovo.cli check           --session "<完整 Session JSON>"
python -m zovo.cli session-to-cookie --session "<完整 Session JSON>"
python -m zovo.cli link     --session "<...>" --plan plus --country PH --currency PHP --long
python -m zovo.cli preflight --session "<...>" --plan plus
python -m zovo.cli pay      --link https://checkout.stripe.com/c/pay/cs_live_xxx --card "4111...|12|30|123"
python -m zovo.cli topup    --session "<...>" --plan plus --card "4111...|12|30|123"
python -m zovo.cli topup    --session "<...>" --plan plus --dry-run     # 只取链，不扣款

python -m zovo.api --port 8850 --proxy http://127.0.0.1:7897
python -m zovo.script_gen --plan plus --mode short
```

`--session` 传 `-` 表示从 stdin 读。

## 已知限制

- Stripe `confirm` 的辅助字段会随版本轮换，缺字段时按返回的 `error.param` 修正。
- 3DS 需要人工完成，返回 `requires_action` 与 `next_action`。
- 开卡（发卡）依赖外部供应商 API，本实现只留接口位，`card_mode=open` 未实现。
- 卡号明文存 SQLite，仅适合本地调试。
