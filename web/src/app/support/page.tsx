"use client";

import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, LoaderCircle, MessageSquare, Paperclip, RefreshCw, Send } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  addSupportTicketMessage,
  createSupportTicket,
  fetchSupportTicket,
  fetchSupportTickets,
  uploadSupportTicketAttachment,
  type SupportTicketAttachment,
  type SupportTicketItem,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";

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

export default function SupportPage() {
  const { isCheckingAuth, session } = useAuthGuard(["user"]);
  const [items, setItems] = useState<SupportTicketItem[]>([]);
  const [selected, setSelected] = useState<SupportTicketItem | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isReplying, setIsReplying] = useState(false);
  const [subject, setSubject] = useState("");
  const [category, setCategory] = useState("image");
  const [priority, setPriority] = useState("normal");
  const [message, setMessage] = useState("");
  const [reply, setReply] = useState("");
  const [createFile, setCreateFile] = useState<File | null>(null);
  const [replyFile, setReplyFile] = useState<File | null>(null);

  const visibleItems = useMemo(() => items.slice(0, 20), [items]);

  const load = async () => {
    setIsLoading(true);
    try {
      const result = await fetchSupportTickets({ limit: 100 });
      setItems(result.items);
      if (selected) {
        const detail = await fetchSupportTicket(selected.id);
        setSelected(detail.item);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "工单加载失败");
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreate = async () => {
    if (!subject.trim() || !message.trim()) {
      toast.error("请填写标题和问题描述");
      return;
    }
    setIsSubmitting(true);
    try {
      const result = await createSupportTicket({
        subject: subject.trim(),
        message: message.trim(),
        category,
        priority,
      });
      let nextItem = result.item;
      let attachmentFailed = false;
      if (createFile) {
        try {
          const uploadResult = await uploadSupportTicketAttachment(result.item.id, createFile, "初始问题附件");
          nextItem = uploadResult.item;
        } catch (error) {
          attachmentFailed = true;
          toast.error(error instanceof Error ? `工单已提交，但附件上传失败：${error.message}` : "工单已提交，但附件上传失败");
        }
      }
      setSubject("");
      setMessage("");
      setCreateFile(null);
      setSelected(nextItem);
      if (!attachmentFailed) {
        toast.success(createFile ? "工单和附件已提交" : "工单已提交");
      }
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "提交工单失败");
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSelect = async (ticket: SupportTicketItem) => {
    try {
      const result = await fetchSupportTicket(ticket.id);
      setSelected(result.item);
      setReplyFile(null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "读取工单失败");
    }
  };

  const handleReply = async () => {
    if (!selected || (!reply.trim() && !replyFile)) {
      toast.error("请填写回复内容或选择附件");
      return;
    }
    setIsReplying(true);
    try {
      const result = replyFile
        ? await uploadSupportTicketAttachment(selected.id, replyFile, reply.trim())
        : await addSupportTicketMessage(selected.id, reply.trim());
      setSelected(result.item);
      setReply("");
      setReplyFile(null);
      toast.success(replyFile ? "附件已上传" : "回复已发送");
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "回复失败");
    } finally {
      setIsReplying(false);
    }
  };

  useEffect(() => {
    if (session?.role === "user") {
      void load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.role]);

  if (isCheckingAuth || !session || session.role !== "user") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-4 sm:p-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <MessageSquare className="size-5 text-stone-800" />
            <h1 className="text-xl font-semibold text-stone-950">客服工单</h1>
          </div>
          <p className="mt-2 text-sm text-stone-500">提交账单、图片生成、账号和 API 使用问题，客服会在后台回复。</p>
        </div>
        <Button variant="outline" className="h-10 rounded-xl" onClick={() => void load()} disabled={isLoading}>
          {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
          刷新
        </Button>
      </div>

      <div className="grid gap-5 lg:grid-cols-[0.9fr_1.1fr]">
        <div className="space-y-5">
          <Card className="border-white/80 bg-white/90">
            <CardContent className="space-y-3 p-5">
              <h2 className="text-base font-semibold text-stone-900">提交新工单</h2>
              <Input value={subject} onChange={(event) => setSubject(event.target.value)} placeholder="问题标题" className="h-10 rounded-xl" />
              <div className="grid gap-2 sm:grid-cols-2">
                <select value={category} onChange={(event) => setCategory(event.target.value)} className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm outline-none">
                  <option value="image">图片生成</option>
                  <option value="billing">账单/支付</option>
                  <option value="refund">退款</option>
                  <option value="account">账号</option>
                  <option value="api">API</option>
                  <option value="other">其他</option>
                </select>
                <select value={priority} onChange={(event) => setPriority(event.target.value)} className="h-10 rounded-xl border border-stone-200 bg-white px-3 text-sm outline-none">
                  <option value="normal">普通</option>
                  <option value="high">高</option>
                  <option value="urgent">紧急</option>
                  <option value="low">低</option>
                </select>
              </div>
              <Textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="请描述问题、订单号、任务 ID 或截图链接" className="min-h-32 rounded-xl" />
              <label className="flex cursor-pointer items-center justify-between gap-3 rounded-xl border border-dashed border-stone-200 bg-stone-50 px-3 py-2 text-xs text-stone-500 hover:border-stone-300">
                <span className="flex min-w-0 items-center gap-2">
                  <Paperclip className="size-4" />
                  <span className="truncate">{createFile ? createFile.name : "可选：上传截图 / PDF / 日志附件"}</span>
                </span>
                <input
                  type="file"
                  className="hidden"
                  accept="image/png,image/jpeg,image/webp,application/pdf,text/plain"
                  onChange={(event) => setCreateFile(event.target.files?.[0] || null)}
                />
              </label>
              <Button className="h-10 rounded-xl" onClick={() => void handleCreate()} disabled={isSubmitting}>
                {isSubmitting ? <LoaderCircle className="size-4 animate-spin" /> : <Send className="size-4" />}
                提交工单
              </Button>
            </CardContent>
          </Card>

          <Card className="border-white/80 bg-white/90">
            <CardContent className="space-y-3 p-5">
              <h2 className="text-base font-semibold text-stone-900">我的工单</h2>
              {visibleItems.length ? (
                <div className="space-y-2">
                  {visibleItems.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => void handleSelect(item)}
                      className="w-full rounded-xl border border-stone-100 bg-white p-3 text-left transition hover:border-stone-200 hover:bg-stone-50"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium text-stone-800">{item.subject}</div>
                          <div className="mt-1 text-xs text-stone-500">
                            {formatDateTime(item.last_message_at || item.updated_at)} · {item.message_count} 条消息
                          </div>
                          <div className={item.sla_status?.includes("overdue") ? "mt-1 text-xs text-rose-600" : "mt-1 text-xs text-stone-500"}>
                            {slaLabel(item)} · 首响截止 {formatDateTime(item.first_response_due_at)}
                          </div>
                        </div>
                        <Badge variant={badgeVariant(item.status)}>{statusLabel(item.status)}</Badge>
                      </div>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="rounded-xl bg-stone-50 p-6 text-center text-sm text-stone-500">暂无工单</div>
              )}
            </CardContent>
          </Card>
        </div>

        <Card className="border-white/80 bg-white/90">
          <CardContent className="space-y-4 p-5">
            {selected ? (
              <>
                <div className="flex flex-col gap-2 border-b border-stone-100 pb-4 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <div className="text-lg font-semibold text-stone-900">{selected.subject}</div>
                    <div className="mt-1 text-xs text-stone-500">
                      {selected.id} · {selected.category} · 优先级 {priorityLabel(selected.priority)}
                    </div>
                    <div className={selected.sla_status?.includes("overdue") ? "mt-1 text-xs text-rose-600" : "mt-1 text-xs text-stone-500"}>
                      {slaLabel(selected)} · 首响截止 {formatDateTime(selected.first_response_due_at)} · 解决截止 {formatDateTime(selected.resolution_due_at)}
                    </div>
                  </div>
                  <Badge variant={badgeVariant(selected.status)}>{statusLabel(selected.status)}</Badge>
                </div>
                <div className="space-y-3">
                  {(selected.messages || []).map((item) => (
                    <div key={item.id} className="rounded-xl border border-stone-100 bg-stone-50 p-3">
                      <div className="flex items-center justify-between gap-3 text-xs text-stone-500">
                        <span>{item.author_type === "admin" ? "客服" : "我"}</span>
                        <span>{formatDateTime(item.created_at)}</span>
                      </div>
                      <div className="mt-2 whitespace-pre-wrap text-sm leading-6 text-stone-700">{item.body}</div>
                      <AttachmentList attachments={item.attachments} />
                    </div>
                  ))}
                </div>
                {selected.status === "closed" ? (
                  <div className="rounded-xl bg-emerald-50 p-4 text-sm text-emerald-700">
                    <CheckCircle2 className="mr-2 inline size-4" />
                    该工单已关闭。如需继续沟通，请创建新工单。
                  </div>
                ) : (
                  <div className="space-y-2 border-t border-stone-100 pt-4">
                    <Textarea value={reply} onChange={(event) => setReply(event.target.value)} placeholder="继续补充信息或回复客服" className="min-h-28 rounded-xl" />
                    <label className="flex cursor-pointer items-center justify-between gap-3 rounded-xl border border-dashed border-stone-200 bg-stone-50 px-3 py-2 text-xs text-stone-500 hover:border-stone-300">
                      <span className="flex min-w-0 items-center gap-2">
                        <Paperclip className="size-4" />
                        <span className="truncate">{replyFile ? replyFile.name : "可选：上传补充截图 / PDF / 日志附件"}</span>
                      </span>
                      <input
                        type="file"
                        className="hidden"
                        accept="image/png,image/jpeg,image/webp,application/pdf,text/plain"
                        onChange={(event) => setReplyFile(event.target.files?.[0] || null)}
                      />
                    </label>
                    <Button className="h-10 rounded-xl" onClick={() => void handleReply()} disabled={isReplying}>
                      {isReplying ? <LoaderCircle className="size-4 animate-spin" /> : <Send className="size-4" />}
                      发送回复
                    </Button>
                  </div>
                )}
              </>
            ) : (
              <div className="flex min-h-96 items-center justify-center rounded-xl bg-stone-50 text-sm text-stone-500">
                请选择左侧工单查看详情
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
