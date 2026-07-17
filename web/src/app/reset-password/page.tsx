"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CheckCircle2, LoaderCircle, LockKeyhole } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { confirmPasswordReset, requestPasswordReset } from "@/lib/api";

type ResetStatus = "idle" | "submitting" | "success" | "failed";

export default function ResetPasswordPage() {
  const [token, setToken] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [status, setStatus] = useState<ResetStatus>("idle");
  const [message, setMessage] = useState("打开邮件里的重置链接，或复制 token 到这里设置新密码。");
  const [requesting, setRequesting] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const queryToken = params.get("token") || "";
    if (queryToken) {
      setToken(queryToken);
    }
  }, []);

  const handleReset = async () => {
    const normalizedToken = token.trim();
    if (!normalizedToken) {
      toast.error("请输入重置 token");
      return;
    }
    if (!password) {
      toast.error("请输入新密码");
      return;
    }
    if (password !== confirmPassword) {
      toast.error("两次输入的密码不一致");
      return;
    }
    setStatus("submitting");
    try {
      await confirmPasswordReset(normalizedToken, password);
      setStatus("success");
      setMessage("密码已重置，旧会话已失效，请使用新密码登录。");
      toast.success("密码已重置");
      setPassword("");
      setConfirmPassword("");
    } catch (error) {
      setStatus("failed");
      setMessage(error instanceof Error ? error.message : "密码重置失败");
      toast.error(error instanceof Error ? error.message : "密码重置失败");
    }
  };

  const handleRequest = async () => {
    const normalizedEmail = email.trim();
    if (!normalizedEmail) {
      toast.error("请输入邮箱");
      return;
    }
    setRequesting(true);
    try {
      const result = await requestPasswordReset(normalizedEmail);
      if (result.token) {
        setToken(result.token);
        toast.success(`重置 token：${result.token}`);
      } else if (result.email_sent === false) {
        toast.error("重置邮件发送失败，请稍后重试或联系管理员");
      } else {
        toast.success("如果邮箱存在，重置链接已发送");
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "申请失败");
    } finally {
      setRequesting(false);
    }
  };

  return (
    <div className="grid min-h-[calc(100vh-1rem)] w-full place-items-center px-4 py-6">
      <Card className="w-full max-w-[520px] rounded-[30px] border-white/80 bg-white/95 shadow-[0_28px_90px_rgba(28,25,23,0.10)]">
        <CardContent className="space-y-7 p-6 sm:p-8">
          <div className="space-y-4 text-center">
            <div className="mx-auto inline-flex size-14 items-center justify-center rounded-[18px] bg-stone-950 text-white shadow-sm">
              {status === "success" ? <CheckCircle2 className="size-5" /> : <LockKeyhole className="size-5" />}
            </div>
            <div className="space-y-2">
              <h1 className="text-3xl font-semibold tracking-tight text-stone-950">重置密码</h1>
              <p className="text-sm leading-6 text-stone-500">{message}</p>
            </div>
          </div>

          <div className="space-y-4">
            <div className="space-y-2">
              <label className="block text-sm font-medium text-stone-700">重置 token</label>
              <Input
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="pr-..."
                className="h-13 rounded-2xl border-stone-200 bg-white px-4"
                disabled={status === "submitting" || status === "success"}
              />
            </div>
            <div className="space-y-2">
              <label className="block text-sm font-medium text-stone-700">新密码</label>
              <Input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="至少 8 位"
                className="h-13 rounded-2xl border-stone-200 bg-white px-4"
                disabled={status === "submitting" || status === "success"}
              />
            </div>
            <div className="space-y-2">
              <label className="block text-sm font-medium text-stone-700">确认密码</label>
              <Input
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                placeholder="再次输入新密码"
                className="h-13 rounded-2xl border-stone-200 bg-white px-4"
                disabled={status === "submitting" || status === "success"}
              />
            </div>
            <Button
              className="h-13 w-full rounded-2xl bg-stone-950 text-white hover:bg-stone-800"
              onClick={() => void handleReset()}
              disabled={status === "submitting" || status === "success"}
            >
              {status === "submitting" ? <LoaderCircle className="size-4 animate-spin" /> : null}
              设置新密码
            </Button>
          </div>

          {status !== "success" ? (
            <div className="space-y-3 rounded-2xl bg-stone-50 p-4">
              <p className="text-xs leading-5 text-stone-500">没有重置链接？输入账号邮箱重新发送。</p>
              <Input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="user@example.com"
                className="h-12 rounded-2xl border-stone-200 bg-white px-4"
              />
              <Button variant="outline" className="h-12 w-full rounded-2xl" onClick={() => void handleRequest()} disabled={requesting}>
                {requesting ? <LoaderCircle className="size-4 animate-spin" /> : null}
                发送重置邮件
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
