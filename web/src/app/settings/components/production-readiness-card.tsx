"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Eye, LoaderCircle, RefreshCw, ShieldCheck, Trash2, Upload, XCircle } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  createLaunchEvidence,
  deleteLaunchEvidence,
  fetchLaunchEvidence,
  fetchLaunchEvidenceDetail,
  fetchProductionReadiness,
  replayCheckoutWebhook,
  replayPaymentWebhook,
  type LaunchEvidenceItem,
  type ProductionReadinessItem,
  type ProductionReadinessResult,
} from "@/lib/api";

function statusLabel(status: string) {
  if (status === "passed") return "通过";
  if (status === "warning") return "警告";
  if (status === "failed") return "失败";
  return status;
}

function statusVariant(status: string) {
  if (status === "passed") return "success" as const;
  if (status === "warning") return "warning" as const;
  if (status === "failed") return "danger" as const;
  return "secondary" as const;
}

function statusIcon(status: string) {
  if (status === "passed") return <CheckCircle2 className="size-4 text-emerald-600" />;
  if (status === "warning") return <AlertTriangle className="size-4 text-amber-600" />;
  if (status === "failed") return <XCircle className="size-4 text-rose-600" />;
  return <ShieldCheck className="size-4 text-stone-500" />;
}

function formatDetail(detail?: Record<string, unknown>) {
  if (!detail) return "";
  try {
    return JSON.stringify(detail, null, 2);
  } catch {
    return "";
  }
}

function formatDateTime(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function ReadinessRow({ item }: { item: ProductionReadinessItem }) {
  const detail = formatDetail(item.detail);
  return (
    <div className="rounded-xl border border-stone-100 bg-white p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2">
          <div className="mt-0.5">{statusIcon(item.status)}</div>
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-stone-800" title={item.id}>
              {item.id}
            </div>
            <div className="mt-1 text-xs leading-5 text-stone-500">{item.message}</div>
          </div>
        </div>
        <Badge variant={statusVariant(item.status)}>{statusLabel(item.status)}</Badge>
      </div>
      {detail ? (
        <pre className="mt-3 max-h-40 overflow-auto rounded-lg bg-stone-50 p-3 text-xs leading-5 text-stone-600">
          {detail}
        </pre>
      ) : null}
    </div>
  );
}

export function ProductionReadinessCard() {
  const [data, setData] = useState<ProductionReadinessResult | null>(null);
  const [evidenceItems, setEvidenceItems] = useState<LaunchEvidenceItem[]>([]);
  const [selectedEvidence, setSelectedEvidence] = useState<LaunchEvidenceItem | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isEvidenceLoading, setIsEvidenceLoading] = useState(false);
  const [isEvidenceSaving, setIsEvidenceSaving] = useState(false);
  const [isCheckoutWebhookReplaying, setIsCheckoutWebhookReplaying] = useState(false);
  const [isWebhookReplaying, setIsWebhookReplaying] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [evidenceName, setEvidenceName] = useState("");
  const [evidenceJson, setEvidenceJson] = useState("");
  const [webhookProvider, setWebhookProvider] = useState("stripe");
  const [webhookSecret, setWebhookSecret] = useState("");
  const [webhookAmountCents, setWebhookAmountCents] = useState("1990");
  const [webhookQuota, setWebhookQuota] = useState("1");
  const [checkoutProvider, setCheckoutProvider] = useState("");
  const [checkoutWebhookProvider, setCheckoutWebhookProvider] = useState("stripe");
  const [checkoutWebhookSecret, setCheckoutWebhookSecret] = useState("");
  const [checkoutAmountCents, setCheckoutAmountCents] = useState("1990");
  const [checkoutQuota, setCheckoutQuota] = useState("1");

  const failedOrWarning = useMemo(
    () => (data?.items || []).filter((item) => item.status !== "passed"),
    [data],
  );
  const visibleItems = showAll ? data?.items || [] : failedOrWarning.slice(0, 8);

  const load = async () => {
    setIsLoading(true);
    try {
      const result = await fetchProductionReadiness(true);
      setData(result);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "生产预检加载失败");
    } finally {
      setIsLoading(false);
    }
  };

  const loadEvidence = async () => {
    setIsEvidenceLoading(true);
    try {
      const result = await fetchLaunchEvidence(10);
      setEvidenceItems(result.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "上线证据加载失败");
    } finally {
      setIsEvidenceLoading(false);
    }
  };

  const handleUploadEvidence = async () => {
    if (!evidenceJson.trim()) {
      toast.error("请粘贴 verify_production_deployment.py 生成的 JSON 报告");
      return;
    }
    let report: Record<string, unknown>;
    try {
      const parsed = JSON.parse(evidenceJson) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("report must be an object");
      }
      report = parsed as Record<string, unknown>;
    } catch {
      toast.error("JSON 格式无效");
      return;
    }
    setIsEvidenceSaving(true);
    try {
      await createLaunchEvidence({
        name: evidenceName.trim() || `launch evidence ${new Date().toLocaleString()}`,
        source: "admin-ui",
        report,
      });
      setEvidenceName("");
      setEvidenceJson("");
      toast.success("上线验收证据已保存");
      await loadEvidence();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存上线证据失败");
    } finally {
      setIsEvidenceSaving(false);
    }
  };

  const handleCheckoutWebhookReplay = async () => {
    const amountCents = Number(checkoutAmountCents);
    const quota = Number(checkoutQuota);
    if (!Number.isFinite(amountCents) || amountCents <= 0) {
      toast.error("请输入大于 0 的 Checkout 金额（分）");
      return;
    }
    if (!Number.isFinite(quota) || quota <= 0) {
      toast.error("请输入大于 0 的验证额度");
      return;
    }
    setIsCheckoutWebhookReplaying(true);
    try {
      const result = await replayCheckoutWebhook({
        checkout_provider: checkoutProvider,
        webhook_provider: checkoutWebhookProvider,
        secret: checkoutWebhookSecret.trim(),
        amount_cents: Math.floor(amountCents),
        currency: "CNY",
        quota: Math.floor(quota),
        run_refund: true,
        archive: true,
        evidence_name: `checkout webhook replay ${checkoutProvider || "default"} ${checkoutWebhookProvider} ${new Date().toLocaleString()}`,
      });
      if (result.report.ready) {
        toast.success("Checkout → paid/refund webhook 全链路验收通过并已归档");
      } else {
        toast.error("Checkout 全链路验收失败，已保存失败报告供排查");
      }
      await loadEvidence();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Checkout 全链路验收失败");
    } finally {
      setIsCheckoutWebhookReplaying(false);
    }
  };

  const handlePaymentWebhookReplay = async () => {
    const amountCents = Number(webhookAmountCents);
    const quota = Number(webhookQuota);
    if (!Number.isFinite(amountCents) || amountCents < 0) {
      toast.error("请输入有效的支付金额（分）");
      return;
    }
    if (!Number.isFinite(quota) || quota <= 0) {
      toast.error("请输入大于 0 的验证额度");
      return;
    }
    setIsWebhookReplaying(true);
    try {
      const result = await replayPaymentWebhook({
        provider: webhookProvider,
        secret: webhookSecret.trim(),
        amount_cents: Math.floor(amountCents),
        currency: "CNY",
        quota: Math.floor(quota),
        run_refund: true,
        archive: true,
        evidence_name: `payment webhook replay ${webhookProvider} ${new Date().toLocaleString()}`,
      });
      if (result.report.ready) {
        toast.success("支付 webhook paid/refund 验收通过并已归档");
      } else {
        toast.error("支付 webhook 验收失败，已保存失败报告供排查");
      }
      await loadEvidence();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "支付 webhook 验收失败");
    } finally {
      setIsWebhookReplaying(false);
    }
  };

  const handleViewEvidence = async (item: LaunchEvidenceItem) => {
    try {
      const result = await fetchLaunchEvidenceDetail(item.id);
      setSelectedEvidence(result.item);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "读取上线证据失败");
    }
  };

  const handleDeleteEvidence = async (item: LaunchEvidenceItem) => {
    if (!window.confirm(`确认删除上线证据：${item.name || item.id}？`)) {
      return;
    }
    try {
      await deleteLaunchEvidence(item.id);
      toast.success("上线证据已删除");
      await loadEvidence();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除上线证据失败");
    }
  };

  useEffect(() => {
    void load();
    void loadEvidence();
  }, []);

  const status = data?.status || "unknown";
  const summary = data?.summary;

  return (
    <Card className="overflow-hidden border-white/80 bg-white/90">
      <CardContent className="space-y-5 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <ShieldCheck className="size-5 text-stone-700" />
              <h2 className="text-lg font-semibold text-stone-900">生产上线预检</h2>
              {data ? <Badge variant={statusVariant(status)}>{statusLabel(status)}</Badge> : null}
            </div>
            <p className="max-w-3xl text-sm leading-6 text-stone-500">
              对 APP_ENV、HTTPS/CORS、安全响应头、PostgreSQL、迁移版本、Redis 队列、对象存储、备份和告警阈值做上线前检查。
              该结果可配合 scripts/check_production_ready.py 和 scripts/verify_production_deployment.py 作为正式发布前证据。
            </p>
          </div>
          <Button variant="outline" className="h-10 rounded-xl" onClick={() => void load()} disabled={isLoading}>
            {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
            刷新预检
          </Button>
        </div>

        <div className="grid gap-3 sm:grid-cols-4">
          <div className="rounded-xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">总检查项</div>
            <div className="mt-1 text-2xl font-semibold text-stone-900">{summary?.total ?? "-"}</div>
          </div>
          <div className="rounded-xl bg-emerald-50 p-4">
            <div className="text-xs text-emerald-700">通过</div>
            <div className="mt-1 text-2xl font-semibold text-emerald-700">{summary?.passed ?? "-"}</div>
          </div>
          <div className="rounded-xl bg-amber-50 p-4">
            <div className="text-xs text-amber-700">警告</div>
            <div className="mt-1 text-2xl font-semibold text-amber-700">{summary?.warning ?? "-"}</div>
          </div>
          <div className="rounded-xl bg-rose-50 p-4">
            <div className="text-xs text-rose-700">失败</div>
            <div className="mt-1 text-2xl font-semibold text-rose-700">{summary?.failed ?? "-"}</div>
          </div>
        </div>

        {isLoading && !data ? (
          <div className="flex items-center justify-center rounded-xl bg-stone-50 p-8 text-sm text-stone-500">
            <LoaderCircle className="mr-2 size-4 animate-spin" />
            正在执行生产预检...
          </div>
        ) : null}

        {!isLoading && data && failedOrWarning.length === 0 ? (
          <div className="rounded-xl border border-emerald-100 bg-emerald-50 p-4 text-sm text-emerald-700">
            当前配置通过生产上线预检。正式上线前仍建议在公网域名执行远程验收脚本并保存 JSON 证据。
          </div>
        ) : null}

        {data && failedOrWarning.length > 0 ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-semibold text-stone-800">
                {showAll ? "全部检查项" : "需要处理的检查项"}
              </div>
              <Button variant="ghost" className="h-8 rounded-lg px-3 text-stone-500" onClick={() => setShowAll((value) => !value)}>
                {showAll ? "只看问题" : "查看全部"}
              </Button>
            </div>
            <div className="grid gap-3 lg:grid-cols-2">
              {visibleItems.map((item) => (
                <ReadinessRow key={item.id} item={item} />
              ))}
            </div>
          </div>
        ) : null}

        <div className="grid gap-4 border-t border-stone-100 pt-5 lg:grid-cols-[1.1fr_0.9fr]">
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold text-stone-800">上线验收证据</h3>
                <p className="mt-1 text-xs text-stone-500">
                  保存 verify_production_deployment.py 生成的 JSON 报告，作为正式对外前的可追溯验收记录。
                </p>
              </div>
              <Button variant="ghost" className="h-8 rounded-lg px-3 text-stone-500" onClick={() => void loadEvidence()} disabled={isEvidenceLoading}>
                {isEvidenceLoading ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                刷新
              </Button>
            </div>
            <div className="space-y-2">
              {evidenceItems.length ? evidenceItems.map((item) => (
                <div key={item.id} className="rounded-xl border border-stone-100 bg-white p-3">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <div className="truncate text-sm font-medium text-stone-800" title={item.name || item.id}>
                          {item.name || item.id}
                        </div>
                        <Badge variant={statusVariant(item.status)}>{statusLabel(item.status)}</Badge>
                      </div>
                      <div className="mt-1 text-xs text-stone-500">
                        {item.base_url || "-"} · {formatDateTime(item.created_at)}
                      </div>
                      <div className="mt-1 text-xs text-stone-500">
                        总计 {item.summary?.total ?? 0} / 通过 {item.summary?.passed ?? 0} / 警告 {item.summary?.warning ?? 0} / 失败 {item.summary?.failed ?? 0}
                      </div>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <Button variant="outline" size="sm" className="rounded-lg" onClick={() => void handleViewEvidence(item)}>
                        <Eye className="size-4" />
                        查看
                      </Button>
                      <Button variant="outline" size="sm" className="rounded-lg text-rose-600" onClick={() => void handleDeleteEvidence(item)}>
                        <Trash2 className="size-4" />
                        删除
                      </Button>
                    </div>
                  </div>
                  {item.failed_checks?.length ? (
                    <div className="mt-2 rounded-lg bg-rose-50 p-2 text-xs text-rose-700">
                      {item.failed_checks.slice(0, 3).map((check) => check.id || check.message).join("，")}
                    </div>
                  ) : null}
                </div>
              )) : (
                <div className="rounded-xl bg-stone-50 p-6 text-center text-sm text-stone-500">
                  暂无上线验收证据
                </div>
              )}
            </div>
          </div>

          <div className="space-y-4">
            <div className="space-y-3 rounded-xl border border-emerald-100 bg-emerald-50/60 p-4">
              <div>
                <h3 className="text-sm font-semibold text-stone-800">Checkout 全链路验收</h3>
                <p className="mt-1 text-xs leading-5 text-stone-500">
                  自动创建临时套餐、用户和订单，先生成 checkout 支付入口，再对同一订单签名回放 paid + refund webhook，
                  验证支付入口、履约、退款、额度扣回和清理归档。
                </p>
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                <select
                  value={checkoutProvider}
                  onChange={(event) => setCheckoutProvider(event.target.value)}
                  className="h-10 w-full rounded-xl border border-stone-200 bg-white px-3 text-sm text-stone-700 outline-none transition focus:border-emerald-300 focus:ring-2 focus:ring-emerald-100"
                >
                  <option value="">Checkout 使用环境默认</option>
                  <option value="manual">Manual</option>
                  <option value="redirect">Redirect</option>
                  <option value="stripe">Stripe</option>
                </select>
                <select
                  value={checkoutWebhookProvider}
                  onChange={(event) => setCheckoutWebhookProvider(event.target.value)}
                  className="h-10 w-full rounded-xl border border-stone-200 bg-white px-3 text-sm text-stone-700 outline-none transition focus:border-emerald-300 focus:ring-2 focus:ring-emerald-100"
                >
                  <option value="stripe">Webhook: Stripe</option>
                  <option value="alipay">Webhook: Alipay</option>
                  <option value="wechatpay">Webhook: WeChat Pay</option>
                  <option value="generic">Webhook: Generic</option>
                </select>
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                <Input
                  value={checkoutAmountCents}
                  onChange={(event) => setCheckoutAmountCents(event.target.value)}
                  type="number"
                  min={1}
                  placeholder="金额（分）"
                  className="h-10 rounded-xl bg-white"
                />
                <Input
                  value={checkoutQuota}
                  onChange={(event) => setCheckoutQuota(event.target.value)}
                  type="number"
                  min={1}
                  placeholder="验证额度"
                  className="h-10 rounded-xl bg-white"
                />
              </div>
              <Input
                value={checkoutWebhookSecret}
                onChange={(event) => setCheckoutWebhookSecret(event.target.value)}
                type="password"
                placeholder="webhook secret 留空使用服务器环境变量"
                className="h-10 rounded-xl bg-white"
              />
              <Button
                className="h-10 rounded-xl bg-emerald-600 hover:bg-emerald-700"
                onClick={() => void handleCheckoutWebhookReplay()}
                disabled={isCheckoutWebhookReplaying}
              >
                {isCheckoutWebhookReplaying ? <LoaderCircle className="size-4 animate-spin" /> : <ShieldCheck className="size-4" />}
                创建 checkout 并验收 webhook 闭环
              </Button>
            </div>

            <div className="space-y-3 rounded-xl border border-blue-100 bg-blue-50/60 p-4">
              <div>
                <h3 className="text-sm font-semibold text-stone-800">支付 webhook 一键验收</h3>
                <p className="mt-1 text-xs leading-5 text-stone-500">
                  自动创建一次性验证订单，使用服务器环境变量中的 webhook secret（也可临时输入覆盖）完成 paid + refund
                  签名回放，并归档上线证据。
                </p>
              </div>
              <select
                value={webhookProvider}
                onChange={(event) => setWebhookProvider(event.target.value)}
                className="h-10 w-full rounded-xl border border-stone-200 bg-white px-3 text-sm text-stone-700 outline-none transition focus:border-blue-300 focus:ring-2 focus:ring-blue-100"
              >
                <option value="stripe">Stripe</option>
                <option value="alipay">Alipay</option>
                <option value="wechatpay">WeChat Pay</option>
                <option value="generic">Generic</option>
              </select>
              <div className="grid gap-2 sm:grid-cols-2">
                <Input
                  value={webhookAmountCents}
                  onChange={(event) => setWebhookAmountCents(event.target.value)}
                  type="number"
                  min={0}
                  placeholder="金额（分）"
                  className="h-10 rounded-xl bg-white"
                />
                <Input
                  value={webhookQuota}
                  onChange={(event) => setWebhookQuota(event.target.value)}
                  type="number"
                  min={1}
                  placeholder="验证额度"
                  className="h-10 rounded-xl bg-white"
                />
              </div>
              <Input
                value={webhookSecret}
                onChange={(event) => setWebhookSecret(event.target.value)}
                type="password"
                placeholder="secret 留空使用服务器环境变量"
                className="h-10 rounded-xl bg-white"
              />
              <Button className="h-10 rounded-xl bg-blue-600 hover:bg-blue-700" onClick={() => void handlePaymentWebhookReplay()} disabled={isWebhookReplaying}>
                {isWebhookReplaying ? <LoaderCircle className="size-4 animate-spin" /> : <ShieldCheck className="size-4" />}
                生成订单并验收 paid/refund
              </Button>
            </div>

            <div className="space-y-3 rounded-xl border border-stone-100 bg-stone-50 p-4">
              <div>
                <h3 className="text-sm font-semibold text-stone-800">上传验收 JSON</h3>
                <p className="mt-1 text-xs text-stone-500">
                  运行远程验收脚本后，把输出 JSON 粘贴到这里归档。
                </p>
              </div>
              <Input
                value={evidenceName}
                onChange={(event) => setEvidenceName(event.target.value)}
                placeholder="证据名称，例如 2026-07-07 prod launch"
                className="h-10 rounded-xl bg-white"
              />
              <Textarea
                value={evidenceJson}
                onChange={(event) => setEvidenceJson(event.target.value)}
                placeholder='{"status":"passed","ready":true,"summary":{...},"checks":[...]}'
                className="min-h-44 rounded-xl bg-white font-mono text-xs"
              />
              <Button className="h-10 rounded-xl" onClick={() => void handleUploadEvidence()} disabled={isEvidenceSaving}>
                {isEvidenceSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Upload className="size-4" />}
                保存上线证据
              </Button>
            </div>
          </div>
        </div>

        <Dialog open={Boolean(selectedEvidence)} onOpenChange={(open) => !open && setSelectedEvidence(null)}>
          <DialogContent className="w-[min(92vw,960px)] rounded-2xl p-6">
            <DialogHeader>
              <DialogTitle>上线验收证据详情</DialogTitle>
            </DialogHeader>
            <div className="grid gap-3 text-sm text-stone-600 md:grid-cols-2">
              <div>名称：{selectedEvidence?.name || "-"}</div>
              <div>状态：{selectedEvidence ? statusLabel(selectedEvidence.status) : "-"}</div>
              <div>域名：{selectedEvidence?.base_url || "-"}</div>
              <div>创建时间：{formatDateTime(selectedEvidence?.created_at)}</div>
            </div>
            <pre className="max-h-[72vh] overflow-auto rounded-xl border border-stone-200 bg-stone-50 p-4 text-xs leading-6 text-stone-700">
              {JSON.stringify(selectedEvidence?.report || selectedEvidence || {}, null, 2)}
            </pre>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}
