"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Copy,
  CreditCard,
  Download,
  ExternalLink,
  Gift,
  LoaderCircle,
  PackagePlus,
  RotateCcw,
  Search,
  Users,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  adjustRegisteredUserQuota,
  createAdminOrderCheckout,
  createCDKs,
  createPackage,
  createRegisteredUser,
  fetchAdminDeadLetterImageJobs,
  fetchAdminImageAssets,
  fetchAdminImageJobs,
  fetchAdminOrders,
  fetchAdminPayments,
  fetchAdminQuotaLedger,
  fetchCDKs,
  fetchPackages,
  fetchRedemptions,
  fetchRegisteredUsers,
  fulfillAdminOrder,
  markAdminOrderPaid,
  refundAdminOrder,
  resetRegisteredUserPassword,
  recoverStaleAdminImageJobs,
  retryAdminImageJob,
  runNextAdminImageJob,
  updateCDK,
  updatePackage,
  updateRegisteredUser,
  type CDKItem,
  type CheckoutSession,
  type ImageAsset,
  type ImageJob,
  type OrderItem,
  type PackageItem,
  type PaymentItem,
  type QuotaLedgerItem,
  type RedemptionRecord,
  type RegisteredUser,
} from "@/lib/api";

const QUOTA_LEDGER_LIMIT = 200;

function formatDateTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function csvCell(value: unknown) {
  const text = String(value ?? "");
  if (/[",\n\r]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

function downloadCsv(fileName: string, rows: unknown[][]) {
  const content = rows.map((row) => row.map(csvCell).join(",")).join("\n");
  const blob = new Blob([`\ufeff${content}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function parseNumber(value: string, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
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

function amountLabel(amount: number) {
  return amount > 0 ? `+${amount}` : String(amount);
}

function formatBytes(value?: number | null) {
  const size = Number(value || 0);
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(2)} MB`;
  if (size >= 1024) return `${Math.ceil(size / 1024)} KB`;
  return `${size} B`;
}

function jobStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    succeeded: "已成功",
    failed: "已失败",
    cancelled: "已取消",
  };
  return labels[status] || status;
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

function formatMoney(cents?: number | null, currency = "CNY") {
  const amount = Number(cents || 0) / 100;
  return `${currency} ${amount.toFixed(2)}`;
}

function getOrderCheckout(order: OrderItem): CheckoutSession | null {
  const checkout = order.metadata?.checkout;
  if (!checkout || typeof checkout !== "object" || Array.isArray(checkout)) return null;
  return checkout as CheckoutSession;
}

export function CommercialManagementCard() {
  const [users, setUsers] = useState<RegisteredUser[]>([]);
  const [packages, setPackages] = useState<PackageItem[]>([]);
  const [cdks, setCdks] = useState<CDKItem[]>([]);
  const [redemptions, setRedemptions] = useState<RedemptionRecord[]>([]);
  const [quotaLedger, setQuotaLedger] = useState<QuotaLedgerItem[]>([]);
  const [imageJobs, setImageJobs] = useState<ImageJob[]>([]);
  const [deadLetterJobs, setDeadLetterJobs] = useState<ImageJob[]>([]);
  const [imageAssets, setImageAssets] = useState<ImageAsset[]>([]);
  const [orders, setOrders] = useState<OrderItem[]>([]);
  const [payments, setPayments] = useState<PaymentItem[]>([]);
  const [codes, setCodes] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isLedgerLoading, setIsLedgerLoading] = useState(false);
  const [isJobRunning, setIsJobRunning] = useState(false);
  const [isJobRecovering, setIsJobRecovering] = useState(false);
  const [packageName, setPackageName] = useState("");
  const [packageDescription, setPackageDescription] = useState("");
  const [packageQuota, setPackageQuota] = useState("100");
  const [packagePriceCents, setPackagePriceCents] = useState("0");
  const [packageCurrency, setPackageCurrency] = useState("CNY");
  const [packageValidDays, setPackageValidDays] = useState("");
  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserQuota, setNewUserQuota] = useState("0");
  const [revealedUserToken, setRevealedUserToken] = useState("");
  const [cdkName, setCdkName] = useState("");
  const [cdkType, setCdkType] = useState<"quota" | "package">("quota");
  const [cdkQuota, setCdkQuota] = useState("100");
  const [cdkCount, setCdkCount] = useState("1");
  const [cdkMaxRedemptions, setCdkMaxRedemptions] = useState("1");
  const [cdkPerUserLimit, setCdkPerUserLimit] = useState("1");
  const [cdkExpiresAt, setCdkExpiresAt] = useState("");
  const [selectedPackage, setSelectedPackage] = useState("");
  const [cdkSearch, setCdkSearch] = useState("");
  const [cdkStatusFilter, setCdkStatusFilter] = useState<"all" | "enabled" | "disabled">("all");
  const [redemptionSearch, setRedemptionSearch] = useState("");
  const [quotaLedgerUserId, setQuotaLedgerUserId] = useState("");
  const [jobStatusFilter, setJobStatusFilter] = useState("");
  const [orderStatusFilter, setOrderStatusFilter] = useState("");
  const [isOrderActionRunning, setIsOrderActionRunning] = useState(false);
  const [quotaUser, setQuotaUser] = useState<RegisteredUser | null>(null);
  const [quotaDelta, setQuotaDelta] = useState("10");
  const [quotaReason, setQuotaReason] = useState("admin");
  const [passwordUser, setPasswordUser] = useState<RegisteredUser | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    description: string;
    action: () => Promise<void>;
  } | null>(null);

  const userById = useMemo(() => new Map(users.map((user) => [user.id, user])), [users]);
  const totalUserQuota = useMemo(() => users.reduce((sum, user) => sum + Number(user.quota_balance || 0), 0), [users]);
  const enabledCdkCount = useMemo(() => cdks.filter((item) => item.enabled).length, [cdks]);
  const pendingOrderCount = useMemo(() => orders.filter((item) => item.status === "pending_payment" || item.status === "created").length, [orders]);
  const fulfilledOrderCount = useMemo(() => orders.filter((item) => item.status === "fulfilled").length, [orders]);
  const paidRevenueCents = useMemo(
    () => payments.filter((item) => item.status === "succeeded").reduce((sum, item) => sum + Number(item.amount_cents || 0), 0),
    [payments],
  );
  const activeImageJobCount = useMemo(
    () => imageJobs.filter((job) => job.status === "queued" || job.status === "running").length,
    [imageJobs],
  );
  const deadLetterJobCount = deadLetterJobs.length;

  const filteredCdks = useMemo(() => cdks.filter((item) => {
    const query = cdkSearch.trim().toLowerCase();
    const matchesQuery = !query || [item.name, item.code_prefix, item.package_name, item.type]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query));
    const matchesStatus = cdkStatusFilter === "all" || (cdkStatusFilter === "enabled" ? item.enabled : !item.enabled);
    return matchesQuery && matchesStatus;
  }), [cdkSearch, cdkStatusFilter, cdks]);

  const filteredRedemptions = useMemo(() => redemptions.filter((item) => {
    const query = redemptionSearch.trim().toLowerCase();
    if (!query) return true;
    return [item.email, item.user_id, item.code_prefix, item.package_name, item.type]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query));
  }), [redemptionSearch, redemptions]);

  const filteredImageJobs = useMemo(
    () => imageJobs.filter((job) => !jobStatusFilter || job.status === jobStatusFilter),
    [imageJobs, jobStatusFilter],
  );

  const filteredOrders = useMemo(
    () => orders.filter((order) => !orderStatusFilter || order.status === orderStatusFilter),
    [orders, orderStatusFilter],
  );

  const refreshQuotaLedger = async (userId = quotaLedgerUserId) => {
    setIsLedgerLoading(true);
    try {
      const data = await fetchAdminQuotaLedger({
        user_id: userId || undefined,
        limit: QUOTA_LEDGER_LIMIT,
      });
      setQuotaLedger(data.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载额度流水失败");
    } finally {
      setIsLedgerLoading(false);
    }
  };

  const refreshImageJobs = async () => {
    try {
      const [data, deadLetterData] = await Promise.all([
        fetchAdminImageJobs({ limit: 50 }),
        fetchAdminDeadLetterImageJobs({ limit: 20 }),
      ]);
      setImageJobs(data.items);
      setDeadLetterJobs(deadLetterData.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载异步任务失败");
    }
  };

  const refreshImageAssets = async () => {
    try {
      const data = await fetchAdminImageAssets({ limit: 12 });
      setImageAssets(data.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载图片资产失败");
    }
  };

  const refreshOrders = async () => {
    try {
      const [orderData, paymentData] = await Promise.all([
        fetchAdminOrders({ limit: 80 }),
        fetchAdminPayments({ limit: 80 }),
      ]);
      setOrders(orderData.items);
      setPayments(paymentData.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载订单/支付数据失败");
    }
  };

  const handleRunNextImageJob = async () => {
    setIsJobRunning(true);
    try {
      const data = await runNextAdminImageJob();
      if (data.job) {
        toast.success(`任务 ${jobStatusLabel(data.job.status)}：${data.job.id}`);
      } else {
        toast.info("当前没有排队中的任务");
      }
      await refreshImageJobs();
      await refreshQuotaLedger(quotaLedgerUserId);
      const userData = await fetchRegisteredUsers();
      setUsers(userData.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "执行异步任务失败");
    } finally {
      setIsJobRunning(false);
    }
  };

  const handleRecoverStaleJobs = async () => {
    setIsJobRecovering(true);
    try {
      const data = await recoverStaleAdminImageJobs();
      toast.success(data.recovered ? `已恢复 ${data.recovered} 个卡死任务` : "暂无需要恢复的卡死任务");
      await refreshImageJobs();
      await refreshQuotaLedger(quotaLedgerUserId);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "恢复卡死任务失败");
    } finally {
      setIsJobRecovering(false);
    }
  };

  const handleRetryDeadLetterJob = async (job: ImageJob) => {
    setIsJobRecovering(true);
    try {
      await retryAdminImageJob(job.id, "admin-retry");
      toast.success("失败任务已重新入队");
      await refreshImageJobs();
      await refreshQuotaLedger(quotaLedgerUserId);
      const userData = await fetchRegisteredUsers();
      setUsers(userData.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重新入队失败");
    } finally {
      setIsJobRecovering(false);
    }
  };

  const handleMarkOrderPaid = async (order: OrderItem) => {
    setIsOrderActionRunning(true);
    try {
      await markAdminOrderPaid(order.id, {
        provider: "manual",
        provider_payment_id: `manual-${order.id}`,
        amount_cents: order.amount_cents,
        currency: order.currency,
        auto_fulfill: true,
      });
      toast.success("订单已确认支付并履约");
      await Promise.all([
        refreshOrders(),
        refreshQuotaLedger(quotaLedgerUserId),
        fetchRegisteredUsers().then((data) => setUsers(data.items)),
      ]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "确认支付失败");
    } finally {
      setIsOrderActionRunning(false);
    }
  };

  const handleCreateOrderCheckout = async (order: OrderItem) => {
    setIsOrderActionRunning(true);
    try {
      const result = await createAdminOrderCheckout(order.id, { metadata: { source: "admin-ui" } });
      const checkoutText = result.checkout.payment_url || result.checkout.instructions || "";
      if (checkoutText) {
        await navigator.clipboard.writeText(checkoutText);
        toast.success(result.checkout.payment_url ? "支付链接已生成并复制" : "支付说明已生成并复制");
      } else {
        toast.success("支付入口已生成");
      }
      await refreshOrders();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "生成支付入口失败");
    } finally {
      setIsOrderActionRunning(false);
    }
  };

  const handleFulfillOrder = async (order: OrderItem) => {
    setIsOrderActionRunning(true);
    try {
      await fulfillAdminOrder(order.id);
      toast.success("订单已履约");
      await Promise.all([
        refreshOrders(),
        refreshQuotaLedger(quotaLedgerUserId),
        fetchRegisteredUsers().then((data) => setUsers(data.items)),
      ]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "订单履约失败");
    } finally {
      setIsOrderActionRunning(false);
    }
  };

  const handleRefundOrder = async (order: OrderItem) => {
    setIsOrderActionRunning(true);
    try {
      const data = await refundAdminOrder(order.id, {
        reason: "admin-refund",
        metadata: { source: "admin-ui" },
      });
      toast.success(data.idempotent ? "订单已是退款状态" : `订单已退款，扣回 ${data.quota_deducted ?? 0} 额度`);
      await Promise.all([
        refreshOrders(),
        refreshQuotaLedger(quotaLedgerUserId),
        fetchRegisteredUsers().then((result) => setUsers(result.items)),
      ]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "订单退款失败");
    } finally {
      setIsOrderActionRunning(false);
    }
  };

  const openRefundOrderConfirm = (order: OrderItem) => {
    setConfirmAction({
      title: "确认退款",
      description: `将把订单 ${order.id} 标记为已退款，并从用户当前余额扣回 ${order.quota_granted || order.quota_total || 0} 额度。余额不足时后端会拒绝退款，避免负余额或部分退款。`,
      action: async () => {
        await handleRefundOrder(order);
      },
    });
  };

  const load = async () => {
    setIsLoading(true);
    setIsLedgerLoading(true);
    try {
      const [userData, packageData, cdkData, redemptionData, quotaLedgerData, imageJobData, deadLetterJobData, imageAssetData, orderData, paymentData] = await Promise.all([
        fetchRegisteredUsers(),
        fetchPackages(),
        fetchCDKs(),
        fetchRedemptions(),
        fetchAdminQuotaLedger({ user_id: quotaLedgerUserId || undefined, limit: QUOTA_LEDGER_LIMIT }),
        fetchAdminImageJobs({ limit: 50 }),
        fetchAdminDeadLetterImageJobs({ limit: 20 }),
        fetchAdminImageAssets({ limit: 12 }),
        fetchAdminOrders({ limit: 80 }),
        fetchAdminPayments({ limit: 80 }),
      ]);
      setUsers(userData.items);
      setPackages(packageData.items);
      setCdks(cdkData.items);
      setRedemptions(redemptionData.items);
      setQuotaLedger(quotaLedgerData.items);
      setImageJobs(imageJobData.items);
      setDeadLetterJobs(deadLetterJobData.items);
      setImageAssets(imageAssetData.items);
      setOrders(orderData.items);
      setPayments(paymentData.items);
      setSelectedPackage((current) => current || packageData.items[0]?.id || "");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载商业管理数据失败");
    } finally {
      setIsLoading(false);
      setIsLedgerLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // 初始加载只执行一次；后续刷新由按钮或筛选控件显式触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const userLabel = (userId: string) => userById.get(userId)?.email || userId || "—";

  const handleCreatePackage = async () => {
    const quota = parseNumber(packageQuota);
    if (quota <= 0) {
      toast.error("套餐额度必须大于 0");
      return;
    }

    setIsSaving(true);
    try {
      const data = await createPackage({
        name: packageName.trim() || "未命名套餐",
        description: packageDescription.trim(),
        quota,
        price_cents: Math.max(0, parseNumber(packagePriceCents)),
        currency: packageCurrency.trim().toUpperCase() || "CNY",
        valid_days: packageValidDays ? parseNumber(packageValidDays) : null,
      });
      setPackages(data.items);
      setSelectedPackage((current) => current || data.item.id);
      setPackageName("");
      setPackageDescription("");
      setPackagePriceCents("0");
      setPackageCurrency("CNY");
      setPackageValidDays("");
      toast.success("套餐已创建");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "创建套餐失败");
    } finally {
      setIsSaving(false);
    }
  };

  const handleCreateUser = async () => {
    if (!newUserEmail.trim() || !newUserPassword) {
      toast.error("请输入邮箱和密码");
      return;
    }
    setIsSaving(true);
    try {
      const data = await createRegisteredUser({
        email: newUserEmail.trim(),
        password: newUserPassword,
        quota_balance: parseNumber(newUserQuota),
      });
      setUsers(data.items);
      if (parseNumber(newUserQuota) > 0) {
        await refreshQuotaLedger(quotaLedgerUserId);
      }
      setRevealedUserToken(data.token);
      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserQuota("0");
      toast.success("用户已创建");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "创建用户失败");
    } finally {
      setIsSaving(false);
    }
  };

  const handleCreateCDK = async () => {
    const selectedPackageId = selectedPackage || packages[0]?.id || "";
    const count = Math.max(1, parseNumber(cdkCount, 1));
    const maxRedemptions = Math.max(1, parseNumber(cdkMaxRedemptions, 1));
    const perUserLimit = Math.max(1, parseNumber(cdkPerUserLimit, 1));
    const quota = parseNumber(cdkQuota);

    if (cdkType === "quota" && quota <= 0) {
      toast.error("额度型 CDK 的额度必须大于 0");
      return;
    }
    if (cdkType === "package" && !selectedPackageId) {
      toast.error("请先创建套餐");
      return;
    }

    setIsSaving(true);
    try {
      const data = await createCDKs({
        name: cdkName.trim() || "未命名 CDK",
        type: cdkType,
        count,
        quota,
        package_id: cdkType === "package" ? selectedPackageId : null,
        max_redemptions: maxRedemptions,
        per_user_limit: perUserLimit,
        expires_at: cdkExpiresAt ? new Date(cdkExpiresAt).toISOString() : null,
      });
      setCdks(data.items);
      setCodes(data.codes);
      setCdkName("");
      setCdkExpiresAt("");
      const redemptionData = await fetchRedemptions();
      setRedemptions(redemptionData.items);
      toast.success("CDK 已生成");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "生成 CDK 失败");
    } finally {
      setIsSaving(false);
    }
  };

  const handleToggleUser = (user: RegisteredUser) => {
    setConfirmAction({
      title: user.enabled ? "禁用用户" : "启用用户",
      description: `${user.enabled ? "禁用后该用户将无法继续登录或消费额度。" : "启用后该用户可以继续登录和消费额度。"}\n${user.email}`,
      action: async () => {
        const data = await updateRegisteredUser(user.id, { enabled: !user.enabled });
        setUsers(data.items);
        toast.success(user.enabled ? "用户已禁用" : "用户已启用");
      },
    });
  };

  const openQuotaDialog = (user: RegisteredUser) => {
    setQuotaUser(user);
    setQuotaDelta("10");
    setQuotaReason("admin");
  };

  const submitQuotaDialog = async () => {
    if (!quotaUser) return;
    const delta = parseNumber(quotaDelta);
    if (!Number.isFinite(delta) || delta === 0) {
      toast.error("请输入非 0 的额度变化");
      return;
    }
    setIsSaving(true);
    try {
      const data = await adjustRegisteredUserQuota(quotaUser.id, delta, quotaReason.trim() || "admin");
      setUsers(data.items);
      await refreshQuotaLedger(quotaLedgerUserId);
      setQuotaUser(null);
      toast.success("额度已调整");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "调整额度失败");
    } finally {
      setIsSaving(false);
    }
  };

  const openPasswordDialog = (user: RegisteredUser) => {
    setPasswordUser(user);
    setNewPassword("");
  };

  const submitPasswordDialog = async () => {
    if (!passwordUser) return;
    if (newPassword.length < 8) {
      toast.error("新密码至少 8 位");
      return;
    }
    setIsSaving(true);
    try {
      await resetRegisteredUserPassword(passwordUser.id, newPassword);
      setPasswordUser(null);
      setNewPassword("");
      toast.success("密码已重置");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重置密码失败");
    } finally {
      setIsSaving(false);
    }
  };

  const handleTogglePackage = (item: PackageItem) => {
    setConfirmAction({
      title: item.enabled ? "禁用套餐" : "启用套餐",
      description: `${item.enabled ? "禁用后套餐型 CDK 将不能继续发放该套餐。" : "启用后套餐可继续用于 CDK。"}\n${item.name}`,
      action: async () => {
        const data = await updatePackage(item.id, { enabled: !item.enabled });
        setPackages(data.items);
        toast.success(item.enabled ? "套餐已禁用" : "套餐已启用");
      },
    });
  };

  const handleToggleCDK = (item: CDKItem) => {
    setConfirmAction({
      title: item.enabled ? "禁用 CDK" : "启用 CDK",
      description: `${item.enabled ? "禁用后该 CDK 无法继续兑换。" : "启用后该 CDK 可以继续兑换。"}\n${item.name} · ${item.code_prefix}***`,
      action: async () => {
        const data = await updateCDK(item.id, { enabled: !item.enabled });
        setCdks(data.items);
        toast.success(item.enabled ? "CDK 已禁用" : "CDK 已启用");
      },
    });
  };

  const copyCodes = async () => {
    try {
      await navigator.clipboard.writeText(codes.join("\n"));
      toast.success("CDK 已复制");
    } catch {
      toast.error("复制失败");
    }
  };

  const exportVisibleCdks = () => {
    downloadCsv("cdk-list.csv", [
      ["prefix", "name", "type", "grant", "redeemed", "max", "per_user", "status", "expires_at"],
      ...filteredCdks.map((item) => [
        item.code_prefix,
        item.name,
        item.type,
        item.package_name || item.quota,
        item.redeemed_count,
        item.max_redemptions,
        item.per_user_limit,
        item.enabled ? "enabled" : "disabled",
        item.expires_at || "",
      ]),
    ]);
    toast.success("当前 CDK 列表已导出");
  };

  const exportQuotaLedger = () => {
    downloadCsv("quota-ledger.csv", [
      [
        "created_at",
        "email",
        "user_id",
        "type",
        "amount",
        "balance_before",
        "balance_after",
        "reason",
        "ref_type",
        "ref_id",
        "actor_type",
        "actor_id",
        "metadata",
      ],
      ...quotaLedger.map((item) => [
        item.created_at,
        userLabel(item.user_id),
        item.user_id,
        item.type,
        item.amount,
        item.balance_before,
        item.balance_after,
        item.reason || "",
        item.ref_type || "",
        item.ref_id || "",
        item.actor_type || "",
        item.actor_id || "",
        JSON.stringify(item.metadata || {}),
      ]),
    ]);
    toast.success("额度流水已导出");
  };

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-6 p-6">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex size-10 items-center justify-center rounded-xl bg-stone-100">
              <Gift className="size-5 text-stone-600" />
            </div>
            <div>
              <h2 className="text-lg font-semibold tracking-tight">商业用户 / CDK 管理</h2>
              <p className="text-sm text-stone-500">
                管理邮箱注册用户、套餐、CDK、兑换记录和可审计额度流水。
              </p>
            </div>
          </div>
          <Button variant="outline" className="h-9 rounded-xl" onClick={() => void load()}>
            <RotateCcw className="size-4" />
            刷新
          </Button>
        </div>

        <div className="grid gap-3 md:grid-cols-9">
          <div className="rounded-2xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">注册用户</div>
            <div className="mt-1 text-xl font-semibold text-stone-950">{users.length}</div>
          </div>
          <div className="rounded-2xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">用户总余额</div>
            <div className="mt-1 text-xl font-semibold text-stone-950">{totalUserQuota}</div>
          </div>
          <div className="rounded-2xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">待支付订单</div>
            <div className="mt-1 text-xl font-semibold text-stone-950">{pendingOrderCount}</div>
          </div>
          <div className="rounded-2xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">已履约订单</div>
            <div className="mt-1 text-xl font-semibold text-stone-950">{fulfilledOrderCount}</div>
            <div className="mt-1 truncate text-[11px] text-stone-400">{formatMoney(paidRevenueCents)}</div>
          </div>
          <div className="rounded-2xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">启用 CDK</div>
            <div className="mt-1 text-xl font-semibold text-stone-950">{enabledCdkCount}</div>
          </div>
          <div className="rounded-2xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">兑换记录</div>
            <div className="mt-1 text-xl font-semibold text-stone-950">{redemptions.length}</div>
          </div>
          <div className="rounded-2xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">活跃任务</div>
            <div className="mt-1 text-xl font-semibold text-stone-950">{activeImageJobCount}</div>
          </div>
          <div className="rounded-2xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">死信任务</div>
            <div className="mt-1 text-xl font-semibold text-stone-950">{deadLetterJobCount}</div>
          </div>
          <div className="rounded-2xl bg-stone-50 p-4">
            <div className="text-xs text-stone-500">图片资产</div>
            <div className="mt-1 text-xl font-semibold text-stone-950">{imageAssets.length}</div>
          </div>
        </div>

        {isLoading ? (
          <div className="flex justify-center py-10">
            <LoaderCircle className="size-5 animate-spin text-stone-400" />
          </div>
        ) : (
          <div className="grid gap-6 xl:grid-cols-2">
            <section className="space-y-3">
              <h3 className="flex items-center gap-2 text-sm font-semibold text-stone-800">
                <Users className="size-4" />
                注册用户
              </h3>
              <div className="grid gap-2 sm:grid-cols-[1fr_1fr_90px_auto]">
                <Input
                  value={newUserEmail}
                  onChange={(event) => setNewUserEmail(event.target.value)}
                  placeholder="邮箱"
                  className="h-10 rounded-xl"
                />
                <Input
                  value={newUserPassword}
                  onChange={(event) => setNewUserPassword(event.target.value)}
                  placeholder="初始密码"
                  type="password"
                  className="h-10 rounded-xl"
                />
                <Input
                  value={newUserQuota}
                  onChange={(event) => setNewUserQuota(event.target.value)}
                  placeholder="额度"
                  type="number"
                  className="h-10 rounded-xl"
                />
                <Button className="h-10 rounded-xl" onClick={() => void handleCreateUser()} disabled={isSaving}>
                  创建
                </Button>
              </div>
              {revealedUserToken ? (
                <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-900">
                  <div className="font-medium">新用户登录 token 仅展示一次：</div>
                  <code className="mt-2 block break-all">{revealedUserToken}</code>
                </div>
              ) : null}
              <div className="space-y-2">
                {users.length ? users.map((user) => (
                  <div key={user.id} className="rounded-xl border border-stone-200 bg-white p-3 text-sm">
                    <div className="flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate font-medium text-stone-900">{user.email}</div>
                        <div className="text-xs text-stone-500">
                          额度 {user.quota_balance} · 套餐 {user.package_name || "无"}
                        </div>
                      </div>
                      <Badge variant={user.enabled ? "success" : "secondary"}>
                        {user.enabled ? "启用" : "禁用"}
                      </Badge>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Button size="sm" variant="outline" className="h-8 rounded-lg" onClick={() => openQuotaDialog(user)}>
                        调整额度
                      </Button>
                      <Button size="sm" variant="outline" className="h-8 rounded-lg" onClick={() => openPasswordDialog(user)}>
                        重置密码
                      </Button>
                      <Button size="sm" variant="outline" className="h-8 rounded-lg" onClick={() => handleToggleUser(user)}>
                        {user.enabled ? "禁用" : "启用"}
                      </Button>
                    </div>
                  </div>
                )) : (
                  <div className="rounded-xl bg-stone-50 p-6 text-center text-sm text-stone-500">
                    暂无注册用户
                  </div>
                )}
              </div>
            </section>

            <section className="space-y-3">
              <h3 className="flex items-center gap-2 text-sm font-semibold text-stone-800">
                <PackagePlus className="size-4" />
                套餐
              </h3>
              <div className="grid gap-2 sm:grid-cols-[1fr_110px_110px_90px_100px_auto]">
                <Input
                  value={packageName}
                  onChange={(event) => setPackageName(event.target.value)}
                  placeholder="套餐名称"
                  className="h-10 rounded-xl"
                />
                <Input
                  value={packageQuota}
                  onChange={(event) => setPackageQuota(event.target.value)}
                  type="number"
                  placeholder="额度"
                  className="h-10 rounded-xl"
                />
                <Input
                  value={packagePriceCents}
                  onChange={(event) => setPackagePriceCents(event.target.value)}
                  type="number"
                  min="0"
                  placeholder="价格(分)"
                  className="h-10 rounded-xl"
                />
                <Input
                  value={packageCurrency}
                  onChange={(event) => setPackageCurrency(event.target.value)}
                  placeholder="币种"
                  className="h-10 rounded-xl"
                />
                <Input
                  value={packageValidDays}
                  onChange={(event) => setPackageValidDays(event.target.value)}
                  type="number"
                  placeholder="有效天"
                  className="h-10 rounded-xl"
                />
                <Button className="h-10 rounded-xl" onClick={() => void handleCreatePackage()} disabled={isSaving}>
                  创建
                </Button>
              </div>
              <Input
                value={packageDescription}
                onChange={(event) => setPackageDescription(event.target.value)}
                placeholder="套餐说明（可选）"
                className="h-10 rounded-xl"
              />
              <div className="space-y-2">
                {packages.length ? packages.map((item) => (
                  <div key={item.id} className="rounded-xl border border-stone-200 bg-white p-3 text-sm">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="font-medium text-stone-900">{item.name} · {item.quota} 额度</div>
                        <div className="text-xs text-stone-500">
                          {formatMoney(item.price_cents, item.currency || "CNY")} · {item.description || "无说明"} · {item.valid_days ? `${item.valid_days} 天有效` : "长期有效"}
                        </div>
                      </div>
                      <Button size="sm" variant="outline" className="h-8 rounded-lg" onClick={() => handleTogglePackage(item)}>
                        {item.enabled ? "禁用" : "启用"}
                      </Button>
                    </div>
                  </div>
                )) : (
                  <div className="rounded-xl bg-stone-50 p-6 text-center text-sm text-stone-500">
                    暂无套餐，先创建套餐后才能生成套餐型 CDK。
                  </div>
                )}
              </div>
            </section>

            <section className="space-y-3 xl:col-span-2">
              <h3 className="text-sm font-semibold text-stone-800">生成 CDK</h3>
              <div className="grid gap-2 lg:grid-cols-[1fr_110px_90px_150px_120px_120px_170px_auto]">
                <Input
                  value={cdkName}
                  onChange={(event) => setCdkName(event.target.value)}
                  placeholder="CDK 批次名"
                  className="h-10 rounded-xl"
                />
                <select
                  className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm"
                  value={cdkType}
                  onChange={(event) => setCdkType(event.target.value as "quota" | "package")}
                >
                  <option value="quota">额度</option>
                  <option value="package">套餐</option>
                </select>
                <Input
                  value={cdkCount}
                  onChange={(event) => setCdkCount(event.target.value)}
                  type="number"
                  min="1"
                  placeholder="数量"
                  className="h-10 rounded-xl"
                />
                {cdkType === "package" ? (
                  <select
                    className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm"
                    value={selectedPackage}
                    onChange={(event) => setSelectedPackage(event.target.value)}
                  >
                    {packages.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                  </select>
                ) : (
                  <Input
                    value={cdkQuota}
                    onChange={(event) => setCdkQuota(event.target.value)}
                    type="number"
                    placeholder="额度"
                    className="h-10 rounded-xl"
                  />
                )}
                <Input
                  value={cdkMaxRedemptions}
                  onChange={(event) => setCdkMaxRedemptions(event.target.value)}
                  type="number"
                  min="1"
                  placeholder="总次数"
                  className="h-10 rounded-xl"
                />
                <Input
                  value={cdkPerUserLimit}
                  onChange={(event) => setCdkPerUserLimit(event.target.value)}
                  type="number"
                  min="1"
                  placeholder="每人"
                  className="h-10 rounded-xl"
                />
                <Input
                  value={cdkExpiresAt}
                  onChange={(event) => setCdkExpiresAt(event.target.value)}
                  type="datetime-local"
                  className="h-10 rounded-xl"
                />
                <Button
                  className="h-10 rounded-xl"
                  onClick={() => void handleCreateCDK()}
                  disabled={isSaving || (cdkType === "package" && packages.length === 0)}
                >
                  生成
                </Button>
              </div>
              {codes.length ? (
                <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
                  <div className="mb-2 flex items-center justify-between">
                    <strong>新 CDK 仅展示一次</strong>
                    <Button size="sm" variant="outline" onClick={() => void copyCodes()}>
                      <Copy className="size-4" />
                      复制
                    </Button>
                  </div>
                  <pre className="whitespace-pre-wrap break-all text-xs">{codes.join("\n")}</pre>
                </div>
              ) : null}

              <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
                <div className="relative md:w-80">
                  <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-stone-400" />
                  <Input
                    value={cdkSearch}
                    onChange={(event) => setCdkSearch(event.target.value)}
                    placeholder="搜索 CDK 批次、前缀或套餐"
                    className="h-10 rounded-xl pl-9"
                  />
                </div>
                <div className="flex gap-2">
                  <select
                    className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm"
                    value={cdkStatusFilter}
                    onChange={(event) => setCdkStatusFilter(event.target.value as "all" | "enabled" | "disabled")}
                  >
                    <option value="all">全部状态</option>
                    <option value="enabled">启用</option>
                    <option value="disabled">禁用</option>
                  </select>
                  <Button variant="outline" className="h-10 rounded-xl" onClick={exportVisibleCdks} disabled={!filteredCdks.length}>
                    <Download className="size-4" />
                    导出当前
                  </Button>
                </div>
              </div>
              <div className="grid gap-2 md:grid-cols-2">
                {filteredCdks.length ? filteredCdks.map((item) => (
                  <div key={item.id} className="rounded-xl border border-stone-200 bg-white p-3 text-sm">
                    <div className="flex justify-between gap-3">
                      <span className="font-medium text-stone-900">{item.name} · {item.code_prefix}***</span>
                      <Badge variant={item.enabled ? "success" : "secondary"}>{item.enabled ? "启用" : "禁用"}</Badge>
                    </div>
                    <div className="mt-1 text-xs text-stone-500">
                      {item.type === "package" ? item.package_name : `${item.quota} 额度`} ·
                      已兑 {item.redeemed_count}/{item.max_redemptions} · 每人 {item.per_user_limit}
                    </div>
                    <div className="mt-1 text-xs text-stone-400">过期时间：{formatDateTime(item.expires_at)}</div>
                    <Button size="sm" variant="outline" className="mt-2 h-8 rounded-lg" onClick={() => handleToggleCDK(item)}>
                      {item.enabled ? "禁用" : "启用"}
                    </Button>
                  </div>
                )) : (
                  <div className="rounded-xl bg-stone-50 p-6 text-center text-sm text-stone-500 md:col-span-2">
                    没有匹配的 CDK
                  </div>
                )}
              </div>
            </section>

            <section className="space-y-3 xl:col-span-2">
              <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
                <h3 className="text-sm font-semibold text-stone-800">兑换记录</h3>
                <div className="relative md:w-80">
                  <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-stone-400" />
                  <Input
                    value={redemptionSearch}
                    onChange={(event) => setRedemptionSearch(event.target.value)}
                    placeholder="搜索邮箱、用户、CDK 或套餐"
                    className="h-10 rounded-xl pl-9"
                  />
                </div>
              </div>
              <div className="space-y-2">
                {filteredRedemptions.length ? filteredRedemptions.map((item) => (
                  <div key={item.id} className="rounded-xl bg-stone-50 p-3 text-sm text-stone-600">
                    {item.email || item.user_id} 兑换 {item.type === "package" ? item.package_name : `${item.quota_granted} 额度`}
                    {" · "}{formatDateTime(item.redeemed_at)} · {item.code_prefix}***
                  </div>
                )) : (
                  <div className="rounded-xl bg-stone-50 p-6 text-center text-sm text-stone-500">
                    暂无兑换记录
                  </div>
                )}
              </div>
            </section>

            <section className="space-y-3 xl:col-span-2">
              <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <h3 className="text-sm font-semibold text-stone-800">订单 / 支付履约</h3>
                  <p className="text-xs text-stone-500">
                    用户创建套餐订单后，管理员可人工或模拟支付确认；支付确认具备幂等保护，成功后自动发放额度并写入账本。
                  </p>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <select
                    className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm"
                    value={orderStatusFilter}
                    onChange={(event) => setOrderStatusFilter(event.target.value)}
                  >
                    <option value="">全部状态</option>
                    <option value="pending_payment">待支付</option>
                    <option value="paid">已支付</option>
                    <option value="fulfilled">已履约</option>
                    <option value="cancelled">已取消</option>
                    <option value="refunded">已退款</option>
                  </select>
                  <Button variant="outline" className="h-10 rounded-xl" onClick={() => void refreshOrders()}>
                    <RotateCcw className="size-4" />
                    刷新订单
                  </Button>
                </div>
              </div>

              <div className="overflow-hidden rounded-xl border border-stone-200 bg-white">
                <div className="max-h-[420px] overflow-auto">
                  <Table>
                    <TableHeader className="sticky top-0 bg-white">
                      <TableRow>
                        <TableHead>订单</TableHead>
                        <TableHead>用户</TableHead>
                        <TableHead>金额</TableHead>
                        <TableHead>状态</TableHead>
                        <TableHead>时间</TableHead>
                        <TableHead>操作</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {filteredOrders.length ? filteredOrders.map((order) => (
                        <TableRow key={order.id}>
                          <TableCell className="max-w-72">
                            <div className="truncate font-medium text-stone-800" title={order.id}>
                              {order.package_name || order.package_id} · {order.quota_total} 额度
                            </div>
                            <div className="text-xs text-stone-400">{order.id}</div>
                          </TableCell>
                          <TableCell className="max-w-52 truncate">
                            {order.email || userLabel(order.user_id)}
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-xs text-stone-500">
                            {formatMoney(order.amount_cents, order.currency)}
                          </TableCell>
                          <TableCell>
                            <Badge variant={order.status === "fulfilled" ? "success" : order.status === "paid" || order.status === "pending_payment" ? "warning" : order.status === "cancelled" || order.status === "refunded" ? "secondary" : "outline"}>
                              {orderStatusLabel(order.status)}
                            </Badge>
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-xs text-stone-500">
                            {formatDateTime(order.updated_at || order.created_at)}
                          </TableCell>
                          <TableCell>
                            <div className="flex flex-wrap gap-2">
                              {order.status === "pending_payment" || order.status === "created" ? (
                                <>
                                  <Button
                                    size="sm"
                                    variant="outline"
                                    className="h-8 rounded-lg"
                                    disabled={isOrderActionRunning}
                                    onClick={() => void handleCreateOrderCheckout(order)}
                                  >
                                    {getOrderCheckout(order)?.payment_url ? <ExternalLink className="size-3" /> : <CreditCard className="size-3" />}
                                    {getOrderCheckout(order)?.payment_url ? "复制支付链接" : "生成支付入口"}
                                  </Button>
                                  <Button
                                    size="sm"
                                    className="h-8 rounded-lg"
                                    disabled={isOrderActionRunning}
                                    onClick={() => void handleMarkOrderPaid(order)}
                                  >
                                    标记已支付
                                  </Button>
                                </>
                              ) : null}
                              {order.status === "paid" ? (
                                <Button
                                  size="sm"
                                  variant="outline"
                                  className="h-8 rounded-lg"
                                  disabled={isOrderActionRunning}
                                  onClick={() => void handleFulfillOrder(order)}
                                >
                                  手动履约
                                </Button>
                              ) : null}
                              {order.status === "fulfilled" ? (
                                <Button
                                  size="sm"
                                  variant="outline"
                                  className="h-8 rounded-lg border-rose-200 text-rose-700 hover:bg-rose-50"
                                  disabled={isOrderActionRunning}
                                  onClick={() => openRefundOrderConfirm(order)}
                                >
                                  退款
                                </Button>
                              ) : null}
                            </div>
                          </TableCell>
                        </TableRow>
                      )) : (
                        <TableRow>
                          <TableCell colSpan={6} className="py-8 text-center text-sm text-stone-500">
                            暂无订单
                          </TableCell>
                        </TableRow>
                      )}
                    </TableBody>
                  </Table>
                </div>
              </div>
            </section>

            <section className="space-y-3 xl:col-span-2">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-stone-800">最近图片资产</h3>
                  <p className="text-xs text-stone-500">
                    生成结果会写入对象存储目录和 image_assets 元数据，供作品库、清理和 CDN 分发使用。
                  </p>
                </div>
                <Button variant="outline" className="h-10 rounded-xl" onClick={() => void refreshImageAssets()}>
                  <RotateCcw className="size-4" />
                  刷新资产
                </Button>
              </div>
              {imageAssets.length ? (
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  {imageAssets.map((asset) => (
                    <div key={asset.id} className="overflow-hidden rounded-xl border border-stone-200 bg-white">
                      <a href={asset.url} target="_blank" rel="noreferrer" className="block aspect-square bg-stone-100">
                        <img src={asset.url} alt={asset.prompt_preview || asset.id} className="h-full w-full object-cover" />
                      </a>
                      <div className="space-y-1 p-3 text-xs text-stone-500">
                        <div className="truncate font-medium text-stone-800" title={asset.prompt_preview || asset.id}>
                          {asset.prompt_preview || asset.id}
                        </div>
                        <div className="truncate">{asset.owner?.email || asset.owner?.user_id || asset.owner?.key_name || "—"}</div>
                        <div className="flex justify-between gap-2">
                          <span>{asset.width || 0} × {asset.height || 0}</span>
                          <span>{formatBytes(asset.size_bytes)}</span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="rounded-xl bg-stone-50 p-6 text-center text-sm text-stone-500">
                  暂无图片资产
                </div>
              )}
            </section>

            <section className="space-y-3 xl:col-span-2">
              <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <h3 className="text-sm font-semibold text-stone-800">异步任务队列</h3>
                  <p className="text-xs text-stone-500">
                    任务创建时先预扣额度，Worker 成功后结算，失败或取消会自动退款。
                  </p>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <select
                    className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm"
                    value={jobStatusFilter}
                    onChange={(event) => setJobStatusFilter(event.target.value)}
                  >
                    <option value="">全部状态</option>
                    <option value="queued">排队中</option>
                    <option value="running">执行中</option>
                    <option value="succeeded">已成功</option>
                    <option value="failed">已失败</option>
                    <option value="cancelled">已取消</option>
                  </select>
                  <Button variant="outline" className="h-10 rounded-xl" onClick={() => void refreshImageJobs()}>
                    <RotateCcw className="size-4" />
                    刷新任务
                  </Button>
                  <Button variant="outline" className="h-10 rounded-xl" onClick={() => void handleRecoverStaleJobs()} disabled={isJobRecovering}>
                    {isJobRecovering ? <LoaderCircle className="size-4 animate-spin" /> : null}
                    恢复卡死
                  </Button>
                  <Button className="h-10 rounded-xl" onClick={() => void handleRunNextImageJob()} disabled={isJobRunning}>
                    {isJobRunning ? <LoaderCircle className="size-4 animate-spin" /> : null}
                    执行下一项
                  </Button>
                </div>
              </div>

              <div className="overflow-hidden rounded-xl border border-stone-200 bg-white">
                <div className="max-h-[360px] overflow-auto">
                  <Table>
                    <TableHeader className="sticky top-0 bg-white">
                      <TableRow>
                        <TableHead>任务</TableHead>
                        <TableHead>用户</TableHead>
                        <TableHead>状态</TableHead>
                        <TableHead>额度</TableHead>
                        <TableHead>更新时间</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {filteredImageJobs.length ? filteredImageJobs.map((job) => (
                        <TableRow key={job.id}>
                          <TableCell className="max-w-80">
                            <div className="truncate font-medium text-stone-800" title={job.prompt_preview || job.request?.prompt || ""}>
                              {job.prompt_preview || job.request?.prompt || job.id}
                            </div>
                            <div className="text-xs text-stone-400">{job.id} · {job.request?.model || "auto"}</div>
                          </TableCell>
                          <TableCell className="max-w-52 truncate">
                            {job.owner?.email || job.owner?.user_id || job.owner?.key_name || job.owner?.key_id || "—"}
                          </TableCell>
                          <TableCell>
                            <Badge variant={job.status === "succeeded" ? "success" : job.status === "failed" ? "danger" : job.status === "running" ? "warning" : "outline"}>
                              {jobStatusLabel(job.status)}
                            </Badge>
                            {job.error?.message ? <div className="mt-1 max-w-56 truncate text-xs text-rose-500">{job.error.message}</div> : null}
                          </TableCell>
                          <TableCell className="text-xs text-stone-500">
                            预扣 {job.reserved_quota ?? 0} · 退款 {job.refunded_quota ?? 0} · 成本 {job.cost_units ?? 0}
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-xs text-stone-500">
                            {formatDateTime(job.updated_at)}
                          </TableCell>
                        </TableRow>
                      )) : (
                        <TableRow>
                          <TableCell colSpan={5} className="py-8 text-center text-sm text-stone-500">
                            暂无异步任务
                          </TableCell>
                        </TableRow>
                      )}
                    </TableBody>
                  </Table>
                </div>
              </div>

              <div className="space-y-2 rounded-xl border border-rose-100 bg-rose-50/50 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h4 className="text-sm font-semibold text-rose-900">Dead-letter 失败任务</h4>
                    <p className="text-xs text-rose-700">达到最大重试次数后的任务会进入死信，可在确认用户额度足够后重新入队。</p>
                  </div>
                  <Badge variant={deadLetterJobCount ? "danger" : "secondary"}>{deadLetterJobCount}</Badge>
                </div>
                {deadLetterJobs.length ? (
                  <div className="space-y-2">
                    {deadLetterJobs.slice(0, 5).map((job) => (
                      <div key={job.id} className="flex flex-col gap-2 rounded-lg bg-white p-3 text-sm sm:flex-row sm:items-center sm:justify-between">
                        <div className="min-w-0">
                          <div className="truncate font-medium text-stone-800" title={job.prompt_preview || job.id}>
                            {job.prompt_preview || job.id}
                          </div>
                          <div className="text-xs text-stone-500">
                            {job.id} · 尝试 {job.attempts ?? 0}/{job.max_attempts ?? 0} · {formatDateTime(job.dead_lettered_at || job.completed_at)}
                          </div>
                          {job.error?.message ? <div className="mt-1 truncate text-xs text-rose-500">{job.error.message}</div> : null}
                        </div>
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-8 rounded-lg"
                          disabled={isJobRecovering}
                          onClick={() => void handleRetryDeadLetterJob(job)}
                        >
                          重新入队
                        </Button>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="rounded-lg bg-white p-3 text-center text-sm text-stone-500">暂无 dead-letter 任务</div>
                )}
              </div>
            </section>

            <section className="space-y-3 xl:col-span-2">
              <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <h3 className="text-sm font-semibold text-stone-800">额度流水</h3>
                  <p className="text-xs text-stone-500">
                    记录发放、消费、退款和人工调整，用于商业计费、售后查账和风控审计。
                  </p>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <select
                    className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm"
                    value={quotaLedgerUserId}
                    onChange={(event) => {
                      const nextUserId = event.target.value;
                      setQuotaLedgerUserId(nextUserId);
                      void refreshQuotaLedger(nextUserId);
                    }}
                  >
                    <option value="">全部用户</option>
                    {users.map((user) => <option key={user.id} value={user.id}>{user.email}</option>)}
                  </select>
                  <Button variant="outline" className="h-10 rounded-xl" onClick={() => void refreshQuotaLedger()} disabled={isLedgerLoading}>
                    {isLedgerLoading ? <LoaderCircle className="size-4 animate-spin" /> : <RotateCcw className="size-4" />}
                    刷新流水
                  </Button>
                  <Button variant="outline" className="h-10 rounded-xl" onClick={exportQuotaLedger} disabled={!quotaLedger.length}>
                    <Download className="size-4" />
                    导出 CSV
                  </Button>
                </div>
              </div>

              <div className="overflow-hidden rounded-xl border border-stone-200 bg-white">
                <div className="max-h-[520px] overflow-auto">
                  <Table>
                    <TableHeader className="sticky top-0 bg-white">
                      <TableRow>
                        <TableHead>时间</TableHead>
                        <TableHead>用户</TableHead>
                        <TableHead>类型</TableHead>
                        <TableHead>变动</TableHead>
                        <TableHead>余额</TableHead>
                        <TableHead>原因 / 关联</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {quotaLedger.length ? quotaLedger.map((item) => (
                        <TableRow key={item.id}>
                          <TableCell className="whitespace-nowrap text-xs text-stone-500">
                            {formatDateTime(item.created_at)}
                          </TableCell>
                          <TableCell className="max-w-52 truncate" title={item.user_id}>
                            <div className="font-medium text-stone-800">{userLabel(item.user_id)}</div>
                            <div className="text-xs text-stone-400">{item.user_id}</div>
                          </TableCell>
                          <TableCell>
                            <Badge variant={item.type === "consume" ? "warning" : "outline"}>
                              {ledgerTypeLabel(item.type)}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <Badge variant={item.amount > 0 ? "success" : item.amount < 0 ? "danger" : "secondary"}>
                              {amountLabel(item.amount)}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-xs text-stone-500">
                            {item.balance_before} → {item.balance_after}
                          </TableCell>
                          <TableCell className="max-w-72 text-xs text-stone-500">
                            <div className="truncate" title={item.reason || ""}>原因：{item.reason || "—"}</div>
                            <div className="truncate" title={`${item.ref_type || ""}:${item.ref_id || ""}`}>
                              关联：{item.ref_type || "—"} / {item.ref_id || "—"}
                            </div>
                          </TableCell>
                        </TableRow>
                      )) : (
                        <TableRow>
                          <TableCell colSpan={6} className="py-8 text-center text-sm text-stone-500">
                            暂无额度流水
                          </TableCell>
                        </TableRow>
                      )}
                    </TableBody>
                  </Table>
                </div>
              </div>
            </section>
          </div>
        )}

        <Dialog open={Boolean(quotaUser)} onOpenChange={(open) => !open && setQuotaUser(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>调整用户额度</DialogTitle>
              <DialogDescription>
                {quotaUser?.email} 当前额度 {quotaUser?.quota_balance ?? 0}。可输入负数扣减额度。
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-3">
              <Input
                value={quotaDelta}
                onChange={(event) => setQuotaDelta(event.target.value)}
                type="number"
                placeholder="额度变化，例如 10 或 -5"
                className="h-11 rounded-xl"
              />
              <Input
                value={quotaReason}
                onChange={(event) => setQuotaReason(event.target.value)}
                placeholder="调整原因（会进入审计流水）"
                className="h-11 rounded-xl"
              />
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setQuotaUser(null)}>取消</Button>
              <Button onClick={() => void submitQuotaDialog()} disabled={isSaving}>确认调整</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        <Dialog open={Boolean(passwordUser)} onOpenChange={(open) => !open && setPasswordUser(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>重置用户密码</DialogTitle>
              <DialogDescription>
                {passwordUser?.email} 的新密码不会明文保存，请只通过安全渠道告知用户。
              </DialogDescription>
            </DialogHeader>
            <Input
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              type="password"
              placeholder="新密码，至少 8 位且不能是弱密码"
              className="h-11 rounded-xl"
            />
            <DialogFooter>
              <Button variant="outline" onClick={() => setPasswordUser(null)}>取消</Button>
              <Button onClick={() => void submitPasswordDialog()} disabled={isSaving}>确认重置</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        <Dialog open={Boolean(confirmAction)} onOpenChange={(open) => !open && setConfirmAction(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{confirmAction?.title}</DialogTitle>
              <DialogDescription className="whitespace-pre-line">{confirmAction?.description}</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirmAction(null)}>取消</Button>
              <Button
                onClick={() => void (async () => {
                  if (!confirmAction) return;
                  try {
                    await confirmAction.action();
                    setConfirmAction(null);
                  } catch (error) {
                    toast.error(error instanceof Error ? error.message : "操作失败");
                  }
                })()}
              >
                确认
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}
