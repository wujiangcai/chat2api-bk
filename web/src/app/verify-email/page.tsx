"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, LoaderCircle, MailCheck } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { confirmEmailVerification, requestEmailVerification } from "@/lib/api";

type VerifyStatus = "idle" | "submitting" | "success" | "failed";

export default function VerifyEmailPage() {
  const [token, setToken] = useState("");
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<VerifyStatus>("idle");
  const [message, setMessage] = useState("打开邮件里的验证链接，或复制 token 到这里完成邮箱验证。");
  const [resending, setResending] = useState(false);

  const confirmToken = useCallback(async (nextToken: string) => {
    const normalizedToken = nextToken.trim();
    if (!normalizedToken) {
      toast.error("请输入邮箱验证 token");
      return;
    }
    setStatus("submitting");
    try {
      const result = await confirmEmailVerification(normalizedToken);
      setStatus("success");
      setMessage(`${result.user.email} 已完成邮箱验证，现在可以登录。`);
      toast.success("邮箱验证成功");
    } catch (error) {
      setStatus("failed");
      setMessage(error instanceof Error ? error.message : "邮箱验证失败");
      toast.error(error instanceof Error ? error.message : "邮箱验证失败");
    }
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const queryToken = params.get("token") || "";
    if (!queryToken) return;
    setToken(queryToken);
    void confirmToken(queryToken);
  }, [confirmToken]);

  const handleResend = async () => {
    const normalizedEmail = email.trim();
    if (!normalizedEmail) {
      toast.error("请输入注册邮箱");
      return;
    }
    setResending(true);
    try {
      const result = await requestEmailVerification(normalizedEmail);
      if (result.token) {
        setToken(result.token);
        toast.success(`验证 token：${result.token}`);
      } else if (result.email_sent === false) {
        toast.error("验证邮件发送失败，请稍后重试或联系管理员");
      } else {
        toast.success("如果邮箱存在，验证邮件已发送");
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "发送失败");
    } finally {
      setResending(false);
    }
  };

  return (
    <div className="grid min-h-[calc(100vh-1rem)] w-full place-items-center px-4 py-6">
      <Card className="w-full max-w-[520px] rounded-[30px] border-white/80 bg-white/95 shadow-[0_28px_90px_rgba(28,25,23,0.10)]">
        <CardContent className="space-y-7 p-6 sm:p-8">
          <div className="space-y-4 text-center">
            <div className="mx-auto inline-flex size-14 items-center justify-center rounded-[18px] bg-stone-950 text-white shadow-sm">
              {status === "success" ? <CheckCircle2 className="size-5" /> : <MailCheck className="size-5" />}
            </div>
            <div className="space-y-2">
              <h1 className="text-3xl font-semibold tracking-tight text-stone-950">邮箱验证</h1>
              <p className="text-sm leading-6 text-stone-500">{message}</p>
            </div>
          </div>

          <div className="space-y-3">
            <label className="block text-sm font-medium text-stone-700">验证 token</label>
            <Input
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="ev-..."
              className="h-13 rounded-2xl border-stone-200 bg-white px-4"
              disabled={status === "submitting" || status === "success"}
            />
            <Button
              className="h-13 w-full rounded-2xl bg-stone-950 text-white hover:bg-stone-800"
              onClick={() => void confirmToken(token)}
              disabled={status === "submitting" || status === "success"}
            >
              {status === "submitting" ? <LoaderCircle className="size-4 animate-spin" /> : null}
              确认邮箱验证
            </Button>
          </div>

          {status !== "success" ? (
            <div className="space-y-3 rounded-2xl bg-stone-50 p-4">
              <p className="text-xs leading-5 text-stone-500">没有收到邮件？输入注册邮箱重新发送验证链接。</p>
              <Input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="user@example.com"
                className="h-12 rounded-2xl border-stone-200 bg-white px-4"
              />
              <Button variant="outline" className="h-12 w-full rounded-2xl" onClick={() => void handleResend()} disabled={resending}>
                {resending ? <LoaderCircle className="size-4 animate-spin" /> : null}
                重新发送验证邮件
              </Button>
            </div>
          ) : null}

          <div className="text-center text-sm text-stone-500">
            <Link href="/login" className="font-medium text-stone-950 hover:underline">
              返回登录
            </Link>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
