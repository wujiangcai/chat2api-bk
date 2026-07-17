import { httpRequest } from "@/lib/request";

export type AccountType = "Free" | "Plus" | "ProLite" | "Pro" | "Team";
export type AccountStatus = "正常" | "限流" | "异常" | "禁用";
export type ImageModel = "auto" | "gpt-image-1" | "gpt-image-2";
export type AuthRole = "admin" | "user";

export type Account = {
  id: string;
  access_token: string;
  type: AccountType;
  status: AccountStatus;
  quota: number;
  imageQuotaUnknown?: boolean;
  email?: string | null;
  user_id?: string | null;
  limits_progress?: Array<{
    feature_name?: string;
    remaining?: number;
    reset_after?: string;
  }>;
  default_model_slug?: string | null;
  restoreAt?: string | null;
  success: number;
  fail: number;
  lastUsedAt: string | null;
};

type AccountListResponse = {
  items: Account[];
};

type AccountMutationResponse = {
  items: Account[];
  added?: number;
  skipped?: number;
  removed?: number;
  refreshed?: number;
  errors?: Array<{ access_token: string; error: string }>;
};

type AccountRefreshResponse = {
  items: Account[];
  refreshed: number;
  errors: Array<{ access_token: string; error: string }>;
};

type AccountUpdateResponse = {
  item: Account;
  items: Account[];
};

export type SettingsConfig = {
  proxy: string;
  base_url?: string;
  refresh_account_interval_minute?: number | string;
  image_retention_days?: number | string;
  auto_remove_invalid_accounts?: boolean;
  [key: string]: unknown;
};

export type ManagedImage = {
  name: string;
  date: string;
  size: number;
  url: string;
  created_at: string;
};

export type SystemLog = {
  time: string;
  type: "call" | "account" | string;
  summary?: string;
  detail?: Record<string, unknown>;
  [key: string]: unknown;
};

export type AuditLog = {
  id: string;
  action: string;
  status: "succeeded" | "failed" | string;
  summary?: string;
  actor?: {
    type?: string;
    id?: string;
    key_id?: string;
    email?: string;
    name?: string;
  };
  target?: {
    type?: string;
    id?: string;
  };
  request?: {
    ip?: string;
    method?: string;
    path?: string;
    user_agent?: string;
    request_id?: string;
  };
  detail?: Record<string, unknown>;
  created_at: string;
};

export type LoginResponse = {
  ok: boolean;
  version: string;
  role: AuthRole;
  subject_id: string;
  key_id?: string;
  email?: string | null;
  name: string;
  token?: string;
  quota_balance?: number | null;
  package_name?: string | null;
  email_verified?: boolean;
  email_verified_at?: string | null;
  session_cookie?: boolean;
  verification_required?: boolean;
  verification_expires_at?: string | null;
  verification_token?: string;
  email_sent?: boolean;
  email_provider?: string | null;
  email_delivery?: {
    sent?: boolean;
    provider?: string;
    message?: string;
  };
};

export type AuthCapabilities = {
  registration_enabled: boolean;
  email_verification_required: boolean;
  session_cookie_enabled: boolean;
  password_reset_enabled: boolean;
  email_delivery_configured?: boolean;
  email_provider?: string | null;
};

export type UserKey = {
  id: string;
  name: string;
  role: "user";
  enabled: boolean;
  created_at: string | null;
  last_used_at: string | null;
  permissions: string[];
  quota_limit: number | null;
  quota_used: number;
  quota_remaining: number | null;
  rate_limit_per_minute: number | null;
  expires_at: string | null;
  metadata?: Record<string, unknown>;
};

export type UserKeyPayload = {
  name?: string;
  permissions?: string[];
  quota_limit?: number | null;
  quota_unlimited?: boolean;
  rate_limit_per_minute?: number | null;
  rate_limit_unlimited?: boolean;
  expires_at?: string | null;
  expires_never?: boolean;
  metadata?: Record<string, unknown>;
  enabled?: boolean;
  reset_quota_used?: boolean;
  add_quota?: number;
};

export type RegisteredUser = {
  id: string;
  email: string;
  name: string;
  role: "user";
  enabled: boolean;
  quota_balance: number;
  email_verified?: boolean;
  email_verified_at?: string | null;
  package_id?: string | null;
  package_name?: string | null;
  package_expires_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  last_login_at?: string | null;
  last_used_at?: string | null;
};

export type PackageItem = {
  id: string;
  name: string;
  description?: string;
  quota: number;
  price_cents?: number;
  currency?: string;
  valid_days?: number | null;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
};

export type CDKItem = {
  id: string;
  code_prefix: string;
  name: string;
  type: "quota" | "package";
  quota: number;
  package_id?: string | null;
  package_name?: string | null;
  max_redemptions: number;
  redeemed_count: number;
  per_user_limit: number;
  enabled: boolean;
  expires_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  last_redeemed_at?: string | null;
};

export type RedemptionRecord = {
  id: string;
  cdk_id: string;
  code_prefix?: string;
  user_id: string;
  email?: string;
  type: "quota" | "package";
  quota_granted: number;
  package_id?: string | null;
  package_name?: string | null;
  redeemed_at: string;
};

export type QuotaLedgerItem = {
  id: string;
  user_id: string;
  type: "grant" | "consume" | "refund" | "adjust" | "set" | string;
  amount: number;
  balance_before: number;
  balance_after: number;
  reason?: string | null;
  ref_type?: string | null;
  ref_id?: string | null;
  actor_type?: string | null;
  actor_id?: string | null;
  created_at: string;
  metadata?: Record<string, unknown>;
};

export type OrderItem = {
  id: string;
  user_id: string;
  email?: string | null;
  package_id: string;
  package_name?: string | null;
  package_snapshot?: Partial<PackageItem> & Record<string, unknown>;
  quantity: number;
  quota_total: number;
  amount_cents: number;
  currency: string;
  status: "created" | "pending_payment" | "paid" | "fulfilled" | "cancelled" | "refunded" | string;
  payment_id?: string | null;
  provider?: string | null;
  provider_payment_id?: string | null;
  quota_granted: number;
  package_expires_at?: string | null;
  created_at: string;
  updated_at: string;
  paid_at?: string | null;
  fulfilled_at?: string | null;
  cancelled_at?: string | null;
  refunded_at?: string | null;
  metadata?: Record<string, unknown>;
};

export type PaymentItem = {
  id: string;
  order_id: string;
  user_id: string;
  email?: string | null;
  provider: string;
  provider_payment_id?: string | null;
  idempotency_key?: string | null;
  amount_cents: number;
  currency: string;
  status: "succeeded" | "refunded" | string;
  created_at: string;
  paid_at?: string | null;
  refunded_at?: string | null;
  metadata?: Record<string, unknown>;
};

export type CheckoutSession = {
  id: string;
  provider: "manual" | "redirect" | "stripe" | string;
  mode?: string | null;
  order_id: string;
  amount_cents: number;
  currency: string;
  status: string;
  payment_url?: string | null;
  qr_code_url?: string | null;
  instructions?: string | null;
  provider_session_id?: string | null;
  success_url?: string | null;
  cancel_url?: string | null;
  created_at?: string | null;
  expires_at?: string | number | null;
  metadata?: Record<string, unknown>;
};

export type ReceiptItem = {
  id: string;
  receipt_type?: "receipt" | "refund" | string;
  receipt_number: string;
  order_id: string;
  payment_id?: string | null;
  provider?: string | null;
  provider_payment_id?: string | null;
  status: "issued" | string;
  seller: {
    name?: string | null;
    tax_id?: string | null;
    address?: string | null;
    support_email?: string | null;
    website?: string | null;
  };
  buyer: {
    user_id?: string | null;
    email?: string | null;
  };
  currency: string;
  amount_cents: number;
  amount_display: string;
  tax_cents: number;
  total_cents: number;
  total_display: string;
  lines: Array<{
    description: string;
    package_id?: string | null;
    quantity: number;
    quota: number;
    unit_amount_cents: number;
    amount_cents: number;
    currency: string;
  }>;
  quota_granted?: number;
  quota_deducted?: number;
  package_expires_at?: string | null;
  paid_at?: string | null;
  refunded_at?: string | null;
  issued_at: string;
  created_at?: string | null;
  metadata?: Record<string, unknown>;
};

export type ImageJob = {
  id: string;
  type: "image.generation" | string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  owner?: {
    role?: AuthRole | string;
    key_id?: string | null;
    key_name?: string | null;
    user_id?: string | null;
    email?: string | null;
  };
  request: {
    prompt?: string;
    model?: string;
    n?: number;
    size?: string | null;
    response_format?: string;
  };
  prompt_preview?: string;
  reserved_quota?: number;
  refunded_quota?: number;
  cost_units?: number;
  attempts?: number;
  max_attempts?: number;
  next_run_after?: string | null;
  dead_lettered_at?: string | null;
  assets?: ImageAsset[];
  result?: { data?: Array<{ url?: string; b64_json?: string; revised_prompt?: string }> } | null;
  error?: { message?: string; type?: string } | null;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
};

export type ImageAsset = {
  id: string;
  owner?: {
    role?: AuthRole | string;
    key_id?: string | null;
    key_name?: string | null;
    user_id?: string | null;
    email?: string | null;
  };
  job_id?: string | null;
  source?: string | null;
  model?: string | null;
  prompt_hash?: string | null;
  prompt_preview?: string | null;
  object_key?: string | null;
  url: string;
  mime_type?: string | null;
  size_bytes: number;
  width?: number;
  height?: number;
  status: "active" | "deleted" | string;
  revised_prompt?: string | null;
  created_at: string;
  deleted_at?: string | null;
  name?: string;
  date?: string;
  size?: number;
};

export type ProductionReadinessItem = {
  id: string;
  status: "passed" | "warning" | "failed" | string;
  message: string;
  detail?: Record<string, unknown>;
};

export type ProductionReadinessResult = {
  status: "passed" | "warning" | "failed" | string;
  ready: boolean;
  strict: boolean;
  summary: {
    total: number;
    passed: number;
    warning: number;
    failed: number;
  };
  items: ProductionReadinessItem[];
};

export type LaunchEvidenceItem = {
  id: string;
  name: string;
  status: "passed" | "warning" | "failed" | string;
  ready: boolean;
  source?: string | null;
  base_url?: string | null;
  generated_at?: string | null;
  created_at: string;
  created_by?: string | null;
  summary?: {
    total?: number;
    passed?: number;
    warning?: number;
    failed?: number;
  };
  failed_checks?: Array<{ id?: string; message?: string }>;
  report?: Record<string, unknown>;
};

export type PaymentWebhookReplayReport = {
  status: "passed" | "failed" | "warning" | string;
  ready: boolean;
  source?: string;
  generated_at?: string;
  provider: string;
  webhook_provider?: string;
  checkout_provider?: string;
  checkout_id?: string;
  order_id?: string;
  user_id?: string;
  package_id?: string;
  summary: {
    total: number;
    passed: number;
    warning: number;
    failed: number;
  };
  evidence: {
    payment_webhook_replay_requested?: boolean;
    payment_webhook_paid_replay?: boolean;
    payment_webhook_refund_replay?: boolean;
    disposable_order_id?: string;
    disposable_user_disabled?: boolean;
    temporary_package_disabled?: boolean;
    [key: string]: unknown;
  };
  checks: Array<{
    id: string;
    status: "passed" | "warning" | "failed" | string;
    message: string;
    detail?: Record<string, unknown>;
  }>;
};

export type BusinessReportPeriod = {
  label: string;
  start_at?: string | null;
  users: {
    total: number;
    enabled: number;
    disabled: number;
    with_positive_quota: number;
    quota_balance_total: number;
    current_total: number;
    current_enabled: number;
  };
  auth_keys?: {
    total?: number | null;
    enabled_total?: number | null;
  };
  packages: {
    total: number;
    enabled_total: number;
    disabled_total: number;
  };
  orders: {
    total: number;
    by_status: Record<string, number>;
    pending_total: number;
    paid_total: number;
    fulfilled_total: number;
    cancelled_total: number;
    refunded_total: number;
    payable_amount_cents_by_currency: Record<string, number>;
    paid_or_fulfilled_amount_cents_by_currency: Record<string, number>;
    order_amount_cents_by_currency: Record<string, number>;
    quota_total: number;
    quota_granted: number;
  };
  payments: {
    total: number;
    by_status: Record<string, number>;
    succeeded_total: number;
    refunded_total: number;
    gross_revenue_cents_by_currency: Record<string, number>;
    refunded_cents_by_currency: Record<string, number>;
    net_revenue_cents_by_currency: Record<string, number>;
    by_provider: Record<string, number>;
  };
  quota: {
    ledger_total: number;
    by_type: Record<string, number>;
    granted_units: number;
    consumed_units: number;
    refunded_units: number;
    adjusted_units: number;
    positive_units: number;
    negative_units: number;
    net_units: number;
    current_balance_total: number;
  };
  redemptions: {
    total: number;
    quota_granted: number;
    by_type: Record<string, number>;
  };
  image_jobs: {
    total: number;
    by_status: Record<string, number>;
    queued_total: number;
    running_total: number;
    succeeded_total: number;
    failed_total: number;
    cancelled_total: number;
    dead_letter_total: number;
    success_rate?: number | null;
    failure_rate?: number | null;
    reserved_quota: number;
    refunded_quota: number;
    cost_units: number;
    attempts_total: number;
  };
  image_assets: {
    total: number;
    active_total: number;
    deleted_total: number;
    by_status: Record<string, number>;
    size_bytes: number;
    active_size_bytes: number;
    deleted_size_bytes: number;
  };
  unit_economics: {
    cost_per_image_cents: number;
    cost_currency: string;
    estimated_image_cost_cents: number;
    gross_revenue_cents_by_currency: Record<string, number>;
    net_revenue_cents_by_currency: Record<string, number>;
    estimated_gross_margin_cents_by_currency: Record<string, number>;
  };
};

export type BusinessReport = {
  generated_at: string;
  window_days: number;
  window_start: string;
  window_end: string;
  cost_per_image_cents: number;
  cost_currency: string;
  summary: {
    users_total: number;
    users_enabled_total: number;
    quota_balance_total: number;
    gross_revenue_cents_by_currency: Record<string, number>;
    window_gross_revenue_cents_by_currency: Record<string, number>;
    estimated_image_cost_cents: number;
    window_estimated_image_cost_cents: number;
    estimated_gross_margin_cents_by_currency: Record<string, number>;
    window_estimated_gross_margin_cents_by_currency: Record<string, number>;
    orders_total: number;
    orders_pending_total: number;
    orders_fulfilled_total: number;
    payments_succeeded_total: number;
    image_jobs_success_rate?: number | null;
    window_image_jobs_success_rate?: number | null;
    image_jobs_dead_letter_total: number;
    image_assets_active_total: number;
    image_assets_active_bytes: number;
  };
  all_time: BusinessReportPeriod;
  window: BusinessReportPeriod;
};

export async function login(authKey: string) {
  const normalizedAuthKey = String(authKey || "").trim();
  return httpRequest<LoginResponse>("/auth/login", {
    method: "POST",
    body: {},
    headers: {
      Authorization: `Bearer ${normalizedAuthKey}`,
    },
    redirectOnUnauthorized: false,
  });
}

export async function loginWithPassword(email: string, password: string) {
  return httpRequest<LoginResponse>("/auth/login", {
    method: "POST",
    body: { email, password },
    redirectOnUnauthorized: false,
  });
}

export async function registerWithPassword(email: string, password: string, name = "") {
  return httpRequest<LoginResponse>("/auth/register", {
    method: "POST",
    body: { email, password, name },
    redirectOnUnauthorized: false,
  });
}

export async function fetchAuthCapabilities() {
  return httpRequest<AuthCapabilities>("/auth/capabilities", {
    redirectOnUnauthorized: false,
  });
}

export async function logout() {
  return httpRequest<{ ok: boolean }>("/auth/logout", {
    method: "POST",
    body: {},
    redirectOnUnauthorized: false,
  });
}

export async function requestEmailVerification(email: string) {
  return httpRequest<{
    ok: boolean;
    sent: boolean;
    expires_at?: string | null;
    token?: string;
    email_sent?: boolean;
    email_provider?: string | null;
  }>("/auth/email/verification/request", {
    method: "POST",
    body: { email },
    redirectOnUnauthorized: false,
  });
}

export async function confirmEmailVerification(token: string) {
  return httpRequest<{ ok: boolean; user: RegisteredUser }>("/auth/email/verification/confirm", {
    method: "POST",
    body: { token },
    redirectOnUnauthorized: false,
  });
}

export async function requestPasswordReset(email: string) {
  return httpRequest<{
    ok: boolean;
    sent: boolean;
    expires_at?: string | null;
    token?: string;
    email_sent?: boolean;
    email_provider?: string | null;
  }>("/auth/password/reset/request", {
    method: "POST",
    body: { email },
    redirectOnUnauthorized: false,
  });
}

export async function confirmPasswordReset(token: string, password: string) {
  return httpRequest<{ ok: boolean; user: RegisteredUser }>("/auth/password/reset/confirm", {
    method: "POST",
    body: { token, password },
    redirectOnUnauthorized: false,
  });
}

export type CurrentIdentity = {
  id: string;
  key_id?: string;
  user_id?: string;
  email?: string | null;
  name: string;
  role: AuthRole;
  permissions: string[];
  quota_limit?: number | null;
  quota_used?: number | null;
  quota_remaining?: number | null;
  quota_balance?: number | null;
  package_id?: string | null;
  package_name?: string | null;
  package_expires_at?: string | null;
  email_verified?: boolean;
  email_verified_at?: string | null;
  rate_limit_per_minute?: number | null;
  expires_at?: string | null;
};

export async function fetchMe() {
  return httpRequest<CurrentIdentity>("/auth/me");
}

export async function redeemCode(code: string) {
  return httpRequest<{ ok: boolean; grant: Record<string, unknown>; user: RegisteredUser }>("/auth/redeem", {
    method: "POST",
    body: { code },
  });
}

export async function fetchMyQuotaLedger(limit = 100) {
  const params = new URLSearchParams({ limit: String(limit) });
  return httpRequest<{ items: QuotaLedgerItem[] }>(`/api/me/quota-ledger?${params.toString()}`);
}

export async function fetchAdminQuotaLedger(filters: { user_id?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.user_id) params.set("user_id", filters.user_id);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: QuotaLedgerItem[] }>(`/api/admin/quota-ledger${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchPublicPackages() {
  return httpRequest<{ items: PackageItem[] }>("/api/packages");
}

export async function createOrder(payload: { package_id: string; quantity?: number; metadata?: Record<string, unknown> }) {
  return httpRequest<{ order: OrderItem }>("/api/orders", {
    method: "POST",
    body: payload,
  });
}

export async function fetchOrders(filters: { status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: OrderItem[] }>(`/api/orders${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchOrder(orderId: string) {
  return httpRequest<{ order: OrderItem }>(`/api/orders/${orderId}`);
}

export async function fetchOrderReceipt(orderId: string) {
  return httpRequest<{ receipt: ReceiptItem }>(`/api/orders/${orderId}/receipt`);
}

export async function createOrderCheckout(
  orderId: string,
  payload: {
    provider?: string;
    success_url?: string;
    cancel_url?: string;
    metadata?: Record<string, unknown>;
  } = {},
) {
  return httpRequest<{ order: OrderItem; checkout: CheckoutSession }>(`/api/orders/${orderId}/checkout`, {
    method: "POST",
    body: payload,
  });
}

export async function cancelOrder(orderId: string, reason = "") {
  return httpRequest<{ order: OrderItem }>(`/api/orders/${orderId}/cancel`, {
    method: "POST",
    body: { reason },
  });
}

export async function fetchAdminOrders(filters: { status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: OrderItem[] }>(`/api/admin/orders${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchAdminPayments(filters: { provider?: string; status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.provider) params.set("provider", filters.provider);
  if (filters.status) params.set("status", filters.status);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: PaymentItem[] }>(`/api/admin/payments${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchAdminOrderReceipt(orderId: string) {
  return httpRequest<{ receipt: ReceiptItem }>(`/api/admin/orders/${orderId}/receipt`);
}

export async function createAdminOrderCheckout(
  orderId: string,
  payload: {
    provider?: string;
    success_url?: string;
    cancel_url?: string;
    metadata?: Record<string, unknown>;
  } = {},
) {
  return httpRequest<{ order: OrderItem; checkout: CheckoutSession }>(`/api/admin/orders/${orderId}/checkout`, {
    method: "POST",
    body: payload,
  });
}

export async function markAdminOrderPaid(
  orderId: string,
  payload: {
    provider?: string;
    provider_payment_id?: string;
    amount_cents?: number | null;
    currency?: string | null;
    idempotency_key?: string;
    metadata?: Record<string, unknown>;
    auto_fulfill?: boolean;
  } = {},
) {
  return httpRequest<{ order: OrderItem; payment?: PaymentItem | null; user?: RegisteredUser; idempotent?: boolean }>(
    `/api/admin/orders/${orderId}/mark-paid`,
    {
      method: "POST",
      body: payload,
    },
  );
}

export async function fulfillAdminOrder(orderId: string) {
  return httpRequest<{ order: OrderItem; payment?: PaymentItem | null; user?: RegisteredUser; idempotent?: boolean }>(
    `/api/admin/orders/${orderId}/fulfill`,
    {
      method: "POST",
      body: {},
    },
  );
}

export async function refundAdminOrder(
  orderId: string,
  payload: { reason?: string; metadata?: Record<string, unknown> } = {},
) {
  return httpRequest<{
    order: OrderItem;
    payment?: PaymentItem | null;
    user?: RegisteredUser;
    quota_deducted?: number;
    idempotent?: boolean;
  }>(`/api/admin/orders/${orderId}/refund`, {
    method: "POST",
    body: payload,
  });
}

export async function enqueueImageGenerationJob(payload: {
  prompt: string;
  model?: ImageModel | string;
  n?: number;
  size?: string | null;
  response_format?: string;
}) {
  return httpRequest<{ job: ImageJob }>("/api/jobs/images/generations", {
    method: "POST",
    body: payload,
  });
}

export async function fetchImageJobs(filters: { status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: ImageJob[] }>(`/api/jobs${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchImageJob(jobId: string) {
  return httpRequest<{ job: ImageJob }>(`/api/jobs/${jobId}`);
}

export async function cancelImageJob(jobId: string) {
  return httpRequest<{ job: ImageJob }>(`/api/jobs/${jobId}/cancel`, {
    method: "POST",
    body: {},
  });
}

export async function fetchAdminImageJobs(filters: { status?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: ImageJob[] }>(`/api/admin/jobs${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchAdminDeadLetterImageJobs(filters: { limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: ImageJob[] }>(`/api/admin/jobs/dead-letter${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function runNextAdminImageJob() {
  return httpRequest<{ job: ImageJob | null }>("/api/admin/jobs/run-next", {
    method: "POST",
    body: {},
  });
}

export async function retryAdminImageJob(jobId: string, reason = "admin-retry") {
  return httpRequest<{ job: ImageJob }>(`/api/admin/jobs/${jobId}/retry`, {
    method: "POST",
    body: { reason },
  });
}

export async function recoverStaleAdminImageJobs(staleAfterSeconds?: number) {
  const params = new URLSearchParams();
  if (staleAfterSeconds) params.set("stale_after_seconds", String(staleAfterSeconds));
  return httpRequest<{ items: ImageJob[]; recovered: number }>(
    `/api/admin/jobs/recover-stale${params.toString() ? `?${params.toString()}` : ""}`,
    {
      method: "POST",
      body: {},
    },
  );
}

export async function fetchImageAssets(filters: { start_date?: string; end_date?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.start_date) params.set("start_date", filters.start_date);
  if (filters.end_date) params.set("end_date", filters.end_date);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: ImageAsset[] }>(`/api/assets${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchImageAsset(assetId: string) {
  return httpRequest<{ asset: ImageAsset }>(`/api/assets/${assetId}`);
}

export async function deleteImageAsset(assetId: string) {
  return httpRequest<{ asset: ImageAsset }>(`/api/assets/${assetId}`, {
    method: "DELETE",
  });
}

export async function fetchAdminImageAssets(filters: { start_date?: string; end_date?: string; limit?: number; include_deleted?: boolean } = {}) {
  const params = new URLSearchParams();
  if (filters.start_date) params.set("start_date", filters.start_date);
  if (filters.end_date) params.set("end_date", filters.end_date);
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.include_deleted) params.set("include_deleted", "true");
  return httpRequest<{ items: ImageAsset[] }>(`/api/admin/assets${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function deleteAdminImageAsset(assetId: string) {
  return httpRequest<{ asset: ImageAsset }>(`/api/admin/assets/${assetId}`, {
    method: "DELETE",
  });
}

export async function fetchAccounts() {
  return httpRequest<AccountListResponse>("/api/accounts");
}

export async function createAccounts(tokens: string[]) {
  return httpRequest<AccountMutationResponse>("/api/accounts", {
    method: "POST",
    body: { tokens },
  });
}

export async function deleteAccounts(tokens: string[]) {
  return httpRequest<AccountMutationResponse>("/api/accounts", {
    method: "DELETE",
    body: { tokens },
  });
}

export async function refreshAccounts(accessTokens: string[]) {
  return httpRequest<AccountRefreshResponse>("/api/accounts/refresh", {
    method: "POST",
    body: { access_tokens: accessTokens },
  });
}

export async function updateAccount(
  accessToken: string,
  updates: {
    type?: AccountType;
    status?: AccountStatus;
    quota?: number;
  },
) {
  return httpRequest<AccountUpdateResponse>("/api/accounts/update", {
    method: "POST",
    body: {
      access_token: accessToken,
      ...updates,
    },
  });
}

type AccountBatchUpdateResponse = {
  updated: number;
  items: Account[];
};

export async function batchUpdateAccounts(
  accessTokens: string[],
  updates: {
    type?: AccountType;
    status?: AccountStatus;
    quota?: number;
  },
) {
  return httpRequest<AccountBatchUpdateResponse>("/api/accounts/batch-update", {
    method: "POST",
    body: {
      access_tokens: accessTokens,
      ...updates,
    },
  });
}

export async function generateImage(prompt: string, model?: ImageModel, size?: string) {
  return httpRequest<{ created: number; data: Array<{ b64_json: string; revised_prompt?: string }> }>(
    "/v1/images/generations",
    {
      method: "POST",
      body: {
        prompt,
        ...(model ? { model } : {}),
        ...(size ? { size } : {}),
        n: 1,
        response_format: "b64_json",
      },
    },
  );
}

export async function editImage(files: File | File[], prompt: string, model?: ImageModel, size?: string) {
  const formData = new FormData();
  const uploadFiles = Array.isArray(files) ? files : [files];

  uploadFiles.forEach((file) => {
    formData.append("image", file);
  });
  formData.append("prompt", prompt);
  if (model) {
    formData.append("model", model);
  }
  if (size) {
    formData.append("size", size);
  }
  formData.append("n", "1");

  return httpRequest<{ created: number; data: Array<{ b64_json: string; revised_prompt?: string }> }>(
    "/v1/images/edits",
    {
      method: "POST",
      body: formData,
    },
  );
}

export async function fetchSettingsConfig() {
  return httpRequest<{ config: SettingsConfig }>("/api/settings");
}

export async function updateSettingsConfig(settings: SettingsConfig) {
  return httpRequest<{ config: SettingsConfig }>("/api/settings", {
    method: "POST",
    body: settings,
  });
}

export async function fetchManagedImages(filters: { start_date?: string; end_date?: string }) {
  const params = new URLSearchParams();
  if (filters.start_date) params.set("start_date", filters.start_date);
  if (filters.end_date) params.set("end_date", filters.end_date);
  return httpRequest<{ items: ManagedImage[]; groups: Array<{ date: string; items: ManagedImage[] }> }>(
    `/api/images${params.toString() ? `?${params.toString()}` : ""}`,
  );
}

export async function fetchSystemLogs(filters: { type?: string; start_date?: string; end_date?: string }) {
  const params = new URLSearchParams();
  if (filters.type) params.set("type", filters.type);
  if (filters.start_date) params.set("start_date", filters.start_date);
  if (filters.end_date) params.set("end_date", filters.end_date);
  return httpRequest<{ items: SystemLog[] }>(`/api/logs${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchAuditLogs(filters: { action?: string; actor_id?: string; target_type?: string; target_id?: string; start_date?: string; end_date?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.action) params.set("action", filters.action);
  if (filters.actor_id) params.set("actor_id", filters.actor_id);
  if (filters.target_type) params.set("target_type", filters.target_type);
  if (filters.target_id) params.set("target_id", filters.target_id);
  if (filters.start_date) params.set("start_date", filters.start_date);
  if (filters.end_date) params.set("end_date", filters.end_date);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: AuditLog[] }>(`/api/admin/audit-logs${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchProductionReadiness(strict = true) {
  const params = new URLSearchParams({ strict: strict ? "true" : "false" });
  return httpRequest<ProductionReadinessResult>(`/api/admin/production-readiness?${params.toString()}`);
}

export async function fetchBusinessReport(days = 30) {
  const params = new URLSearchParams({ days: String(days) });
  return httpRequest<BusinessReport>(`/api/admin/business-report?${params.toString()}`);
}

export async function fetchLaunchEvidence(limit = 20) {
  const params = new URLSearchParams({ limit: String(limit) });
  return httpRequest<{ items: LaunchEvidenceItem[] }>(`/api/admin/launch-evidence?${params.toString()}`);
}

export async function fetchLaunchEvidenceDetail(evidenceId: string) {
  return httpRequest<{ item: LaunchEvidenceItem }>(`/api/admin/launch-evidence/${evidenceId}`);
}

export async function createLaunchEvidence(payload: { name?: string; source?: string; report: Record<string, unknown> }) {
  return httpRequest<{ item: LaunchEvidenceItem }>("/api/admin/launch-evidence", {
    method: "POST",
    body: payload,
  });
}

export async function deleteLaunchEvidence(evidenceId: string) {
  return httpRequest<{ ok: boolean }>(`/api/admin/launch-evidence/${evidenceId}`, {
    method: "DELETE",
  });
}

export async function replayPaymentWebhook(payload: {
  provider?: string;
  secret?: string;
  package_id?: string;
  amount_cents?: number;
  currency?: string;
  quota?: number;
  run_refund?: boolean;
  archive?: boolean;
  evidence_name?: string;
  email?: string;
}) {
  return httpRequest<{ report: PaymentWebhookReplayReport; item?: LaunchEvidenceItem | null }>("/api/admin/payment-webhook/replay", {
    method: "POST",
    body: payload,
  });
}


export async function replayCheckoutWebhook(payload: {
  checkout_provider?: string;
  webhook_provider?: string;
  secret?: string;
  amount_cents?: number;
  currency?: string;
  quota?: number;
  run_refund?: boolean;
  archive?: boolean;
  evidence_name?: string;
  email?: string;
}) {
  return httpRequest<{ report: PaymentWebhookReplayReport; item?: LaunchEvidenceItem | null }>("/api/admin/checkout-webhook/replay", {
    method: "POST",
    body: payload,
  });
}


export async function createSupportTicket(payload: {
  subject: string;
  message: string;
  category?: string;
  priority?: string;
  metadata?: Record<string, unknown>;
}) {
  return httpRequest<{ item: SupportTicketItem }>("/api/support/tickets", {
    method: "POST",
    body: payload,
  });
}

export async function fetchSupportTickets(filters: { status?: string; priority?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.priority) params.set("priority", filters.priority);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: SupportTicketItem[] }>(`/api/support/tickets${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchSupportTicket(ticketId: string) {
  return httpRequest<{ item: SupportTicketItem }>(`/api/support/tickets/${ticketId}`);
}

export async function addSupportTicketMessage(ticketId: string, message: string) {
  return httpRequest<{ item: SupportTicketItem }>(`/api/support/tickets/${ticketId}/messages`, {
    method: "POST",
    body: { message },
  });
}

export async function uploadSupportTicketAttachment(ticketId: string, file: File, message = "") {
  const formData = new FormData();
  formData.append("file", file);
  if (message) formData.append("message", message);
  return httpRequest<{ item: SupportTicketItem }>(`/api/support/tickets/${ticketId}/attachments`, {
    method: "POST",
    body: formData,
  });
}

export async function fetchAdminSupportTickets(filters: { status?: string; priority?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.priority) params.set("priority", filters.priority);
  if (filters.limit) params.set("limit", String(filters.limit));
  return httpRequest<{ items: SupportTicketItem[] }>(`/api/admin/support/tickets${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function fetchAdminSupportTicket(ticketId: string) {
  return httpRequest<{ item: SupportTicketItem }>(`/api/admin/support/tickets/${ticketId}`);
}

export async function updateAdminSupportTicket(
  ticketId: string,
  payload: {
    status?: string | null;
    priority?: string | null;
    assignee_id?: string | null;
    assignee_name?: string | null;
    tags?: string[] | null;
    metadata?: Record<string, unknown> | null;
  },
) {
  return httpRequest<{ item: SupportTicketItem }>(`/api/admin/support/tickets/${ticketId}`, {
    method: "POST",
    body: payload,
  });
}

export async function addAdminSupportTicketMessage(ticketId: string, message: string, internal = false) {
  return httpRequest<{ item: SupportTicketItem }>(`/api/admin/support/tickets/${ticketId}/messages`, {
    method: "POST",
    body: { message, internal },
  });
}

export async function uploadAdminSupportTicketAttachment(ticketId: string, file: File, message = "", internal = false) {
  const formData = new FormData();
  formData.append("file", file);
  if (message) formData.append("message", message);
  formData.append("internal", String(internal));
  return httpRequest<{ item: SupportTicketItem }>(`/api/admin/support/tickets/${ticketId}/attachments`, {
    method: "POST",
    body: formData,
  });
}

export async function fetchUserKeys() {
  return httpRequest<{ items: UserKey[] }>("/api/auth/users");
}

export async function createUserKey(payload: string | UserKeyPayload) {
  const body = typeof payload === "string" ? { name: payload } : payload;
  return httpRequest<{ item: UserKey; key: string; items: UserKey[] }>("/api/auth/users", {
    method: "POST",
    body,
  });
}

export async function updateUserKey(keyId: string, updates: UserKeyPayload) {
  return httpRequest<{ item: UserKey; items: UserKey[] }>(`/api/auth/users/${keyId}`, {
    method: "POST",
    body: updates,
  });
}

export async function deleteUserKey(keyId: string) {
  return httpRequest<{ items: UserKey[] }>(`/api/auth/users/${keyId}`, {
    method: "DELETE",
  });
}

export async function fetchRegisteredUsers() {
  return httpRequest<{ items: RegisteredUser[] }>("/api/admin/users");
}

export async function createRegisteredUser(payload: { email: string; password: string; name?: string; quota_balance?: number }) {
  return httpRequest<{ item: RegisteredUser; token: string; key: UserKey; items: RegisteredUser[] }>("/api/admin/users", {
    method: "POST",
    body: payload,
  });
}

export async function updateRegisteredUser(userId: string, updates: Partial<RegisteredUser>) {
  return httpRequest<{ item: RegisteredUser; items: RegisteredUser[] }>(`/api/admin/users/${userId}`, {
    method: "POST",
    body: updates,
  });
}

export async function adjustRegisteredUserQuota(userId: string, delta: number, reason = "") {
  return httpRequest<{ item: RegisteredUser; items: RegisteredUser[] }>(`/api/admin/users/${userId}/quota`, {
    method: "POST",
    body: { delta, reason },
  });
}

export async function resetRegisteredUserPassword(userId: string, password: string) {
  return httpRequest<{ item: RegisteredUser }>(`/api/admin/users/${userId}/password`, {
    method: "POST",
    body: { password },
  });
}

export async function fetchPackages() {
  return httpRequest<{ items: PackageItem[] }>("/api/admin/packages");
}

export async function createPackage(payload: { name: string; description?: string; quota: number; price_cents?: number; currency?: string; valid_days?: number | null }) {
  return httpRequest<{ item: PackageItem; items: PackageItem[] }>("/api/admin/packages", { method: "POST", body: payload });
}

export async function updatePackage(packageId: string, updates: Partial<PackageItem>) {
  return httpRequest<{ item: PackageItem; items: PackageItem[] }>(`/api/admin/packages/${packageId}`, { method: "POST", body: updates });
}

export async function fetchCDKs() {
  return httpRequest<{ items: CDKItem[] }>("/api/admin/cdks");
}

export async function createCDKs(payload: { name: string; type: "quota" | "package"; count: number; quota?: number; package_id?: string | null; max_redemptions?: number; per_user_limit?: number; expires_at?: string | null }) {
  return httpRequest<{ items: CDKItem[]; created: CDKItem[]; codes: string[] }>("/api/admin/cdks", { method: "POST", body: payload });
}

export async function updateCDK(cdkId: string, updates: Partial<CDKItem>) {
  return httpRequest<{ item: CDKItem; items: CDKItem[] }>(`/api/admin/cdks/${cdkId}`, { method: "POST", body: updates });
}

export async function fetchRedemptions() {
  return httpRequest<{ items: RedemptionRecord[] }>("/api/admin/redemptions");
}

// ── CPA (CLIProxyAPI) ──────────────────────────────────────────────


export type SupportTicketAttachment = {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  object_key?: string | null;
  url?: string | null;
  uploaded_by?: "user" | "admin" | "system" | string | null;
  uploader_id?: string | null;
  internal?: boolean;
  created_at: string;
};

export type SupportTicketMessage = {
  id: string;
  author_type: "user" | "admin" | "system" | string;
  author_id?: string | null;
  author_email?: string | null;
  author_name?: string | null;
  body: string;
  internal?: boolean;
  created_at: string;
  attachments?: SupportTicketAttachment[];
};

export type SupportTicketItem = {
  id: string;
  user_id?: string | null;
  email?: string | null;
  name?: string | null;
  subject: string;
  category: string;
  priority: "low" | "normal" | "high" | "urgent" | string;
  status: "open" | "in_progress" | "resolved" | "closed" | string;
  assignee_id?: string | null;
  assignee_name?: string | null;
  created_at: string;
  updated_at: string;
  last_message_at?: string | null;
  first_response_due_at?: string | null;
  first_response_at?: string | null;
  resolution_due_at?: string | null;
  resolved_at?: string | null;
  closed_at?: string | null;
  sla_status?: "on_track" | "response_overdue" | "resolution_overdue" | "resolved" | string;
  overdue_seconds?: number;
  response_overdue_seconds?: number;
  resolution_overdue_seconds?: number;
  tags?: string[];
  metadata?: Record<string, unknown>;
  notifications?: Array<Record<string, unknown>>;
  message_count: number;
  messages?: SupportTicketMessage[];
};

export type CPAPool = {
  id: string;
  name: string;
  base_url: string;
  import_job?: CPAImportJob | null;
};

export type CPARemoteFile = {
  name: string;
  email: string;
};

export type CPAImportJob = {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed";
  created_at: string;
  updated_at: string;
  total: number;
  completed: number;
  added: number;
  skipped: number;
  refreshed: number;
  failed: number;
  errors: Array<{ name: string; error: string }>;
};

export async function fetchCPAPools() {
  return httpRequest<{ pools: CPAPool[] }>("/api/cpa/pools");
}

export async function createCPAPool(pool: { name: string; base_url: string; secret_key: string }) {
  return httpRequest<{ pool: CPAPool; pools: CPAPool[] }>("/api/cpa/pools", {
    method: "POST",
    body: pool,
  });
}

export async function updateCPAPool(
  poolId: string,
  updates: { name?: string; base_url?: string; secret_key?: string },
) {
  return httpRequest<{ pool: CPAPool; pools: CPAPool[] }>(`/api/cpa/pools/${poolId}`, {
    method: "POST",
    body: updates,
  });
}

export async function deleteCPAPool(poolId: string) {
  return httpRequest<{ pools: CPAPool[] }>(`/api/cpa/pools/${poolId}`, {
    method: "DELETE",
  });
}

export async function fetchCPAPoolFiles(poolId: string) {
  return httpRequest<{ pool_id: string; files: CPARemoteFile[] }>(`/api/cpa/pools/${poolId}/files`);
}

export async function startCPAImport(poolId: string, names: string[]) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/cpa/pools/${poolId}/import`, {
    method: "POST",
    body: { names },
  });
}

export async function fetchCPAPoolImportJob(poolId: string) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/cpa/pools/${poolId}/import`);
}

// ── Sub2API ────────────────────────────────────────────────────────

export type Sub2APIServer = {
  id: string;
  name: string;
  base_url: string;
  email: string;
  has_api_key: boolean;
  group_id: string;
  import_job?: CPAImportJob | null;
};

export type Sub2APIRemoteAccount = {
  id: string;
  name: string;
  email: string;
  plan_type: string;
  status: string;
  expires_at: string;
  has_refresh_token: boolean;
};

export type Sub2APIRemoteGroup = {
  id: string;
  name: string;
  description: string;
  platform: string;
  status: string;
  account_count: number;
  active_account_count: number;
};

export async function fetchSub2APIServers() {
  return httpRequest<{ servers: Sub2APIServer[] }>("/api/sub2api/servers");
}

export async function createSub2APIServer(server: {
  name: string;
  base_url: string;
  email: string;
  password: string;
  api_key: string;
  group_id: string;
}) {
  return httpRequest<{ server: Sub2APIServer; servers: Sub2APIServer[] }>("/api/sub2api/servers", {
    method: "POST",
    body: server,
  });
}

export async function updateSub2APIServer(
  serverId: string,
  updates: {
    name?: string;
    base_url?: string;
    email?: string;
    password?: string;
    api_key?: string;
    group_id?: string;
  },
) {
  return httpRequest<{ server: Sub2APIServer; servers: Sub2APIServer[] }>(`/api/sub2api/servers/${serverId}`, {
    method: "POST",
    body: updates,
  });
}

export async function fetchSub2APIServerGroups(serverId: string) {
  return httpRequest<{ server_id: string; groups: Sub2APIRemoteGroup[] }>(
    `/api/sub2api/servers/${serverId}/groups`,
  );
}

export async function deleteSub2APIServer(serverId: string) {
  return httpRequest<{ servers: Sub2APIServer[] }>(`/api/sub2api/servers/${serverId}`, {
    method: "DELETE",
  });
}

export async function fetchSub2APIServerAccounts(serverId: string) {
  return httpRequest<{ server_id: string; accounts: Sub2APIRemoteAccount[] }>(
    `/api/sub2api/servers/${serverId}/accounts`,
  );
}

export async function startSub2APIImport(serverId: string, accountIds: string[]) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/sub2api/servers/${serverId}/import`, {
    method: "POST",
    body: { account_ids: accountIds },
  });
}

export async function fetchSub2APIImportJob(serverId: string) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/sub2api/servers/${serverId}/import`);
}

// ── Upstream proxy ────────────────────────────────────────────────

export type ProxySettings = {
  enabled: boolean;
  url: string;
};

export type ProxyTestResult = {
  ok: boolean;
  status: number;
  latency_ms: number;
  error: string | null;
};

export async function fetchProxy() {
  return httpRequest<{ proxy: ProxySettings }>("/api/proxy");
}

export async function updateProxy(updates: { enabled?: boolean; url?: string }) {
  return httpRequest<{ proxy: ProxySettings }>("/api/proxy", {
    method: "POST",
    body: updates,
  });
}

export async function testProxy(url?: string) {
  return httpRequest<{ result: ProxyTestResult }>("/api/proxy/test", {
    method: "POST",
    body: { url: url ?? "" },
  });
}
