# Production deployment template

This directory provides a single-node commercial deployment template:

- FastAPI + built static web UI
- PostgreSQL primary storage
- Redis image job queue and distributed lock
- MinIO S3-compatible object storage for image assets
- Caddy HTTPS reverse proxy and security headers

## 1. Prepare configuration

```bash
cd deploy/production
cp .env.production.example .env.production
```

Edit `.env.production` and replace:

- `CADDY_SITE_ADDRESS`, `CHATGPT2API_BASE_URL`, `WEB_ALLOWED_ORIGINS`
- `BUSINESS_LEGAL_NAME`, `BUSINESS_SUPPORT_EMAIL` and optional tax/address fields for customer receipts
- `CHATGPT2API_AUTH_KEY`
- `POSTGRES_PASSWORD` and `DATABASE_URL`
- `MINIO_ROOT_PASSWORD` and `OBJECT_STORAGE_SECRET_ACCESS_KEY`
- `OBJECT_STORAGE_PUBLIC_BASE_URL`

Tune `IMAGE_SSE_TIMEOUT_SECONDS` (default `180`) for the maximum total wait on
the upstream ChatGPT image SSE stream. It prevents a connected but silent
upstream from occupying a worker forever; weak networks may use `240`-`300`.

For Cloudflare R2 / AWS S3 / OSS / COS, set `OBJECT_STORAGE_BACKEND=s3` or `r2`,
replace the endpoint/bucket/keys, and point `OBJECT_STORAGE_PUBLIC_BASE_URL` at
the public bucket domain or CDN.

## 2. Start services

```bash
docker compose --env-file .env.production up -d --build
```

## 3. Run database migrations

The migration runner creates dedicated tables for commercial/account data, including `auth_sessions` and `auth_action_tokens`, and records applied versions in `schema_migrations`.

```bash
docker compose --env-file .env.production exec api sh -lc \
  'python scripts/migrate_database.py --database-url "$DATABASE_URL"'
```

Check migration status:

```bash
docker compose --env-file .env.production exec api sh -lc \
  'python scripts/migrate_database.py --database-url "$DATABASE_URL" --status'
```

## 4. Verify health, readiness and metrics

```bash
curl -fsS https://img.example.com/health/live
curl -fsS https://img.example.com/health/ready
curl -fsS -H "Authorization: Bearer <admin-key>" \
  "https://img.example.com/api/admin/metrics?format=prometheus"
curl -fsS -H "Authorization: Bearer <admin-key>" \
  "https://img.example.com/api/admin/business-report?days=30"
```

Run the stricter launch preflight from inside the API container:

```bash
docker compose --env-file .env.production exec api sh -lc \
  'python scripts/check_production_ready.py'
```

The same result is available to admins at:

```bash
curl -fsS -H "Authorization: Bearer <admin-key>" \
  "https://img.example.com/api/admin/production-readiness"
```

Operators can also open the admin Settings page and use the "生产上线预检" card to review the same failed/warning checks visually, including auth cookie/email-verification/email-delivery and payment webhook secret configuration.
After running `verify_production_deployment.py --output launch-evidence.json`,
paste that JSON in the same card to archive the launch evidence in
`launch_evidence` for sign-off and future audits.
Alternatively, add `--upload-evidence` to the verifier command so the report is posted to `/api/admin/launch-evidence` automatically.
The same Settings card includes "支付 webhook 一键验收": it creates disposable commercial fixtures, signs a paid webhook and a refund webhook with the configured provider secret, verifies order fulfillment/refund and quota clawback, disables the fixtures, and archives the evidence automatically.
The card also includes Checkout end-to-end acceptance backed by `POST /api/admin/checkout-webhook/replay`: it creates disposable checkout fixtures, generates a checkout entry, replays paid/refund webhooks on the same order, verifies quota grant/clawback, disables fixtures and archives `payment_checkout_*` evidence.
The same Settings page also exposes "经营报表", backed by `/api/admin/business-report`,
for revenue, order, quota, image-job success-rate, dead-letter, asset storage and
estimated margin review. Set `COST_PER_IMAGE_CENTS` / `COST_CURRENCY` in
`.env.production` to make the margin estimate meaningful.
Customer support tickets are available at `/support` for users and Settings > "Support tickets" for admins. The workflow is backed by `/api/support/tickets`, `/api/admin/support/tickets` and the dedicated `support_tickets` SQL table, so production operators can handle billing/refund/API issues without external spreadsheets. Ticket screenshots/PDF/log attachments use the same S3/R2/MinIO object storage backend as generated image assets; configure `SUPPORT_TICKET_ATTACHMENT_MAX_BYTES` and `SUPPORT_TICKET_ATTACHMENT_ALLOWED_TYPES` before launch. Configure `SUPPORT_TICKET_*_SLA_HOURS*` for first-response/resolution windows, keep `ALERT_SUPPORT_*_OVERDUE_THRESHOLD=1` for SLA alerts, and enable `SUPPORT_TICKET_EMAIL_NOTIFICATIONS_ENABLED=true` when SMTP/Resend is ready.

Payment providers can call the signed webhook endpoint:

```bash
curl -fsS -X POST "https://img.example.com/api/payments/webhook/stripe" \
  -H "Content-Type: application/json" \
  -H "X-Payment-Timestamp: <unix-seconds>" \
  -H "X-Payment-Signature: t=<unix-seconds>,v1=<hmac-sha256>" \
  --data-binary @payment-event.json
```

Use `PAYMENT_WEBHOOK_SECRET` or a provider-specific
`PAYMENT_WEBHOOK_SECRET_<PROVIDER>` in `.env.production`. The signed raw body
must include the commercial `order_id`; successful events are idempotently
recorded in `payments` and automatically fulfill the order quota. Refund events
such as `refund.succeeded`, `charge.refunded`, `payment.refunded`, and
`REFUND.SUCCESS` are routed to the same order refund/negative quota-ledger path.

Self-service checkout is separate from fulfillment. Configure
`PAYMENT_CHECKOUT_PROVIDER=redirect` with `PAYMENT_CHECKOUT_URL_TEMPLATE` and
`PAYMENT_CHECKOUT_SIGNING_SECRET` when using your own payment page/aggregator,
or set `PAYMENT_CHECKOUT_PROVIDER=stripe` plus `STRIPE_SECRET_KEY` for hosted
Stripe Checkout Sessions. Users call `POST /api/orders/{order_id}/checkout`;
admins can generate/copy the same payment link from the commercial management
UI for pending orders. The payment is still considered successful only after
the signed webhook confirms it.

For local or staging acceptance replay:

```bash
python scripts/payment_webhook_sandbox.py \
  --provider stripe \
  --action paid \
  --order-id ord_xxx \
  --secret "$PAYMENT_WEBHOOK_SECRET_STRIPE" \
  --base-url https://img.example.com
```

For public accounts, keep HttpOnly cookie sessions enabled:
`AUTH_SESSION_COOKIE_ENABLED=true`, `AUTH_SESSION_COOKIE_SECURE=true`, and
`AUTH_RESPONSE_INCLUDE_TOKEN=false`. Set `EMAIL_VERIFICATION_REQUIRED=true`
before opening registration. Configure `APP_PUBLIC_URL` plus a real
`EMAIL_PROVIDER=smtp` (or `resend`) sender so registration, `/verify-email`,
and `/reset-password` are fully clickable from user inboxes. Keep
`AUTH_RETURN_ACTION_TOKENS=false` in production so verification/reset tokens
only travel through email. Also keep `RATE_LIMIT_BACKEND=redis` so login,
registration, reset, redemption and API-key rate limits are shared across API
replicas.

From an operator workstation or CI runner, collect a launch evidence report
against the public HTTPS endpoint:

```bash
python ../../scripts/verify_production_deployment.py \
  --base-url https://img.example.com \
  --admin-key "<admin-key>" \
  --checkout-webhook-replay \
  --payment-webhook-secret "$PAYMENT_WEBHOOK_SECRET_STRIPE" \
  --output launch-evidence.json \
  --upload-evidence
```

After quota/upstream accounts are ready, include a real async image pipeline
smoke test:

```bash
python ../../scripts/verify_production_deployment.py \
  --base-url https://img.example.com \
  --admin-key "<admin-key>" \
  --image-job \
  --strict-launch \
  --output launch-evidence-with-image-job.json \
  --upload-evidence
```

Use the second command for final launch sign-off. `--strict-launch` requires the async image job to complete, records the generated asset, fetches the public object-storage URL, and sets `evidence.launch_evidence_strict_ready=true` only when PostgreSQL, migrations, dedicated tables, Redis, object storage, auth/email, metrics and alerts are all production-grade.

`--checkout-initiation` creates a temporary package, user and pending order, calls
`POST /api/orders/{order_id}/checkout` with that user token, verifies the
checkout session/link/instructions, then disables the temporary package/user.
The report records `evidence.payment_checkout_order_created` and
`evidence.payment_checkout_session_created`.
`--checkout-webhook-replay` runs the same fixture setup and then signs paid +
refund webhooks against the same disposable checkout order. The report records
`evidence.payment_checkout_paid_replay` and
`evidence.payment_checkout_refund_replay`. If `--checkout-webhook-secret` is not
set, it reuses `--payment-webhook-secret`.

To include paid/refund webhook evidence, create a disposable pending order in the
deployment, then add the webhook replay flags:

```bash
python ../../scripts/verify_production_deployment.py \
  --base-url https://img.example.com \
  --admin-key "<admin-key>" \
  --payment-webhook-replay \
  --payment-webhook-provider stripe \
  --payment-webhook-order-id ord_xxx \
  --payment-webhook-secret "$PAYMENT_WEBHOOK_SECRET_STRIPE" \
  --output launch-evidence-with-payment-webhook.json \
  --upload-evidence
```

The report records `evidence.payment_webhook_paid_replay` and
`evidence.payment_webhook_refund_replay`; the disposable order will end in
`refunded` state.
If operators cannot run the verifier from a shell, use Settings > "生产上线预检" > "支付 webhook 一键验收" instead; leave the secret override blank to use `PAYMENT_WEBHOOK_SECRET_<PROVIDER>` from `.env.production`. Use the adjacent Checkout end-to-end acceptance action to cover checkout initiation plus same-order paid/refund webhook replay from the browser.

Paid or fulfilled orders expose `/api/orders/{order_id}/receipt` and `/api/orders/{order_id}/receipt?format=html`. Verify the business identity in `.env.production` before issuing receipts to customers.

## 5. Back up and verify

Create a backup:

```bash
docker compose --env-file .env.production exec api sh -lc \
  'python scripts/backup_data.py create --database-url "$DATABASE_URL" --json'
```

Verify a backup:

```bash
docker compose --env-file .env.production exec api sh -lc \
  'python scripts/backup_data.py verify /app/data/backups/<backup-file>.zip --json'
```

Schedule this command daily with cron, systemd timer, or your platform scheduler,
then sync backup zips to a different bucket or server.

## 6. Smoke test checklist

1. `docker compose ps` shows `postgres`, `redis`, `api`, and `caddy` healthy/running.
2. `/health/ready` returns `status != unhealthy`.
3. `/api/storage/info` shows `storage_backend=postgres`, `image_job_queue.backend=redis`,
   and `object_storage.backend=minio|s3|r2`.
4. Register/login or create an API key.
5. Create an async image job and verify it reaches `succeeded`.
6. Open the generated asset URL from `OBJECT_STORAGE_PUBLIC_BASE_URL`.
7. Confirm `/api/admin/metrics`, `/api/admin/alerts`, and `/api/admin/business-report`
   are available to admins.
8. Run backup `create`, `verify`, and a temporary restore rehearsal.

## 7. Scaling notes

- Move PostgreSQL, Redis and object storage to managed services before high traffic.
- Run multiple `api` replicas only after verifying all writes use PostgreSQL and image
  workers use Redis locks in your environment.
- Keep Caddy at the edge or replace it with your cloud load balancer/CDN.

For a Chinese architecture, proxy, test-gate, monitoring, upgrade and rollback
runbook, see [`docs/OPERATIONS.zh-CN.md`](../../docs/OPERATIONS.zh-CN.md).
