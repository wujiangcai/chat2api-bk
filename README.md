<h1 align="center">ChatGPT2API</h1>


<p align="center">ChatGPT2API 主要是对 ChatGPT 官网相关能力进行逆向整理与封装，提供面向 ChatGPT 图片生成、图片编辑、多图组图编辑场景的 OpenAI 兼容图片 API / 代理，并集成在线画图、号池管理、多种账号导入方式与 Docker 自托管部署能力。</p>

## 文档导航

- [生产 Compose 部署模板](deploy/production/README.md)
- [中文部署与运维手册](docs/OPERATIONS.zh-CN.md)
- [功能状态清单](docs/feature-status.en.md)

> [!WARNING]
> 免责声明：
>
> 本项目涉及对 ChatGPT 官网文本生成、图片生成与图片编辑等相关接口的逆向研究，仅供个人学习、技术研究与非商业性技术交流使用。
>
> - 严禁将本项目用于任何商业用途、盈利性使用、批量操作、自动化滥用或规模化调用。
> - 严禁将本项目用于破坏市场秩序、恶意竞争、套利倒卖、二次售卖相关服务，以及任何违反 OpenAI 服务条款或当地法律法规的行为。
> - 严禁将本项目用于生成、传播或协助生成违法、暴力、色情、未成年人相关内容，或用于诈骗、欺诈、骚扰等非法或不当用途。
> - 使用者应自行承担全部风险，包括但不限于账号被限制、临时封禁或永久封禁以及因违规使用等所导致的法律责任。
> - 使用本项目即视为你已充分理解并同意本免责声明全部内容；如因滥用、违规或违法使用造成任何后果，均由使用者自行承担。

> [!IMPORTANT]
> 本项目基于对 ChatGPT 官网相关能力的逆向研究实现，存在账号受限、临时封禁或永久封禁的风险。请勿使用你自己的重要账号、常用账号或高价值账号进行测试。

> [!CAUTION]
> 旧版本存在已知漏洞，请尽快升级到最新版本。公网部署时请尽量不要放置敏感信息，并自行做好访问控制与隔离。

## 快速开始

已发布镜像支持 `linux/amd64` 与 `linux/arm64`，在 x86 服务器和 Apple Silicon / ARM Linux 设备上都会自动拉取匹配架构的版本。

```bash
git clone git@github.com:basketikun/chatgpt2api.git
# 按需编辑 config.json 的密钥和 `refresh_account_interval_minute`
# 也可以直接通过环境变量 CHATGPT2API_AUTH_KEY 覆盖 auth-key
docker compose up -d
```

### 存储后端配置

支持通过环境变量 `STORAGE_BACKEND` 切换存储方式：

- `json` - 本地 JSON 文件（默认）
- `sqlite` - 本地 SQLite 数据库
- `postgres` - 外部 PostgreSQL（需配置 `DATABASE_URL`）
- `git` - Git 私有仓库（需配置 `GIT_REPO_URL` 和 `GIT_TOKEN`）

示例：使用 PostgreSQL
```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

### 数据库迁移与审计日志

数据库后端会维护 `schema_migrations` 迁移版本表。上线或升级前可执行：

```bash
python scripts/migrate_database.py --database-url "$DATABASE_URL" --dry-run
python scripts/migrate_database.py --database-url "$DATABASE_URL"
python scripts/migrate_database.py --database-url "$DATABASE_URL" --status
```

PostgreSQL/SQLite 后端已为商业核心数据维护专用表，避免公网账号与计费数据长期落在通用 JSON collection 中：`users`、`packages`、`cdks`、`redemptions`、`orders`、`payments`、`quota_ledger`、`image_jobs`、`image_assets`、`audit_logs`、`launch_evidence`、`auth_sessions`、`auth_action_tokens`。旧版 `storage_collections` 中的同名集合会在首次读取时自动写入对应专用表。

管理端写操作会持久化到 `audit_logs`，可通过 `/api/admin/audit-logs` 或前端“日志管理 -> 审计日志”查看。审计日志会自动脱敏 password/token/secret/api_key 等敏感字段。

### 数据备份、验证与恢复

上线后建议每天通过系统计划任务执行备份脚本，并把生成的 zip 同步到异地存储：

```bash
python scripts/backup_data.py create --database-url "$DATABASE_URL"
python scripts/backup_data.py verify data/backups/chatgpt2api-backup-xxxx.zip
```

恢复演练示例（SQLite 目标库）：

```bash
python scripts/backup_data.py restore data/backups/chatgpt2api-backup-xxxx.zip \
  --restore-to data-restore \
  --database-url sqlite:///data-restore/accounts.db \
  --overwrite
```

说明：

- 默认备份 `data/` 目录、local 对象存储资产、脱敏后的 `config.json`、SQLite 数据库快照。
- PostgreSQL 模式会调用 `pg_dump`，服务器需要安装 PostgreSQL client。
- 备份包内含 `manifest.json` 和 sha256 校验，`verify` 会校验缺失/篡改。
- 生产中建议把 `data/backups` 同步到 S3/R2/OSS/COS，并用系统 cron / Windows Task Scheduler 做每日定时。

### Production readiness and launch evidence

Before opening traffic, operators can run the production preflight and remote deployment verifier from CLI:

```bash
python scripts/check_production_ready.py
python scripts/verify_production_deployment.py --base-url https://img.example.com --admin-key "<admin-key>" --image-job --checkout-webhook-replay --payment-webhook-secret "$PAYMENT_WEBHOOK_SECRET_STRIPE" --strict-launch --output launch-evidence.json --upload-evidence
```

The remote verifier writes a structured `evidence` block proving HTTPS/security headers, PostgreSQL migrations and dedicated tables, Redis queue, remote object storage with public HTTPS URLs, auth cookie/email recovery, Prometheus metrics, no critical alerts, optional checkout initiation/webhook evidence, and optional image-job/public-asset end-to-end evidence. Use `--checkout-initiation` to create disposable package/user/order fixtures and verify `POST /api/orders/{order_id}/checkout`; use `--checkout-webhook-replay` with a webhook secret to replay paid + refund webhooks against the same disposable checkout order; use `--strict-launch` for final sign-off so missing image-job/object-storage end-to-end evidence fails the report.

To archive payment webhook evidence as part of the same launch report, create a
disposable pending order and add:

```bash
--payment-webhook-replay \
--payment-webhook-provider stripe \
--payment-webhook-order-id ord_xxx \
--payment-webhook-secret "$PAYMENT_WEBHOOK_SECRET_STRIPE"
```

This signs and replays a paid webhook and then a refund webhook against the
order, adding `payment_webhook_paid_replay` and
`payment_webhook_refund_replay` to the evidence block.

The admin settings page also includes a "生产上线预检" card backed by `/api/admin/production-readiness`, showing failed/warning checks for PostgreSQL, Redis, object storage, HTTPS/CORS/security, auth cookie/email-verification/email-delivery settings, payment webhook secrets, backups and alert thresholds.
The same card provides a "支付 webhook 一键验收" action backed by `/api/admin/payment-webhook/replay`: it creates a disposable package/user/order, signs and replays paid + refund webhook events using the configured provider secret (or a temporary secret override), verifies fulfillment/refund/quota clawback, disables the disposable fixtures, and archives the result in `launch_evidence`.
It also provides a "Checkout 全链路验收" action backed by `/api/admin/checkout-webhook/replay`: it creates disposable checkout fixtures, generates a checkout session/link/instructions, replays paid + refund webhooks against that same order, verifies quota grant/clawback, disables fixtures, and archives `payment_checkout_*` evidence without requiring CLI access.

For multi-replica public launch, set `RATE_LIMIT_BACKEND=redis` with `REDIS_URL` or `RATE_LIMIT_REDIS_URL`. Login, registration, password reset, CDK redemption and per-user API-key limits then share counters across API processes; `memory` is only for local/single-process development.

### Business reporting

Operators can open the admin settings page "经营报表" card or call `GET /api/admin/business-report?days=30` to review commercial KPIs: users, orders, payments, gross/net revenue by currency, quota ledger, image-job success rate, dead-letter count, asset storage bytes and estimated margin. Set `COST_PER_IMAGE_CENTS` and `COST_CURRENCY` to enable image cost and gross-margin estimation.

### Customer support tickets

Users can open `/support` to create customer-service tickets for billing, refund, account, API and image-generation issues. Admins manage the same queue from Settings > "Support tickets" or via `/api/admin/support/tickets`, add public replies or internal notes, upload screenshots/PDF/log attachments, update status/priority/assignee, and preserve the full message history in the `support_tickets` dedicated storage table. Attachments are written to the configured object storage backend (`local`, S3, R2, MinIO, OSS/COS-compatible) and are limited by `SUPPORT_TICKET_ATTACHMENT_MAX_BYTES` / `SUPPORT_TICKET_ATTACHMENT_ALLOWED_TYPES`. Each ticket carries first-response and resolution SLA deadlines based on priority; `/api/admin/alerts` raises `support_ticket_response_overdue` / `support_ticket_resolution_overdue`, and optional email notifications can be enabled with `SUPPORT_TICKET_EMAIL_NOTIFICATIONS_ENABLED=true`.

### Customer checkout

Users can create a package order and immediately create a checkout session with:

```http
POST /api/orders/{order_id}/checkout
```

The checkout layer supports:

- `PAYMENT_CHECKOUT_PROVIDER=manual`: return transfer instructions and an optional payment URL/QR code.
- `PAYMENT_CHECKOUT_PROVIDER=redirect`: build a signed redirect URL for an external payment page or aggregator by using `PAYMENT_CHECKOUT_URL_TEMPLATE`.
- `PAYMENT_CHECKOUT_PROVIDER=stripe`: create a hosted Stripe Checkout Session with `STRIPE_SECRET_KEY`.

Checkout creation stores the latest safe checkout attempt in `order.metadata.checkout`; actual fulfillment still happens only after `POST /api/payments/webhook/{provider}` verifies a signed paid event. Admins can also call `POST /api/admin/orders/{order_id}/checkout` or use the commercial management UI to generate/copy a payment link for pending orders. For operator acceptance without shell access, `POST /api/admin/checkout-webhook/replay` performs checkout creation plus same-order paid/refund webhook replay and archives launch evidence.

Remote launch evidence can verify the checkout initiation path without taking a real payment, or additionally replay signed paid/refund webhooks against the same temporary checkout order:

```bash
python scripts/verify_production_deployment.py \
  --base-url https://img.example.com \
  --admin-key "<admin-key>" \
  --checkout-webhook-replay \
  --checkout-provider redirect \
  --checkout-webhook-provider stripe \
  --checkout-webhook-secret "$PAYMENT_WEBHOOK_SECRET_STRIPE" \
  --output launch-evidence-with-checkout.json \
  --upload-evidence
```

### Signed payment webhooks

`POST /api/payments/webhook/{provider}` accepts HMAC-SHA256 signed payment/refund events and, after verification, marks the matching order as paid and fulfills quota idempotently, or refunds an already fulfilled order and deducts the granted quota. Configure `PAYMENT_WEBHOOK_SECRET` globally or `PAYMENT_WEBHOOK_SECRET_<PROVIDER>` for a provider-specific secret. Recommended signature header:

```text
X-Payment-Timestamp: <unix-seconds>
X-Payment-Signature: t=<unix-seconds>,v1=<hmac_sha256("<timestamp>.<raw_body>")>
```

The payload should include `order_id` and either:

- a success type/status such as `payment.succeeded`, `checkout.session.completed`, `paid`, `TRADE_SUCCESS`, or `succeeded`;
- or a refund type/status such as `refund.succeeded`, `charge.refunded`, `payment.refunded`, `refund_success`, or `refunded`.

Repeated payment events are de-duplicated by `provider_payment_id` or `idempotency_key`. Repeated refund events are idempotent at the order level; the first successful refund writes one negative `quota_ledger(ref_type="order_refund")` row, while later duplicates return `idempotent=true`.

Provider adapter notes:

- `stripe` accepts `Stripe-Signature` and common Checkout/PaymentIntent/Refund fields such as `client_reference_id`, `payment_intent`, `amount_total`, `currency`, and `metadata.order_id`.
- `alipay` accepts HMAC-normalized sandbox/gateway payloads with `out_trade_no`, `trade_no`, `trade_status`, `total_amount`, and refund statuses such as `REFUND_SUCCESS`.
- `wechatpay` accepts decrypted or gateway-normalized resources with `event_type=TRANSACTION.SUCCESS` / `REFUND.SUCCESS`, `resource.out_trade_no`, `resource.transaction_id`, `resource.trade_state`, `resource.refund_status`, and `resource.amount.total/refund`.

Local webhook replay helper:

```bash
python scripts/payment_webhook_sandbox.py \
  --provider stripe \
  --action paid \
  --order-id ord_xxx \
  --secret "$PAYMENT_WEBHOOK_SECRET_STRIPE" \
  --base-url http://localhost:8000 \
  --send
```

Use `--action refund` to exercise the refund path after the order has been paid/fulfilled, or omit `--send` to print the signed payload and curl command only.
Admins can run the same paid/refund replay from Settings > "生产上线预检" without shell access; leave the secret field empty to use `PAYMENT_WEBHOOK_SECRET_<PROVIDER>` from the server environment. Use the adjacent "Checkout 全链路验收" card to validate checkout initiation and paid/refund fulfillment against one disposable order from the browser.

### Account sessions and recovery

Email/password login now issues an HttpOnly cookie session (`AUTH_SESSION_COOKIE_NAME`, default `chatgpt2api_session`) in addition to the backward-compatible token response. Production can set `AUTH_RESPONSE_INCLUDE_TOKEN=false` so the browser relies on the cookie instead of storing bearer tokens. The login page also reads `/auth/capabilities` to hide public registration when `REGISTRATION_ENABLED=false`.

Account recovery endpoints:

- `POST /auth/email/verification/request`
- `POST /auth/email/verification/confirm`
- `POST /auth/password/reset/request`
- `POST /auth/password/reset/confirm`
- `POST /auth/logout`

`EMAIL_VERIFICATION_REQUIRED=true` blocks password login until the email token is confirmed. Verification and reset emails are delivered by `services/email_service.py` and the public web pages `/verify-email?token=...` and `/reset-password?token=...`.

Email delivery configuration:

- `EMAIL_PROVIDER=console|smtp|resend`：local development can use `console`; production should use `smtp` or `resend`.
- `APP_PUBLIC_URL=https://img.example.com`：used to build email action links.
- SMTP: `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS` / `SMTP_USE_SSL`.
- Resend: `EMAIL_FROM`, `RESEND_API_KEY`.
- `AUTH_RETURN_ACTION_TOKENS=false` should stay false in production; set it to `true` only in local development to display verification/reset tokens in API responses.

### Receipts

Paid/fulfilled/refunded orders expose self-service receipt endpoints. Refunded
orders return a `receipt_type=refund` credit note with negative totals and
`refunded_at`:

- `GET /api/orders/{order_id}/receipt`
- `GET /api/orders/{order_id}/receipt?format=html`
- `GET /api/admin/orders/{order_id}/receipt`

Admins can refund fulfilled orders with `POST /api/admin/orders/{order_id}/refund`.
The first refund policy is strict: the user's current quota balance must still
cover the quota granted by the order, so the refund can deduct the full grant and
write one negative `quota_ledger(ref_type="order_refund")` row. Repeated refund
calls are idempotent.

Receipt seller information comes from `BUSINESS_LEGAL_NAME`, `BUSINESS_TAX_ID`, `BUSINESS_ADDRESS`, `BUSINESS_SUPPORT_EMAIL` and `APP_PUBLIC_URL`. The user billing page can download receipt JSON for paid/fulfilled/refunded orders.

## 用户权限、邮箱注册与 CDK 管理

管理端的“设置 → 用户密钥管理”可以为普通用户创建独立 API key，并配置接口权限、总额度、每分钟限速和到期时间。新 key 的原始值只在创建响应中展示一次；服务端只保存哈希，不会在列表或日志中返回完整 key。

管理端的“设置 → 商业用户 / CDK 管理”提供第一版商业站能力：

- 邮箱 + 密码注册/登录、邮箱验证、找回密码邮件闭环。
- 注册用户额度余额 `quota_balance`。
- 套餐创建、启用/禁用。
- 批量生成额度 CDK 或套餐 CDK；原始 CDK 只在创建响应中展示一次。
- 用户在 `/redeem` 页面兑换 CDK 后自动增加额度或绑定套餐。
- 管理员可查看注册用户、手动加额度、启用/禁用用户、重置密码、查看兑换记录。

相关环境变量：

- `REGISTRATION_ENABLED=true`：是否开放邮箱注册。设为 `false` / `0` / `off` 可关闭公开注册。
- `LOGIN_RATE_LIMIT_PER_MINUTE=8`：同一邮箱每分钟邮箱密码登录尝试次数。设为 `0` 可关闭内存限流。

当前内置权限包括：

- `image.generate`：允许调用 `POST /v1/images/generations`
- `image.edit`：允许调用 `POST /v1/images/edits`
- `chat.completions`：允许调用 `POST /v1/chat/completions`
- `responses.create`：允许调用 `POST /v1/responses`
- `messages.create`：允许调用 `POST /v1/messages`

注册用户的图片额度会在请求开始前预扣；上游失败时退款，成功时保留扣减。手动 API key 的总额度留空表示不限。每分钟限速和登录限流目前是单进程内存限流，适合单机部署；如果多副本部署或面向真实商业用户，应改为 Redis/PostgreSQL 等集中式限流与审计存储。

> [!IMPORTANT]
> 如需面向真实用户运营，请先确认上游账号/API、模型服务、支付流程、内容安全和当地法规均允许商业使用。本项目默认声明仍是学习/研究用途，禁止将逆向个人账号能力用于转售、批量滥用或违反服务条款的商业化场景。

## 功能

### API 兼容能力

- 兼容 `POST /v1/images/generations` 图片生成接口
- 兼容 `POST /v1/images/edits` 图片编辑接口
- 兼容面向图片场景的 `POST /v1/chat/completions`
- 兼容面向图片场景的 `POST /v1/responses`
- `GET /v1/models` 返回 `gpt-image-2`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、
  `gpt-5-mini`
- 支持通过 `n` 返回多张生成结果
- 支持 Codex 中的画图接口逆向，仅 `Plus` / `Team` / `Pro` 订阅可用，模型别名为 `codex-gpt-image-2`，如有需要可自行在其他场景映射回 `gpt-image-2`，用于和官网画图区分；也就意味着同一账号会同时有官网和 Codex 两份生图额度

### 在线画图功能

- 内置在线画图工作台，支持生成、图片编辑与多图组图编辑
- 支持 `gpt-image-2`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、`gpt-5-mini` 模型选择
- 编辑模式支持参考图上传
- 前端支持多图生成交互
- 本地保存图片会话历史，支持回看、删除和清空
- 支持服务端缓存图片URL

### 号池管理功能

- 自动刷新账号邮箱、类型、额度和恢复时间
- 轮询可用账号执行图片生成与图片编辑
- 账号连续失败达到阈值后会自动禁用并切换到其他可用账号，可通过 `auto_disable_consecutive_fail` 配置阈值（默认 5，设为 0 可关闭）
- 支持在账号池页面手动禁用 / 启用账号，禁用账号不会参与生图轮询
- 遇到 Token 失效类错误时自动剔除无效 Token
- 定时检查限流账号并自动刷新
- 支持网页端配置全局 HTTP / HTTPS / SOCKS5 / SOCKS5H 代理
- 支持搜索、筛选、批量刷新、导出、手动编辑和清理账号
- 支持四种导入方式：本地 CPA JSON 文件导入、远程 CPA 服务器导入、`sub2api` 服务器导入、`access_token` 导入
- 支持在设置页配置 `sub2api` 服务器，筛选并批量导入其中的 OpenAI OAuth 账号

### 实验性 / 规划中

- `/v1/complete` 文本补全与流式输出已实现，但仍在测试，目前会出现对话重复的问题，请谨慎测试使用
- 详细状态说明见：[功能清单](./docs/feature-status.en.md)

## Screenshots

文生图界面：

![image](assets/image.png)

编辑图：

![image](assets/image_edit.png)

Cherry Studio 中使用，支持作为绘图接口接入：

![image](assets/chery_studio.png)

号池管理：

![image](assets/account_pool.png)

New Api 接入：

![image](assets/new_api.png)

## API

所有 AI 接口都需要请求头：

```http
Authorization: Bearer <auth-key>
```

<details>
<summary><code>GET /v1/models</code></summary>
<br>

返回当前暴露的图片模型列表。

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer <auth-key>"
```

<details>
<summary>说明</summary>
<br>

| 字段   | 说明                                                                                                         |
|:-----|:-----------------------------------------------------------------------------------------------------------|
| 返回模型 | `gpt-image-2`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、`gpt-5-mini` |
| 接入场景 | 可接入 Cherry Studio、New API 等上游或客户端                                                                          |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/generations</code></summary>
<br>

OpenAI 兼容图片生成接口，用于文生图。

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "一只漂浮在太空里的猫",
    "n": 1,
    "response_format": "b64_json"
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                | 说明                                                 |
|:------------------|:---------------------------------------------------|
| `model`           | 图片模型，当前可用值以 `/v1/models` 返回结果为准，推荐使用 `gpt-image-2` |
| `prompt`          | 图片生成提示词                                            |
| `n`               | 生成数量，当前后端限制为 `1-4`                                 |
| `response_format` | 当前请求模型中包含该字段，默认值为 `b64_json`                       |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/edits</code></summary>
<br>

OpenAI 兼容图片编辑接口，用于上传图片并生成编辑结果。

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -F "model=gpt-image-2" \
  -F "prompt=把这张图改成赛博朋克夜景风格" \
  -F "n=1" \
  -F "image=@./input.png"
```

<details>
<summary>字段说明</summary>
<br>

| 字段       | 说明                                  |
|:---------|:------------------------------------|
| `model`  | 图片模型， `gpt-image-2`                 |
| `prompt` | 图片编辑提示词                             |
| `n`      | 生成数量，当前后端限制为 `1-4`                  |
| `image`  | 需要编辑的图片文件，使用 multipart/form-data 上传 |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/chat/completions</code></summary>
<br>

面向图片场景的 Chat Completions 兼容接口，不是完整通用聊天代理。

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "messages": [
      {
        "role": "user",
        "content": "生成一张雨夜东京街头的赛博朋克猫"
      }
    ],
    "n": 1
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段         | 说明                |
|:-----------|:------------------|
| `model`    | 图片模型，默认按图片生成场景处理  |
| `messages` | 消息数组，需要是图片相关请求内容  |
| `n`        | 生成数量，按当前实现解析为图片数量 |
| `stream`   | 已实现，但仍在测试         |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/responses</code></summary>
<br>

面向图片生成工具调用的 Responses API 兼容接口，不是完整通用 Responses API 代理。

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-5",
    "input": "生成一张未来感城市天际线图片",
    "tools": [
      {
        "type": "image_generation"
      }
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段       | 说明                            |
|:---------|:------------------------------|
| `model`  | 响应中会回显该模型字段，但图片生成当前仍走图片生成兼容逻辑 |
| `input`  | 输入内容，需要能解析出图片生成提示词            |
| `tools`  | 必须包含 `image_generation` 工具请求  |
| `stream` | 已实现，但仍在测试                     |

<br>
</details>
</details>

## 社区支持

学 AI , 上 L 站：[LinuxDO](https://linux.do)

## Contributors

感谢所有为本项目做出贡献的开发者：

<a href="https://github.com/basketikun/chatgpt2api/graphs/contributors">
  <img alt="Contributors" src="https://contrib.rocks/image?repo=basketikun/chatgpt2api" />
</a>

## Star History

[![Star History Chart](https://api.star-history.com/chart?repos=basketikun/chatgpt2api&type=date&legend=top-left)](https://www.star-history.com/?repos=basketikun%2Fchatgpt2api&type=date&legend=top-left)
