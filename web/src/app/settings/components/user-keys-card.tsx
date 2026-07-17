"use client";

import { useEffect, useRef, useState } from "react";
import { Ban, CheckCircle2, Copy, KeyRound, LoaderCircle, Pencil, Plus, RotateCcw, Trash2 } from "lucide-react";
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
import { createUserKey, deleteUserKey, fetchUserKeys, updateUserKey, type UserKey, type UserKeyPayload } from "@/lib/api";

const DEFAULT_PERMISSIONS = ["image.generate", "image.edit"];

const PERMISSION_OPTIONS = [
  { value: "image.generate", label: "文生图" },
  { value: "image.edit", label: "图生图" },
  { value: "chat.completions", label: "Chat API" },
  { value: "responses.create", label: "Responses" },
  { value: "messages.create", label: "Messages" },
];

function formatDateTime(value?: string | null) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function toDateTimeLocal(value?: string | null) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const offsetDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return offsetDate.toISOString().slice(0, 16);
}

function toIsoOrNull(value: string) {
  return value.trim() ? new Date(value).toISOString() : null;
}

type UserFormState = {
  name: string;
  permissions: string[];
  quotaLimit: string;
  rateLimit: string;
  expiresAt: string;
};

const emptyForm: UserFormState = {
  name: "",
  permissions: DEFAULT_PERMISSIONS,
  quotaLimit: "",
  rateLimit: "",
  expiresAt: "",
};

function payloadFromForm(form: UserFormState): UserKeyPayload {
  const quotaLimited = Boolean(form.quotaLimit.trim());
  const rateLimited = Boolean(form.rateLimit.trim());
  const expiresAt = toIsoOrNull(form.expiresAt);
  return {
    name: form.name.trim(),
    permissions: form.permissions,
    ...(quotaLimited ? { quota_limit: Number(form.quotaLimit) } : { quota_unlimited: true }),
    ...(rateLimited ? { rate_limit_per_minute: Number(form.rateLimit) } : { rate_limit_unlimited: true }),
    ...(expiresAt ? { expires_at: expiresAt } : { expires_never: true }),
  };
}

export function UserKeysCard() {
  const didLoadRef = useRef(false);
  const [items, setItems] = useState<UserKey[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [createForm, setCreateForm] = useState<UserFormState>(emptyForm);
  const [isCreating, setIsCreating] = useState(false);
  const [pendingIds, setPendingIds] = useState<Set<string>>(() => new Set());
  const [revealedKey, setRevealedKey] = useState("");
  const [deletingItem, setDeletingItem] = useState<UserKey | null>(null);
  const [editingItem, setEditingItem] = useState<UserKey | null>(null);
  const [editForm, setEditForm] = useState<UserFormState>(emptyForm);

  const load = async () => {
    setIsLoading(true);
    try {
      const data = await fetchUserKeys();
      setItems(data.items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载用户密钥失败");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void load();
  }, []);

  const updateCreateForm = (patch: Partial<UserFormState>) => {
    setCreateForm((current) => ({ ...current, ...patch }));
  };

  const updateEditForm = (patch: Partial<UserFormState>) => {
    setEditForm((current) => ({ ...current, ...patch }));
  };

  const toggleFormPermission = (form: UserFormState, onChange: (patch: Partial<UserFormState>) => void, value: string) => {
    const permissions = form.permissions.includes(value)
      ? form.permissions.filter((item) => item !== value)
      : [...form.permissions, value];
    onChange({ permissions });
  };

  const handleCreate = async () => {
    setIsCreating(true);
    try {
      const data = await createUserKey(payloadFromForm(createForm));
      setItems(data.items);
      setRevealedKey(data.key);
      setCreateForm(emptyForm);
      setIsDialogOpen(false);
      toast.success("用户密钥已创建");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "创建用户密钥失败");
    } finally {
      setIsCreating(false);
    }
  };

  const setItemPending = (id: string, isPending: boolean) => {
    setPendingIds((current) => {
      const next = new Set(current);
      if (isPending) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  };

  const handleToggle = async (item: UserKey) => {
    setItemPending(item.id, true);
    try {
      const data = await updateUserKey(item.id, { enabled: !item.enabled });
      setItems(data.items);
      toast.success(item.enabled ? "用户密钥已禁用" : "用户密钥已启用");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新用户密钥失败");
    } finally {
      setItemPending(item.id, false);
    }
  };

  const openEditDialog = (item: UserKey) => {
    setEditingItem(item);
    setEditForm({
      name: item.name || "",
      permissions: item.permissions?.length ? item.permissions : DEFAULT_PERMISSIONS,
      quotaLimit: item.quota_limit == null ? "" : String(item.quota_limit),
      rateLimit: item.rate_limit_per_minute == null ? "" : String(item.rate_limit_per_minute),
      expiresAt: toDateTimeLocal(item.expires_at),
    });
  };

  const handleEdit = async () => {
    if (!editingItem) {
      return;
    }
    const item = editingItem;
    setItemPending(item.id, true);
    try {
      const data = await updateUserKey(item.id, payloadFromForm(editForm));
      setItems(data.items);
      setEditingItem(null);
      toast.success("用户权限已更新");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新用户权限失败");
    } finally {
      setItemPending(item.id, false);
    }
  };

  const handleDelete = async () => {
    if (!deletingItem) {
      return;
    }
    const item = deletingItem;
    setItemPending(item.id, true);
    try {
      const data = await deleteUserKey(item.id);
      setItems(data.items);
      setDeletingItem(null);
      toast.success("用户密钥已删除");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除用户密钥失败");
    } finally {
      setItemPending(item.id, false);
    }
  };

  const handleResetQuota = async (item: UserKey) => {
    setItemPending(item.id, true);
    try {
      const data = await updateUserKey(item.id, { reset_quota_used: true });
      setItems(data.items);
      toast.success("用户用量已重置");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重置用户用量失败");
    } finally {
      setItemPending(item.id, false);
    }
  };

  const handleCopy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败，请手动复制");
    }
  };

  const renderUserForm = (form: UserFormState, onChange: (patch: Partial<UserFormState>) => void) => (
    <div className="space-y-4">
      <div className="space-y-2">
        <label className="text-sm font-medium text-stone-700">名称（可选）</label>
        <Input
          value={form.name}
          onChange={(event) => onChange({ name: event.target.value })}
          placeholder="例如：设计同学 A、运营临时账号"
          className="h-11 rounded-xl border-stone-200 bg-white"
        />
      </div>
      <div className="space-y-2">
        <label className="text-sm font-medium text-stone-700">权限</label>
        <div className="grid gap-2 sm:grid-cols-2">
          {PERMISSION_OPTIONS.map((option) => (
            <label key={option.value} className="flex items-center gap-2 rounded-xl border border-stone-200 bg-white px-3 py-2 text-sm text-stone-700">
              <input
                type="checkbox"
                checked={form.permissions.includes(option.value)}
                onChange={() => toggleFormPermission(form, onChange, option.value)}
              />
              {option.label}
            </label>
          ))}
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="space-y-2">
          <label className="text-sm font-medium text-stone-700">总额度</label>
          <Input
            value={form.quotaLimit}
            onChange={(event) => onChange({ quotaLimit: event.target.value })}
            placeholder="留空不限"
            type="number"
            min="0"
            className="h-11 rounded-xl border-stone-200 bg-white"
          />
        </div>
        <div className="space-y-2">
          <label className="text-sm font-medium text-stone-700">每分钟限制</label>
          <Input
            value={form.rateLimit}
            onChange={(event) => onChange({ rateLimit: event.target.value })}
            placeholder="留空不限"
            type="number"
            min="0"
            className="h-11 rounded-xl border-stone-200 bg-white"
          />
        </div>
        <div className="space-y-2">
          <label className="text-sm font-medium text-stone-700">到期时间</label>
          <Input
            value={form.expiresAt}
            onChange={(event) => onChange({ expiresAt: event.target.value })}
            type="datetime-local"
            className="h-11 rounded-xl border-stone-200 bg-white"
          />
        </div>
      </div>
    </div>
  );

  return (
    <>
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-6 p-6">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="flex size-10 items-center justify-center rounded-xl bg-stone-100">
                <KeyRound className="size-5 text-stone-600" />
              </div>
              <div>
                <h2 className="text-lg font-semibold tracking-tight">用户密钥管理</h2>
                <p className="text-sm text-stone-500">为普通用户创建专用密钥，并配置接口权限、额度、限速和到期时间。</p>
              </div>
            </div>
            <Button className="h-9 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800" onClick={() => setIsDialogOpen(true)}>
              <Plus className="size-4" />
              创建用户密钥
            </Button>
          </div>

          {revealedKey ? (
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-4 text-sm text-emerald-900">
              <div className="font-medium">新密钥仅展示一次，请立即保存：</div>
              <div className="mt-3 flex flex-col gap-3 rounded-lg border border-emerald-200 bg-white/80 p-3 md:flex-row md:items-center md:justify-between">
                <code className="break-all font-mono text-[13px]">{revealedKey}</code>
                <Button
                  type="button"
                  variant="outline"
                  className="h-9 rounded-xl border-emerald-200 bg-white px-4 text-emerald-700"
                  onClick={() => void handleCopy(revealedKey)}
                >
                  <Copy className="size-4" />
                  复制
                </Button>
              </div>
            </div>
          ) : null}

          {isLoading ? (
            <div className="flex items-center justify-center py-10">
              <LoaderCircle className="size-5 animate-spin text-stone-400" />
            </div>
          ) : items.length === 0 ? (
            <div className="rounded-xl bg-stone-50 px-6 py-10 text-center text-sm text-stone-500">
              暂无普通用户密钥。点击右上角按钮后即可创建并分发给其他人。
            </div>
          ) : (
            <div className="space-y-3">
              {items.map((item) => {
                const isPending = pendingIds.has(item.id);
                return (
                  <div key={item.id} className="flex flex-col gap-3 rounded-xl border border-stone-200 bg-white px-4 py-4 xl:flex-row xl:items-center xl:justify-between">
                    <div className="min-w-0 space-y-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <div className="truncate text-sm font-medium text-stone-800">{item.name}</div>
                        <Badge variant={item.enabled ? "success" : "secondary"} className="rounded-md">
                          {item.enabled ? "已启用" : "已禁用"}
                        </Badge>
                      </div>
                      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-500">
                        <span>创建时间 {formatDateTime(item.created_at)}</span>
                        <span>最近使用 {formatDateTime(item.last_used_at)}</span>
                        <span>额度 {item.quota_limit == null ? `已用 ${item.quota_used} / 不限` : `已用 ${item.quota_used} / ${item.quota_limit}，剩余 ${item.quota_remaining ?? 0}`}</span>
                        <span>限速 {item.rate_limit_per_minute ? `${item.rate_limit_per_minute}/分钟` : "不限"}</span>
                        <span>到期 {formatDateTime(item.expires_at)}</span>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {(item.permissions || []).map((permission) => (
                          <Badge key={permission} variant="secondary" className="rounded-md text-[11px]">
                            {PERMISSION_OPTIONS.find((option) => option.value === permission)?.label || permission}
                          </Badge>
                        ))}
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                        onClick={() => openEditDialog(item)}
                        disabled={isPending}
                      >
                        <Pencil className="size-4" />
                        编辑
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                        onClick={() => void handleToggle(item)}
                        disabled={isPending}
                      >
                        {isPending ? (
                          <LoaderCircle className="size-4 animate-spin" />
                        ) : item.enabled ? (
                          <Ban className="size-4" />
                        ) : (
                          <CheckCircle2 className="size-4" />
                        )}
                        {item.enabled ? "禁用" : "启用"}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                        onClick={() => void handleResetQuota(item)}
                        disabled={isPending}
                      >
                        {isPending ? <LoaderCircle className="size-4 animate-spin" /> : <RotateCcw className="size-4" />}
                        重置用量
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        className="h-9 rounded-xl border-rose-200 bg-white px-4 text-rose-600 hover:bg-rose-50 hover:text-rose-700"
                        onClick={() => setDeletingItem(item)}
                        disabled={isPending}
                      >
                        {isPending ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                        删除
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>创建用户密钥</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              创建后会生成一条只能查看一次的原始密钥；权限和额度可后续编辑。
            </DialogDescription>
          </DialogHeader>
          {renderUserForm(createForm, updateCreateForm)}
          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setIsDialogOpen(false)}
              disabled={isCreating}
            >
              取消
            </Button>
            <Button
              type="button"
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleCreate()}
              disabled={isCreating}
            >
              {isCreating ? <LoaderCircle className="size-4 animate-spin" /> : <Plus className="size-4" />}
              创建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(editingItem)} onOpenChange={(open) => (!open ? setEditingItem(null) : null)}>
        <DialogContent className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>编辑用户权限</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              修改「{editingItem?.name}」的权限、总额度、限速和到期时间。总额度留空表示不限。
            </DialogDescription>
          </DialogHeader>
          {renderUserForm(editForm, updateEditForm)}
          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setEditingItem(null)}
              disabled={editingItem ? pendingIds.has(editingItem.id) : false}
            >
              取消
            </Button>
            <Button
              type="button"
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleEdit()}
              disabled={editingItem ? pendingIds.has(editingItem.id) : false}
            >
              {editingItem && pendingIds.has(editingItem.id) ? <LoaderCircle className="size-4 animate-spin" /> : <Pencil className="size-4" />}
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(deletingItem)} onOpenChange={(open) => (!open ? setDeletingItem(null) : null)}>
        <DialogContent className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>删除用户密钥</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              确认删除用户密钥「{deletingItem?.name}」吗？删除后该密钥将无法继续调用接口。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setDeletingItem(null)}
              disabled={deletingItem ? pendingIds.has(deletingItem.id) : false}
            >
              取消
            </Button>
            <Button
              type="button"
              className="h-10 rounded-xl bg-rose-600 px-5 text-white hover:bg-rose-700"
              onClick={() => void handleDelete()}
              disabled={deletingItem ? pendingIds.has(deletingItem.id) : false}
            >
              {deletingItem && pendingIds.has(deletingItem.id) ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
