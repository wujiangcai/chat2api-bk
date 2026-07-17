"use client";

import { useEffect, useState } from "react";
import { LoaderCircle, MessageSquare, Paperclip, RefreshCw, Send } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  addAdminSupportTicketMessage,
  fetchAdminSupportTicket,
  fetchAdminSupportTickets,
  updateAdminSupportTicket,
  uploadAdminSupportTicketAttachment,
  type SupportTicketAttachment,
  type SupportTicketItem,
} from "@/lib/api";

function statusLabel(status: string) {
  if (status === "open") return "待处理";
  if (status === "in_progress") return "处理中";
  if (status === "resolved") return "已解决";
  if (status === "closed") return "已关闭";
  return status;
}

function priorityLabel(priority: string) {
  if (priority === "low") return "低";
  if (priority === "normal") return "普通";
  if (priority === "high") return "高";
  if (priority === "urgent") return "紧急";
  return priority;
}

function slaLabel(ticket: SupportTicketItem) {
  if (ticket.sla_status === "response_overdue") return "首次响应超时";
  if (ticket.sla_status === "resolution_overdue") return "解决超时";
  if (ticket.sla_status === "resolved") return "SLA 已结束";
  return "SLA 正常";
}

function badgeVariant(status: string) {
  if (status === "resolved" || status === "closed") return "success" as const;
  if (status === "in_progress") return "warning" as const;
  return "secondary" as const;
}

function formatDateTime(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatFileSize(value?: number | null) {
  const size = Number(value || 0);
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

function AttachmentList({ attachments }: { attachments?: SupportTicketAttachment[] }) {
  if (!attachments?.length) return null;
  return (
    <div className="mt-3 space-y-2">
      {attachments.map((attachment) => (
        <a
          key={attachment.id}
          href={attachment.url || "#"}
          target="_blank"
          rel="noreferrer"
          className="flex items-center justify-between gap-3 rounded-lg border border-stone-200 bg-white px-3 py-2 text-xs text-stone-600 transition hover:border-stone-300 hover:text-stone-900"
        >
          <span className="flex min-w-0 items-center gap-2">
            <Paperclip className="size-3.5 shrink-0" />
            <span className="truncate">{attachment.filename}</span>
          </span>
          <span className="shrink-0 text-stone-400">{formatFileSize(attachment.size_bytes)}</span>
        </a>
      ))}
    </div>
  );
}

export function SupportTicketsCard() {
  const [items, setItems] = useState<SupportTicketItem[]>([]);
  const [selected, setSelected] = useState<SupportTicketItem | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");
  const [reply, setReply] = useState("");
  const [replyFile, setReplyFile] = useState<File | null>(null);
  const [internal, setInternal] = useState(false);
  const [assigneeName, setAssigneeName] = useState("");

  const load = async () => {
    setIsLoading(true);
    try {
      const result = await fetchAdminSupportTickets({ status: statusFilter, limit: 100 });
      setItems(result.items);
      if (selected) {
        const detail = await fetchAdminSupportTicket(selected.id);
        setSelected(detail.item);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "工单加载失败");
    } finally {
      setIsLoading(false);
    }
  };

  const handleSelect = async (item: SupportTicketItem) => {
    try {
      const result = await fetchAdminSupportTicket(item.id);
      setSelected(result.item);
      setAssigneeName(result.item.assignee_name || "");
      setReplyFile(null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "读取工单失败");
    }
  };

  const handleUpdate = async (updates: { status?: string; priority?: string }) => {
    if (!selected) return;
    setIsSaving(true);
    try {
      const result = await updateAdminSupportTicket(selected.id, {
        ...updates,
        assignee_name: assigneeName.trim() || null,
      });
      setSelected(result.item);
      toast.success("工单已更新");
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新工单失败");
    } finally {
      setIsSaving(false);
    }
  };

  const handleReply = async () => {
    if (!selected || (!reply.trim() && !replyFile)) {
      toast.error("请填写回复内容或选择附件");
      return;
    }
    setIsSaving(true);
    try {
      const result = replyFile
        ? await uploadAdminSupportTicketAttachment(selected.id, replyFile, reply.trim(), internal)
        : await addAdminSupportTicketMessage(selected.id, reply.trim(), internal);
      setSelected(result.item);
      setReply("");
      setReplyFile(null);
      setInternal(false);
      toast.success(replyFile ? "附件已上传" : internal ? "内部备注已保存" : "客服回复已发送");
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "回复失败");
    } finally {
      setIsSaving(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  return (
    <Card className="overflow-hidden border-white/80 bg-white/90">
      <CardContent className="space-y-5 p-5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <MessageSquare className="size-5 text-stone-700" />
              <h2 className="text-lg font-semibold text-stone-900">客服工单</h2>
            </div>
            <p className="mt-2 text-sm text-stone-500">集中处理用户提交的账单、账号、图片生成、API 和退款问题。</p>
          </div>
          <div className="flex gap-2">
            <select
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
              className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm outline-none"
            >
              <option value="">全部状态</option>
              <option value="open">待处理</option>
              <option value="in_progress">处理中</option>
              <option value="resolved">已解决</option>
              <option value="closed">已关闭</option>
            </select>
            <Button variant="outline" className="h-10 rounded-xl" onClick={() => void load()} disabled={isLoading}>
              {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
              刷新
            </Button>
          </div>
        </div>

        <div className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
          <div className="space-y-2">
            {items.length ? items.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => void handleSelect(item)}
                className="w-full rounded-xl border border-stone-100 bg-white p-3 text-left transition hover:border-stone-200 hover:bg-stone-50"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-stone-800">{item.subject}</div>
                    <div className="mt-1 text-xs text-stone-500">
                      {item.email || item.user_id || "-"} · {formatDateTime(item.last_message_at || item.updated_at)}
                    </div>
                    <div className="mt-1 text-xs text-stone-500">
                      {item.category} · 优先级 {priorityLabel(item.priority)} · {item.message_count} 条消息
                    </div>
                    <div className={item.sla_status?.includes("overdue") ? "mt-1 text-xs text-rose-600" : "mt-1 text-xs text-stone-500"}>
                      {slaLabel(item)} · 首响 {formatDateTime(item.first_response_due_at)}
                    </div>
                  </div>
                  <Badge variant={badgeVariant(item.status)}>{statusLabel(item.status)}</Badge>
                </div>
              </button>
            )) : (
              <div className="rounded-xl bg-stone-50 p-8 text-center text-sm text-stone-500">暂无工单</div>
            )}
          </div>

          <div className="rounded-xl border border-stone-100 bg-white p-4">
            {selected ? (
              <div className="space-y-4">
                <div className="flex flex-col gap-2 border-b border-stone-100 pb-4 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <div className="text-base font-semibold text-stone-900">{selected.subject}</div>
                    <div className="mt-1 text-xs text-stone-500">{selected.id} · {selected.email || selected.user_id}</div>
                    <div className={selected.sla_status?.includes("overdue") ? "mt-1 text-xs text-rose-600" : "mt-1 text-xs text-stone-500"}>
                      {slaLabel(selected)} · 首响截止 {formatDateTime(selected.first_response_due_at)} · 解决截止 {formatDateTime(selected.resolution_due_at)}
                    </div>
                  </div>
                  <Badge variant={badgeVariant(selected.status)}>{statusLabel(selected.status)}</Badge>
                </div>

                <div className="grid gap-2 sm:grid-cols-3">
                  <select
                    value={selected.status}
                    onChange={(event) => void handleUpdate({ status: event.target.value })}
                    className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm outline-none"
                    disabled={isSaving}
                  >
                    <option value="open">待处理</option>
                    <option value="in_progress">处理中</option>
                    <option value="resolved">已解决</option>
                    <option value="closed">已关闭</option>
                  </select>
                  <select
                    value={selected.priority}
                    onChange={(event) => void handleUpdate({ priority: event.target.value })}
                    className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm outline-none"
                    disabled={isSaving}
                  >
                    <option value="low">低</option>
                    <option value="normal">普通</option>
                    <option value="high">高</option>
                    <option value="urgent">紧急</option>
                  </select>
                  <Input
                    value={assigneeName}
                    onChange={(event) => setAssigneeName(event.target.value)}
                    onBlur={() => void handleUpdate({})}
                    placeholder="处理人"
                    className="h-10 rounded-xl"
                    disabled={isSaving}
                  />
                </div>

                <div className="max-h-96 space-y-3 overflow-auto pr-1">
                  {(selected.messages || []).map((item) => (
                    <div key={item.id} className={item.internal ? "rounded-xl border border-amber-100 bg-amber-50 p-3" : "rounded-xl border border-stone-100 bg-stone-50 p-3"}>
                      <div className="flex items-center justify-between gap-2 text-xs text-stone-500">
                        <span>{item.internal ? "内部备注" : item.author_type === "admin" ? "客服" : "用户"}</span>
                        <span>{formatDateTime(item.created_at)}</span>
                      </div>
                      <div className="mt-2 whitespace-pre-wrap text-sm leading-6 text-stone-700">{item.body}</div>
                      <AttachmentList attachments={item.attachments} />
                    </div>
                  ))}
                </div>

                <div className="space-y-2 border-t border-stone-100 pt-4">
                  <Textarea value={reply} onChange={(event) => setReply(event.target.value)} placeholder="回复用户或记录内部备注" className="min-h-28 rounded-xl" />
                  <label className="flex cursor-pointer items-center justify-between gap-3 rounded-xl border border-dashed border-stone-200 bg-stone-50 px-3 py-2 text-xs text-stone-500 hover:border-stone-300">
                    <span className="flex min-w-0 items-center gap-2">
                      <Paperclip className="size-4" />
                      <span className="truncate">{replyFile ? replyFile.name : "可选：上传截图 / PDF / 日志附件"}</span>
                    </span>
                    <input
                      type="file"
                      className="hidden"
                      accept="image/png,image/jpeg,image/webp,application/pdf,text/plain"
                      onChange={(event) => setReplyFile(event.target.files?.[0] || null)}
                    />
                  </label>
                  <label className="flex items-center gap-2 text-xs text-stone-500">
                    <input type="checkbox" checked={internal} onChange={(event) => setInternal(event.target.checked)} />
                    仅内部可见
                  </label>
                  <Button className="h-10 rounded-xl" onClick={() => void handleReply()} disabled={isSaving}>
                    {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Send className="size-4" />}
                    {internal ? "保存内部备注" : "发送客服回复"}
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex min-h-80 items-center justify-center rounded-xl bg-stone-50 text-sm text-stone-500">请选择工单查看详情</div>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
