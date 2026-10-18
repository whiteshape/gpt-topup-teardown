# GPT 代充「协议直充」拆解

> **作者立场**
>
> - **解密，不牟利。** 这个仓库的目的只有一个：把"代充"这件事从黑箱变成可验证的公开知识，
>   让普通人知道自己的 Session 交出去意味着什么、钱是怎么走的、风险在哪里。
> - **完全免费。** 不收费、不卖卡、不接单、不做代充服务、不提供任何"成品号"或"可用卡源"。
>   代码和文档采用 MIT 协议，任何人可以自由阅读、复现、批评。
> - **只做技术研究。** 仓库里没有任何真实凭据、卡号或他人账号；所有实测都在作者自己的账号上完成。
> - **不鼓励滥用。** 转售账号权益违反 OpenAI 使用条款；未经同意使用他人 Session 属于账号接管。
>   请只在你拥有授权的账号与卡上操作，并自行承担所在地区的法律与合规责任。

对一个在售 GPT 代充平台的 **只读逆向**：它到底怎么用一个"卡密"把 ChatGPT Plus 开到你账号上，
以及这套机制为什么能成立、成本结构是什么、在真实风控下会在哪里断掉。

附带一份可运行的复现实现，用来证明结论——不是为了教人开黑站。

> 逆向过程中没有下单、没有发起扣款、没有提交任何他人凭据。证据来自公开前端产物、平台自己下发的
> 浏览器脚本，以及登录后页面自带的只读接口返回。

---

## 结论先说

**没有协议后门，没有"劫持支付令牌"，也没有"把别人的会员转给你"。**

它做的事情只有一件：**拿到目标账号的登录态，以该账号身份调用 OpenAI 官方结账接口建单，再用平台自己的卡付款。**

```
客户 Session ──► POST chatgpt.com/backend-api/payments/checkout
                   ├─ checkout_ui_mode=hosted → chatgpt.com/checkout/<entity>/oaics_...
                   └─ checkout_ui_mode=custom → checkout.stripe.com/c/pay/cs_live_...
                          │
                 平台虚拟卡 ──► Stripe payment_pages/{id}/init → /confirm
                          │
                 OpenAI 把订阅记到该账号
```

关键点：**会员归谁，由建单时的账号身份决定**。所以代充必须拿到你的 Session——不是要偷号，
而是技术上没有别的办法把你的账号设成订单主体。钱由谁出是完全独立的另一条链。

---

## 三条链，不是一个漏洞

| 链 | 内容 | 边界 |
| --- | --- | --- |
| **身份链** | 客户提交 `chatgpt.com/api/auth/session` 的完整 JSON（含 `sessionToken` JWE） | 纯 `accessToken`（`eyJ...`）被平台明确拒绝 |
| **结账链** | `POST /backend-api/payments/checkout` | 需要 `billing_details{country,currency}`，决定区域价 |
| **资金链** | 平台虚拟卡 → Stripe 前端接口 | 卡源才是真门槛 |
| **权益链** | OpenAI entitlement 落到账号 | 不可转移，只能"以该身份建单" |

`checkout_ui_mode` 只决定**交付形态**：

- `hosted` → `https://chatgpt.com/checkout/<processor_entity>/<sid>`（OpenAI 托管页，`oaics_` 前缀）
- `custom` → `https://checkout.stripe.com/c/pay/<sid>`（Stripe 直付页，`cs_live_` 前缀）

两者底层是**同一个 Stripe Checkout Session**，所以长链拿不到时可以自动降级短链而不中断。

---

## 协议细节

平台自己会生成一段「F12 取链脚本」给下游，让操作方粘到**客户自己已登录的 chatgpt.com 标签页**里执行。
这是唯一一处把真实请求原样写出来的地方：

```js
const session = await fetch('/api/auth/session', { credentials: 'include' }).then(r => r.json());

const headers = {
  Authorization: 'Bearer ' + session.accessToken,
  'Content-Type': 'application/json',
  Accept: 'application/json',
};
if (session.account?.id) headers['chatgpt-account-id'] = String(session.account.id);
// 仅短链模式：
// headers['oai-device-id'] = <cookie oai-did>;  headers['oai-language'] = 'en-US';

const resp = await fetch('/backend-api/payments/checkout', {
  method: 'POST', credentials: 'include', headers,
  body: JSON.stringify(PAYLOAD),
});
```

`PAYLOAD` 字段：

| 字段 | 取值 |
| --- | --- |
| `entry_point` | `all_plans_pricing_modal` / `team_workspace_purchase_modal` / **`settings_upsell`（长链）** |
| `plan_name` | `chatgptplusplan` / `chatgptpro` / `chatgptprolite` / `chatgptteamplan` |
| `billing_details` | `{country, currency}` —— **决定区域定价** |
| `checkout_ui_mode` | `hosted` / `custom` |
| `price_interval` / `selected_cadence` | `month`（非长链、非 Team） |
| `promo_campaign` | `{promo_campaign_id:"plus-1-month", is_coupon_from_query_param:false}`（仅 Plus） |
| `team_plan_data` | `{workspace_name, price_interval, seat_quantity}`（仅 Team） |

响应里取：

```js
const sid    = data.checkout_session_id || data.checkout_session?.id || data.session_id;
const entity = data.processor_entity || 'openai_llc';
// hosted: `https://chatgpt.com/checkout/${entity}/${sid}`
// custom: data.url || ('https://checkout.stripe.com/c/pay/' + sid)
```

---

## 支付路由：两条完全不同的路

| 路由 | 前缀 | 比例（平台自述） | 支付方式 | 需要浏览器 |
| --- | --- | --- | --- | --- |
| `cs_live` | `cs_live_...` | 约 12% | Stripe `payment_pages/{id}/init` + `/confirm` | ❌ |
| `oaics` | `oaics_...` | 约 88% | 打开托管结账页填卡 | ✅ |

这解释了平台为什么对 `cs_live` 订单单独加收服务费——它是另一种会话类型，处理链路不同。

---

## 区域定价（实测，2026-10-17）

> **时效提示**：以下价格是 2026-10-17 用平台自己的采集接口读到的快照。价格、税费口径和可用地区
> 都会变，本表只用于说明"区域差异有多大"，**不要当成当前报价**。

价格由 `billing_details.country/currency` 决定，不是由 IP 决定。平台每天真建一次会话采集价格
（接口里 `source: "collected_checkout"`）。

| 地区 | Plus | 折 USD | Pro 5x | 折 USD |
| --- | --- | --- | --- | --- |
| 菲律宾 PH | ₱982.14 | ≈ $15.6 | ₱5,794.64 | ≈ $92 |
| 埃及 EG | EGP 877.18 | ≈ $16.4 | EGP 4,736.84 | ≈ $88.7 |
| 智利 CL | CLP 16,798 | ≈ $17.1 | CLP 86,365 | ≈ $87.9 |
| 日本 JP | ¥2,727 | ≈ $17.3 | ¥15,273 | ≈ $96.6 |
| 美国 US | $20.00 | $20.0 | $100 | $100 |

**Plus 最低是 PH，Pro 5x 最低是 CL**——最低价要按套餐分开算。

---

## 商业模型：技术不赚钱

| 收入项 | 机制 |
| --- | --- |
| 区域价差 | PH 成本 ≈ $15.6，按 US 价附近卖给客户 |
| **首月免费套利** | 促销成本 $0，卖整月价，付款后立刻取消续费 |
| 服务费 | 每单 $0.15（接口字段 `service_fee_usd_minor: 15`） |
| `cs_live` 附加费 | 约 12% 的订单 |
| 开卡费 + 充值手续费 | 虚拟卡产品本身 |
| CDK 批发 | 下游代理层层加价 |
| 资金沉淀 | USDT 预充 + 全账户 $20 风险保证金 |

所以"卡密"只是平台自己的服务券，和官方会员没有任何关系；**预生成卡密 ≠ 囤积会员**。

---

## 真实风控下的断点（本次实测）

| 场景 | 结果 |
| --- | --- |
| 浏览器 UI 点 Upgrade 建单 | ✅ 成功，拿到 `oaics_` 会话 |
| 同一浏览器里跑取链脚本 | ❌ `400 unusual activity` |
| 命令行 headless 建单 | ❌ `400 unusual activity` |
| 零余额虚拟卡支付 | ❌ `Payment was not approved` |

**同账号、同 IP、同浏览器，UI 能建单而脚本不能** → 结账接口对"非 UI 发起"的请求有独立风控，
与 IP、Cookie、设备指纹无关。连续失败 7 次后整个账号被临时限制。

这条发现比"跑通一次"更重要：**纯协议方案在生产环境不稳定，必须账号轮换 + 失败不重试。**

---

## 复现实现

```
impl/zovo/
  chatgpt.py     协议引擎：Session→Cookie→校验→报价→取链→激活→取消续费
  stripe_pay.py  Stripe 前端支付：init → confirm
  adapters.py    支付路由：cs_live → StripeAdapter，oaics → OaicsAdapter
  cards.py       卡池调度：按 BIN 成功率选卡
  orders.py      订单状态机（凭据→定价→建单→支付→订阅→对账→续费→完成）
  worker.py      异步订单线程池
  store.py       SQLite：用户/卡/CDK/订单/BIN 统计/审计
  auth.py        PBKDF2 + token 哈希
  dpapi.py       Windows DPAPI 静态加密（卡号/CVC/会话）
  api.py         HTTP 服务 + 内置 Web 控制台
  cli.py         手工分步执行
  script_gen.py  生成同款「F12 取链脚本」
gateway/
  local_gateway.py  本地反代网关（绕过区域封锁，处理 Cloudflare 指纹不一致）
```

### 快速开始

```bash
pip install requests
cd impl
python -m zovo.api --port 8850 --proxy http://127.0.0.1:7897
# 打开 http://127.0.0.1:8850/ ，默认管理员 admin@local / admin12345
```

```bash
# 只验证协议，不扣款
python -m zovo.cli check --session "<完整 Session JSON>"
python -m zovo.cli link  --session "@session.json" --plan plus --country PH --currency PHP --long
python -m zovo.cli preflight --session "@session.json" --plan plus
```

`oaics` 支付需要额外依赖：

```bash
pip install playwright && python -m playwright install chromium
```

---

## 安全设计（实现里已经有的）

- 卡号 / CVC / 会话：Windows DPAPI 加密，换机器解不开
- 订单结束立刻清空 `session_enc`
- 密码 PBKDF2-HMAC-SHA256（210k 轮），token 只存 sha256
- 下单强制 `authorized=true` + 写审计日志
- 订单终态不可重入，避免重复扣款

---

## 为什么公开这份拆解

这类服务长期处在一个奇怪的位置：**买的人不知道自己在交什么，卖的人不需要解释**。
于是形成两种误解——有人以为代充是"内部渠道""协议漏洞"，有人以为只要便宜就没事。

实际上它既不神秘也不安全：

- 技术上，它只是把浏览器里的几步操作写成了程序，没有任何"官方后门"；
- 商业上，它赚的是区域价差、首月促销和卡源成本，不是技术红利；
- 风险上，代价由交出 Session 的人承担，而不是卖家。

把这三件事写清楚、并且用可运行的代码证明，比任何"防骗提醒"都有效。
这也是本仓库唯一的动机：**让信息对称，让选择基于事实。**

如果你读完仍然决定用代充——至少你现在知道自己在交什么。如果你读完决定不用——
那这份拆解就已经达到目的了。

## 边界与免责

**这不是一份"代充教程"，是一份拆解报告。**

- 本项目是**技术拆解与可验证实现**，用于理解这类服务如何运作、风险在哪。
- **不提供**：卡源、账号、可用 Session、成品服务、任何规避风控的手段。
- 转售账号权益违反 OpenAI 使用条款，可能导致账号被封；卡源不明时可能涉及拒付与法律责任。
- 未经账号持有人同意使用其 Session 属于账号接管，不在本项目讨论范围内；实现里已用
  `authorized=true` + 审计日志把这条边界写进代码。
- 代码按 MIT 协议"原样"提供，不附带任何担保；作者不对任何使用后果负责。
- 请在你拥有授权的账号与卡上操作，并自行确认所在地区的法律要求。

如果你是因为想"便宜充 Plus"而找到这里：**别把 Session 交给任何人。**
它等于把你的账号钥匙交出去，而省下的那点钱远不够覆盖账号被盗的损失。
官方渠道之外没有"安全的代充"，只有"还没出事的代充"。
