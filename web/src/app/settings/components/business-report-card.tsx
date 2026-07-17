"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { BarChart3, Database, Image as ImageIcon, LoaderCircle, RefreshCw, TrendingUp, Users, WalletCards } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { fetchBusinessReport, type BusinessReport, type BusinessReportPeriod } from "@/lib/api";

function formatDateTime(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatMoney(cents: number, currency = "CNY") {
  return `${currency} ${(Number(cents || 0) / 100).toFixed(2)}`;
}

function formatMoneyMap(values?: Record<string, number>, fallback = "0.00") {
  const entries = Object.entries(values || {});
  if (!entries.length) return fallback;
  return entries.map(([currency, cents]) => formatMoney(cents, currency)).join(" / ");
}

function formatPercent(value?: number | null) {
  if (value === null || value === undefined) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

function formatBytes(value?: number | null) {
  const size = Number(value || 0);
  if (size >= 1024 * 1024 * 1024) return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(2)} MB`;
  if (size >= 1024) return `${Math.ceil(size / 1024)} KB`;
  return `${size} B`;
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    cancelled: "已取消",
    created: "已创建",
    failed: "失败",
    fulfilled: "已履约",
    paid: "已支付",
    pending_payment: "待支付",
    queued: "排队中",
    refunded: "已退款",
    running: "执行中",
    succeeded: "成功",
  };
  return labels[status] || status;
}

function typeLabel(type: string) {
  const labels: Record<string, string> = {
    adjust: "调整",
    consume: "消耗",
    grant: "发放",
    package: "套餐",
    quota: "额度",
    refund: "退款",
    set: "设置",
  };
  return labels[type] || type;
}

function MiniMetric({
  title,
  value,
  description,
  tone = "stone",
}: {
  title: string;
  value: string | number;
  description?: string;
  tone?: "stone" | "emerald" | "amber" | "rose" | "sky";
}) {
  const toneClass = {
    amber: "bg-amber-50 text-amber-700",
    emerald: "bg-emerald-50 text-emerald-700",
    rose: "bg-rose-50 text-rose-700",
    sky: "bg-sky-50 text-sky-700",
    stone: "bg-stone-50 text-stone-700",
  }[tone];
  return (
    <div className={`rounded-xl p-4 ${toneClass}`}>
      <div className="text-xs opacity-80">{title}</div>
      <div className="mt-1 truncate text-2xl font-semibold" title={String(value)}>
        {value}
      </div>
      {description ? <div className="mt-1 text-xs opacity-70">{description}</div> : null}
    </div>
  );
}

function CountMapList({
  title,
  items,
  labeler = statusLabel,
}: {
  title: string;
  items?: Record<string, number>;
  labeler?: (key: string) => string;
}) {
  const entries = Object.entries(items || {}).sort((a, b) => b[1] - a[1]);
  return (
    <div className="rounded-xl border border-stone-100 bg-white p-4">
      <div className="text-sm font-semibold text-stone-800">{title}</div>
      <div className="mt-3 flex flex-wrap gap-2">
        {entries.length ? entries.map(([key, count]) => (
          <Badge key={key} variant="secondary" className="rounded-lg">
            {labeler(key)}：{count}
          </Badge>
        )) : <span className="text-xs text-stone-400">暂无数据</span>}
      </div>
    </div>
  );
}

function PeriodDetails({ period, title }: { period: BusinessReportPeriod; title: string }) {
  return (
    <div className="space-y-3 rounded-2xl border border-stone-100 bg-stone-50/60 p-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-stone-800">{title}</h3>
        {period.start_at ? <span className="text-xs text-stone-400">起始 {formatDateTime(period.start_at)}</span> : null}
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <MiniMetric title="订单数" value={period.orders.total} description={`待支付 ${period.orders.pending_total} / 履约 ${period.orders.fulfilled_total}`} />
        <MiniMetric title="支付收入" value={formatMoneyMap(period.payments.gross_revenue_cents_by_currency)} description={`成功支付 ${period.payments.succeeded_total}`} tone="emerald" />
        <MiniMetric title="图片任务" value={period.image_jobs.total} description={`成功率 ${formatPercent(period.image_jobs.success_rate)}`} tone="sky" />
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        <CountMapList title="订单状态" items={period.orders.by_status} />
        <CountMapList title="图片任务状态" items={period.image_jobs.by_status} />
        <CountMapList title="额度流水类型" items={period.quota.by_type} labeler={typeLabel} />
      </div>
    </div>
  );
}

export function BusinessReportCard() {
  const [days, setDays] = useState(30);
  const [report, setReport] = useState<BusinessReport | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const load = useCallback(async (selectedDays = days) => {
    setIsLoading(true);
    try {
      const data = await fetchBusinessReport(selectedDays);
      setReport(data);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "经营报表加载失败");
    } finally {
      setIsLoading(false);
    }
  }, [days]);

  useEffect(() => {
    void load(days);
  }, [days, load]);

  const windowPeriod = report?.window;
  const allTime = report?.all_time;
  const margin = useMemo(
    () => formatMoneyMap(report?.summary.window_estimated_gross_margin_cents_by_currency),
    [report],
  );
  const revenue = useMemo(
    () => formatMoneyMap(report?.summary.window_gross_revenue_cents_by_currency),
    [report],
  );

  return (
    <Card className="overflow-hidden border-white/80 bg-white/90">
      <CardContent className="space-y-5 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <BarChart3 className="size-5 text-stone-700" />
              <h2 className="text-lg font-semibold text-stone-900">经营报表</h2>
              {report ? <Badge variant={report.summary.image_jobs_dead_letter_total ? "warning" : "success"}>可运营指标</Badge> : null}
            </div>
            <p className="max-w-3xl text-sm leading-6 text-stone-500">
              汇总用户、订单、支付、额度流水、图片任务、资产存储和估算毛利，便于正式对外服务后的日常运营复盘。
              成本估算来自环境变量 COST_PER_IMAGE_CENTS / COST_CURRENCY。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {[7, 30, 90].map((value) => (
              <Button
                key={value}
                variant={days === value ? "default" : "outline"}
                className="h-9 rounded-xl"
                onClick={() => setDays(value)}
              >
                {value} 天
              </Button>
            ))}
            <Button variant="outline" className="h-9 rounded-xl" onClick={() => void load()} disabled={isLoading}>
              {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
              刷新
            </Button>
          </div>
        </div>

        {isLoading && !report ? (
          <div className="flex items-center justify-center rounded-xl bg-stone-50 p-8 text-sm text-stone-500">
            <LoaderCircle className="mr-2 size-4 animate-spin" />
            正在生成经营报表...
          </div>
        ) : null}

        {report && windowPeriod ? (
          <>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
              <MiniMetric
                title={`近 ${report.window_days} 天收入`}
                value={revenue}
                description={`累计 ${formatMoneyMap(report.summary.gross_revenue_cents_by_currency)}`}
                tone="emerald"
              />
              <MiniMetric title="估算毛利" value={margin} description={`图片成本 ${formatMoney(report.summary.window_estimated_image_cost_cents, report.cost_currency)}`} tone="sky" />
              <MiniMetric title="用户规模" value={report.summary.users_total} description={`启用 ${report.summary.users_enabled_total} / 余额 ${report.summary.quota_balance_total}`} />
              <MiniMetric title="图片成功率" value={formatPercent(report.summary.window_image_jobs_success_rate)} description={`死信 ${windowPeriod.image_jobs.dead_letter_total}`} tone={windowPeriod.image_jobs.dead_letter_total ? "amber" : "emerald"} />
              <MiniMetric title="资产容量" value={formatBytes(report.summary.image_assets_active_bytes)} description={`活跃资产 ${report.summary.image_assets_active_total}`} />
              <MiniMetric title="待处理订单" value={report.summary.orders_pending_total} description={`已履约 ${report.summary.orders_fulfilled_total}`} tone={report.summary.orders_pending_total ? "amber" : "stone"} />
            </div>

            <div className="grid gap-3 md:grid-cols-4">
              <div className="rounded-xl border border-stone-100 bg-white p-4">
                <div className="flex items-center gap-2 text-sm font-semibold text-stone-800">
                  <Users className="size-4" />
                  用户
                </div>
                <div className="mt-3 text-xs leading-6 text-stone-500">
                  新增 {windowPeriod.users.total}；启用 {windowPeriod.users.enabled}；有余额 {windowPeriod.users.with_positive_quota}
                </div>
              </div>
              <div className="rounded-xl border border-stone-100 bg-white p-4">
                <div className="flex items-center gap-2 text-sm font-semibold text-stone-800">
                  <WalletCards className="size-4" />
                  额度
                </div>
                <div className="mt-3 text-xs leading-6 text-stone-500">
                  发放 {windowPeriod.quota.granted_units}；消耗 {windowPeriod.quota.consumed_units}；净变化 {windowPeriod.quota.net_units}
                </div>
              </div>
              <div className="rounded-xl border border-stone-100 bg-white p-4">
                <div className="flex items-center gap-2 text-sm font-semibold text-stone-800">
                  <ImageIcon className="size-4" />
                  图片任务
                </div>
                <div className="mt-3 text-xs leading-6 text-stone-500">
                  成功 {windowPeriod.image_jobs.succeeded_total}；失败 {windowPeriod.image_jobs.failed_total}；成本单位 {windowPeriod.image_jobs.cost_units}
                </div>
              </div>
              <div className="rounded-xl border border-stone-100 bg-white p-4">
                <div className="flex items-center gap-2 text-sm font-semibold text-stone-800">
                  <Database className="size-4" />
                  存储
                </div>
                <div className="mt-3 text-xs leading-6 text-stone-500">
                  新增资产 {windowPeriod.image_assets.total}；活跃容量 {formatBytes(windowPeriod.image_assets.active_size_bytes)}
                </div>
              </div>
            </div>

            <PeriodDetails period={windowPeriod} title={`近 ${report.window_days} 天明细`} />
            {allTime ? <PeriodDetails period={allTime} title="累计明细" /> : null}

            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-stone-100 bg-stone-50 p-3 text-xs text-stone-500">
              <TrendingUp className="size-4" />
              报表生成时间：{formatDateTime(report.generated_at)}；窗口：{formatDateTime(report.window_start)} - {formatDateTime(report.window_end)}
            </div>
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}
