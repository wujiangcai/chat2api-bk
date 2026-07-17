"use client";

import { useEffect, useState } from "react";
import { CreditCard, Download, ExternalLink, Gift, LoaderCircle, PackagePlus, RotateCcw } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  createOrder,
  createOrderCheckout,
  fetchMe,
  fetchMyQuotaLedger,
  fetchOrderReceipt,
  fetchOrders,
  fetchPublicPackages,
  redeemCode,
  type CurrentIdentity,
  type CheckoutSession,
  type OrderItem,
  type PackageItem,
  type QuotaLedgerItem,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";

function formatDateTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatMoney(cents?: number | null, currency = "CNY") {
  return `${currency} ${(Number(cents || 0) / 100).toFixed(2)}`;
}

function formatQuota(me: CurrentIdentity | null) {
  if (!me) return "—";
  if (me.quota_balance != null) return String(me.quota_balance);
  if (me.quota_remaining != null) return String(me.quota_remaining);
  if (me.quota_limit == null) return "不限";
  return "—";
}

function quotaHint(me: CurrentIdentity | null) {
  if (!me) return "加载中";
  if (me.quota_balance != null) return "注册用户余额";
  if (me.quota_limit == null) return "密钥不限额";
  return `密钥额度 ${me.quota_used ?? 0}/${me.quota_limit}`;
}

function ledgerTypeLabel(type: string) {
  const labels: Record<string, string> = {
    adjust: "人工调整",
    consume: "消费扣减",
    grant: "发放",
    refund: "退款返还",
    set: "直接设置",
  };
  return labels[type] || type;
}

function orderStatusLabel(status: string) {
  const labels: Record<string, string> = {
    created: "已创建",
    pending_payment: "待支付",
    paid: "已支付",
    fulfilled: "已履约",
    cancelled: "已取消",
    refunded: "已退款",
  };
  return labels[status] || status;
}

function amountLabel(amount: number) {
  return amount > 0 ? `+${amount}` : String(amount);
}

function downloadJson(fileName: string, value: unknown) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function getOrderCheckout(order: OrderItem): CheckoutSession | null {
  const checkout = order.metadata?.checkout;
  if (!checkout || typeof checkout !== "object" || Array.isArray(checkout)) return null;
  return checkout as CheckoutSession;
}

export default function RedeemPage() {
  const { isCheckingAuth, session } = useAuthGuard();
  const [me, setMe] = useState<CurrentIdentity | null>(null);
  const [packages, setPackages] = useState<PackageItem[]>([]);
  const [orders, setOrders] = useState<OrderItem[]>([]);
  const [quotaLedger, setQuotaLedger] = useState<QuotaLedgerItem[]>([]);
  const [code, setCode] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isRedeeming, setIsRedeeming] = useState(false);
  const [isLedgerLoading, setIsLedgerLoading] = useState(false);
  const [creatingOrderPackageId, setCreatingOrderPackageId] = useState("");
  const [checkoutOrderId, setCheckoutOrderId] = useState("");
  const [downloadingReceiptOrderId, setDownloadingReceiptOrderId] = useState("");

  const loadAccountData = async () => {
    setIsLoading(true);
    try {
      const nextMe = await fetchMe();
      setMe(nextMe);

      const packageData = await fetchPublicPackages();
      setPackages(packageData.items);

      if (nextMe.user_id) {
        setIsLedgerLoading(true);
        try {
          const [ledgerData, orderData] = await Promise.all([
            fetchMyQuotaLedger(30),
            fetchOrders({ limit: 30 }),
          ]);
          setQuotaLedger(ledgerData.items);
          setOrders(orderData.items);
        } finally {
          setIsLedgerLoading(false);
        }
      } else {
        setQuotaLedger([]);
        setOrders([]);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载用户信息失败");
    } finally {
      setIsLoading(false);
    }
  };

  const refreshLedger = async () => {
    if (!me?.user_id) return;
    setIsLedgerLoading(true);
    try {
      const [ledgerData, orderData] = await Promise.all([
        fetchMyQuotaLedger(30),
        fetchOrders({ limit: 30 }),
      ]);
      setQuotaLedger(ledgerData.items);
      setOrders(orderData.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载额度流水失败");
    } finally {
      setIsLedgerLoading(false);
    }
  };

  useEffect(() => {
    if (!session) {
      return;
    }
    void loadAccountData();
  }, [session]);

  const handleRedeem = async () => {
    if (!code.trim()) {
      toast.error("请输入 CDK");
      return;
    }
    if (!me?.user_id) {
      toast.error("CDK 仅支持邮箱注册用户兑换，请使用邮箱账号登录");
      return;
    }
    setIsRedeeming(true);
    try {
      await redeemCode(code.trim());
      setCode("");
      toast.success("兑换成功");
      await loadAccountData();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "兑换失败");
    } finally {
      setIsRedeeming(false);
    }
  };

  const handleCreateOrder = async (item: PackageItem) => {
    if (!me?.user_id) {
      toast.error("请使用邮箱注册用户登录后再创建订单");
      return;
    }
    setCreatingOrderPackageId(item.id);
    try {
      const orderResult = await createOrder({ package_id: item.id, quantity: 1 });
      toast.success("订单已创建，正在生成支付入口");
      try {
        const checkoutResult = await createOrderCheckout(orderResult.order.id);
        if (checkoutResult.checkout.payment_url) {
          toast.success("支付入口已生成，正在跳转");
          window.location.assign(checkoutResult.checkout.payment_url);
          return;
        }
        toast.success(checkoutResult.checkout.instructions || "支付说明已生成，请按订单中的说明完成支付");
      } catch (checkoutError) {
        toast.error(checkoutError instanceof Error ? checkoutError.message : "支付入口生成失败，可稍后在订单列表继续支付");
      }
      const orderData = await fetchOrders({ limit: 30 });
      setOrders(orderData.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "创建订单失败");
    } finally {
      setCreatingOrderPackageId("");
    }
  };

  const handleCreateCheckout = async (order: OrderItem) => {
    setCheckoutOrderId(order.id);
    try {
      const result = await createOrderCheckout(order.id);
      const orderData = await fetchOrders({ limit: 30 });
      setOrders(orderData.items);
      if (result.checkout.payment_url) {
        window.location.assign(result.checkout.payment_url);
        return;
      }
      toast.success(result.checkout.instructions || "支付说明已生成");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "支付入口生成失败");
    } finally {
      setCheckoutOrderId("");
    }
  };

  const handleDownloadReceipt = async (order: OrderItem) => {
    setDownloadingReceiptOrderId(order.id);
    try {
      const data = await fetchOrderReceipt(order.id);
      downloadJson(`${data.receipt.receipt_number || order.id}.json`, data.receipt);
      toast.success("收据已下载");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "下载收据失败");
    } finally {
      setDownloadingReceiptOrderId("");
    }
  };

  if (isCheckingAuth || !session) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return (
    <section className="mx-auto max-w-5xl space-y-6">
      <div className="space-y-2">
        <div className="text-xs font-semibold uppercase tracking-[0.22em] text-stone-400">Billing</div>
        <h1 className="text-3xl font-semibold tracking-tight text-stone-950">充值 / CDK 兑换</h1>
        <p className="text-sm text-stone-500">
          可通过 CDK 兑换额度，也可以创建套餐订单；订单支付确认后会自动发放额度并写入可审计流水。
        </p>
      </div>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-6 p-6">
          {isLoading ? (
            <div className="flex items-center justify-center py-8">
              <LoaderCircle className="size-5 animate-spin text-stone-400" />
            </div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-2xl bg-stone-50 p-4">
                <div className="text-xs text-stone-500">当前账号</div>
                <div className="mt-1 truncate text-sm font-medium text-stone-900">{me?.email || me?.name || "—"}</div>
                <div className="mt-1 text-xs text-stone-400">{me?.user_id ? "邮箱注册用户" : "密钥登录"}</div>
              </div>
              <div className="rounded-2xl bg-stone-50 p-4">
                <div className="text-xs text-stone-500">可用额度</div>
                <div className="mt-1 text-sm font-medium text-stone-900">{formatQuota(me)}</div>
                <div className="mt-1 text-xs text-stone-400">{quotaHint(me)}</div>
              </div>
              <div className="rounded-2xl bg-stone-50 p-4">
                <div className="text-xs text-stone-500">当前套餐</div>
                <div className="mt-1">
                  {me?.package_name ? (
                    <Badge className="rounded-md">{me.package_name}</Badge>
                  ) : (
                    <span className="text-sm font-medium text-stone-900">无</span>
                  )}
                </div>
                <div className="mt-1 text-xs text-stone-400">到期：{formatDateTime(me?.package_expires_at)}</div>
              </div>
            </div>
          )}

          {!isLoading && !me?.user_id ? (
            <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
              当前是密钥登录，只能查看密钥额度；如需兑换 CDK 或创建订单，请使用邮箱密码注册/登录。
            </div>
          ) : null}

          <div className="space-y-3">
            <label className="text-sm font-medium text-stone-700">CDK</label>
            <div className="flex flex-col gap-3 sm:flex-row">
              <Input
                value={code}
                onChange={(event) => setCode(event.target.value)}
                placeholder="XXXX-XXXX-XXXX"
                className="h-11 rounded-xl border-stone-200 bg-white"
              />
              <Button
                className="h-11 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
                onClick={() => void handleRedeem()}
                disabled={isRedeeming || !me?.user_id}
              >
                {isRedeeming ? <LoaderCircle className="size-4 animate-spin" /> : <Gift className="size-4" />}
                兑换
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-4 p-6">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight text-stone-950">
                <PackagePlus className="size-5 text-stone-500" />
                套餐订单
              </h2>
              <p className="text-sm text-stone-500">创建订单后会自动生成支付入口；支付回调确认后自动履约发放额度。</p>
            </div>
            <Button variant="outline" className="h-9 rounded-xl" onClick={() => void loadAccountData()}>
              <RotateCcw className="size-4" />
              刷新
            </Button>
          </div>

          <div className="grid gap-3 md:grid-cols-3">
            {packages.length ? packages.map((item) => (
              <div key={item.id} className="rounded-2xl border border-stone-200 bg-white p-4 text-sm">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="font-semibold text-stone-900">{item.name}</div>
                    <div className="mt-1 text-xs text-stone-500">{item.quota} 额度 · {item.valid_days ? `${item.valid_days} 天有效` : "长期有效"}</div>
                  </div>
                  <Badge variant="outline">{formatMoney(item.price_cents, item.currency || "CNY")}</Badge>
                </div>
                {item.description ? <div className="mt-2 text-xs text-stone-500">{item.description}</div> : null}
                <Button
                  className="mt-4 h-9 w-full rounded-xl"
                  onClick={() => void handleCreateOrder(item)}
                  disabled={!me?.user_id || creatingOrderPackageId === item.id}
                >
                  {creatingOrderPackageId === item.id ? <LoaderCircle className="size-4 animate-spin" /> : null}
                  创建并支付
                </Button>
              </div>
            )) : (
              <div className="rounded-xl bg-stone-50 p-6 text-center text-sm text-stone-500 md:col-span-3">
                暂无可购买套餐
              </div>
            )}
          </div>

          <div className="overflow-hidden rounded-xl border border-stone-200 bg-white">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>订单</TableHead>
                  <TableHead>金额</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>支付</TableHead>
                  <TableHead>更新时间</TableHead>
                  <TableHead>收据</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {orders.length ? orders.map((order) => (
                  <TableRow key={order.id}>
                    <TableCell>
                      <div className="font-medium text-stone-800">{order.package_name || order.package_id}</div>
                      <div className="text-xs text-stone-400">{order.id} · {order.quota_total} 额度</div>
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-stone-500">{formatMoney(order.amount_cents, order.currency)}</TableCell>
                    <TableCell>
                      <Badge variant={order.status === "fulfilled" ? "success" : order.status === "cancelled" || order.status === "refunded" ? "secondary" : "warning"}>
                        {orderStatusLabel(order.status)}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {order.status === "created" || order.status === "pending_payment" ? (
                        <div className="space-y-1">
                          <Button
                            variant="outline"
                            className="h-8 rounded-lg px-2 text-xs"
                            onClick={() => void handleCreateCheckout(order)}
                            disabled={checkoutOrderId === order.id}
                          >
                            {checkoutOrderId === order.id ? <LoaderCircle className="size-3 animate-spin" /> : getOrderCheckout(order)?.payment_url ? <ExternalLink className="size-3" /> : <CreditCard className="size-3" />}
                            {getOrderCheckout(order)?.payment_url ? "继续支付" : "生成支付"}
                          </Button>
                          {getOrderCheckout(order)?.instructions ? (
                            <div className="max-w-48 truncate text-xs text-stone-400" title={getOrderCheckout(order)?.instructions || ""}>
                              {getOrderCheckout(order)?.instructions}
                            </div>
                          ) : null}
                        </div>
                      ) : (
                        <span className="text-xs text-stone-400">{order.provider || "—"}</span>
                      )}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-stone-500">{formatDateTime(order.updated_at)}</TableCell>
                    <TableCell>
                      {order.status === "paid" || order.status === "fulfilled" || order.status === "refunded" ? (
                        <Button
                          variant="outline"
                          className="h-8 rounded-lg px-2 text-xs"
                          onClick={() => void handleDownloadReceipt(order)}
                          disabled={downloadingReceiptOrderId === order.id}
                        >
                          {downloadingReceiptOrderId === order.id ? <LoaderCircle className="size-3 animate-spin" /> : <Download className="size-3" />}
                          下载
                        </Button>
                      ) : (
                        <span className="text-xs text-stone-400">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                )) : (
                  <TableRow>
                    <TableCell colSpan={6} className="py-8 text-center text-sm text-stone-500">
                      {me?.user_id ? "暂无订单" : "邮箱用户登录后可查看订单"}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-4 p-6">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold tracking-tight text-stone-950">我的额度流水</h2>
              <p className="text-sm text-stone-500">用于核对兑换、订单发放、消费和退款记录。</p>
            </div>
            <Button variant="outline" className="h-9 rounded-xl" onClick={() => void refreshLedger()} disabled={!me?.user_id || isLedgerLoading}>
              {isLedgerLoading ? <LoaderCircle className="size-4 animate-spin" /> : <RotateCcw className="size-4" />}
              刷新
            </Button>
          </div>

          <div className="overflow-hidden rounded-xl border border-stone-200 bg-white">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>类型</TableHead>
                  <TableHead>变动</TableHead>
                  <TableHead>余额</TableHead>
                  <TableHead>原因</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {quotaLedger.length ? quotaLedger.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell className="whitespace-nowrap text-xs text-stone-500">{formatDateTime(item.created_at)}</TableCell>
                    <TableCell>
                      <Badge variant={item.type === "consume" ? "warning" : "outline"}>{ledgerTypeLabel(item.type)}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={item.amount > 0 ? "success" : item.amount < 0 ? "danger" : "secondary"}>{amountLabel(item.amount)}</Badge>
                    </TableCell>
                    <TableCell className="text-xs text-stone-500">{item.balance_before} → {item.balance_after}</TableCell>
                    <TableCell className="max-w-64 truncate text-xs text-stone-500">{item.reason || item.ref_type || "—"}</TableCell>
                  </TableRow>
                )) : (
                  <TableRow>
                    <TableCell colSpan={5} className="py-8 text-center text-sm text-stone-500">
                      {me?.user_id ? "暂无额度流水" : "密钥登录暂无用户额度流水"}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
