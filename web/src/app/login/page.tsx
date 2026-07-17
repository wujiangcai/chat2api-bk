"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { LoaderCircle, LockKeyhole } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  confirmPasswordReset,
  fetchAuthCapabilities,
  login,
  loginWithPassword,
  registerWithPassword,
  requestPasswordReset,
  type AuthCapabilities,
  type LoginResponse,
} from "@/lib/api";
import { useRedirectIfAuthenticated } from "@/lib/use-auth-guard";
import { getDefaultRouteForRole, setStoredAuthSession } from "@/store/auth";

type LoginMode = "password" | "register" | "reset" | "key";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<LoginMode>("password");
  const [authKey, setAuthKey] = useState("");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [resetToken, setResetToken] = useState("");
  const [capabilities, setCapabilities] = useState<AuthCapabilities | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const { isCheckingAuth } = useRedirectIfAuthenticated();

  useEffect(() => {
    let active = true;
    const loadCapabilities = async () => {
      try {
        const data = await fetchAuthCapabilities();
        if (!active) return;
        setCapabilities(data);
        if (!data.registration_enabled && mode === "register") {
          setMode("password");
        }
      } catch {
        // 能力探测失败不阻塞登录。
      }
    };
    void loadCapabilities();
    return () => {
      active = false;
    };
  }, [mode]);

  const saveSession = async (data: LoginResponse, fallbackKey = "") => {
    const useCookieSession = Boolean(data.session_cookie);
    const key = useCookieSession ? "" : String(data.token || fallbackKey || "").trim();
    if (!key && !useCookieSession) {
      throw new Error("登录响应缺少 token");
    }
    await setStoredAuthSession({
      key,
      sessionMode: useCookieSession ? "cookie" : "token",
      role: data.role,
      subjectId: data.subject_id,
      keyId: data.key_id,
      email: data.email || undefined,
      name: data.name,
      quotaBalance: data.quota_balance ?? null,
      packageName: data.package_name ?? null,
    });
    router.replace(getDefaultRouteForRole(data.role));
  };

  const handleSubmit = async () => {
    setIsSubmitting(true);
    try {
      if (mode === "key") {
        const normalizedAuthKey = authKey.trim();
        if (!normalizedAuthKey) {
          toast.error("请输入密钥");
          return;
        }
        const data = await login(normalizedAuthKey);
        await saveSession(data, normalizedAuthKey);
        return;
      }

      if (!email.trim()) {
        toast.error("请输入邮箱");
        return;
      }

      if (mode === "reset" && !resetToken.trim()) {
        const result = await requestPasswordReset(email.trim());
        if (result.token) {
          toast.success(`重置 token：${result.token}`);
        } else if (result.email_sent === false) {
          toast.error("重置邮件发送失败，请稍后重试或联系管理员");
        } else {
          toast.success("如果邮箱存在，重置链接已发送");
        }
        return;
      }

      if (!password) {
        toast.error("请输入密码");
        return;
      }

      if (mode === "register") {
        if (capabilities && !capabilities.registration_enabled) {
          toast.error("当前已关闭公开注册，请联系管理员开通账号");
          return;
        }
        if (password !== confirmPassword) {
          toast.error("两次输入的密码不一致");
          return;
        }
        const data = await registerWithPassword(email.trim(), password, name.trim());
        if (data.verification_token) {
          toast.success(`邮箱验证 token：${data.verification_token}`);
        }
        if (data.verification_required) {
          if (data.email_sent === false) {
            toast.error("注册成功，但验证邮件发送失败，请稍后重新发送验证邮件");
          } else {
            toast.success("注册成功，请查收邮箱并完成验证后登录");
          }
          setMode("password");
          setPassword("");
          setConfirmPassword("");
          return;
        }
        await saveSession(data);
        return;
      }

      if (mode === "reset") {
        if (password !== confirmPassword) {
          toast.error("两次输入的密码不一致");
          return;
        }
        await confirmPasswordReset(resetToken.trim(), password);
        toast.success("密码已重置，请重新登录");
        setMode("password");
        setPassword("");
        setConfirmPassword("");
        setResetToken("");
        return;
      }

      const data = await loginWithPassword(email.trim(), password);
      await saveSession(data);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "登录失败");
    } finally {
      setIsSubmitting(false);
    }
  };

  if (isCheckingAuth) {
    return (
      <div className="grid min-h-[calc(100vh-1rem)] w-full place-items-center px-4 py-6">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return (
    <div className="grid min-h-[calc(100vh-1rem)] w-full place-items-center px-4 py-6">
      <Card className="w-full max-w-[505px] rounded-[30px] border-white/80 bg-white/95 shadow-[0_28px_90px_rgba(28,25,23,0.10)]">
        <CardContent className="space-y-7 p-6 sm:p-8">
          <div className="space-y-4 text-center">
            <div className="mx-auto inline-flex size-14 items-center justify-center rounded-[18px] bg-stone-950 text-white shadow-sm">
              <LockKeyhole className="size-5" />
            </div>
            <div className="space-y-2">
              <h1 className="text-3xl font-semibold tracking-tight text-stone-950">欢迎回来</h1>
              <p className="text-sm leading-6 text-stone-500">
                使用邮箱账号、注册新账号、找回密码，或通过旧版密钥登录。
              </p>
            </div>
          </div>

          <div className="grid grid-cols-4 gap-2 rounded-2xl bg-stone-100 p-1 text-sm">
            {[
              ["password", "账号登录"],
              ["register", "注册"],
              ["reset", "找回"],
              ["key", "密钥登录"],
            ].map(([value, label]) => (
              <button
                key={value}
                type="button"
                disabled={value === "register" && capabilities?.registration_enabled === false}
                className={`rounded-xl px-3 py-2 transition ${
                  mode === value ? "bg-white font-medium text-stone-950 shadow-sm" : "text-stone-500 hover:text-stone-900"
                } ${value === "register" && capabilities?.registration_enabled === false ? "cursor-not-allowed opacity-50" : ""}`}
                onClick={() => setMode(value as LoginMode)}
              >
                {label}
              </button>
            ))}
          </div>

          {mode === "key" ? (
            <div className="space-y-3">
              <label htmlFor="auth-key" className="block text-sm font-medium text-stone-700">
                密钥
              </label>
              <Input
                id="auth-key"
                type="password"
                value={authKey}
                onChange={(event) => setAuthKey(event.target.value)}
                placeholder="请输入密钥"
                className="h-13 rounded-2xl border-stone-200 bg-white px-4"
              />
            </div>
          ) : (
            <div className="space-y-4">
              {mode === "register" ? (
                <div className="space-y-2">
                  <label className="block text-sm font-medium text-stone-700">昵称（可选）</label>
                  <Input
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    placeholder="例如：设计同学 A"
                    className="h-13 rounded-2xl border-stone-200 bg-white px-4"
                  />
                </div>
              ) : null}
              <div className="space-y-2">
                <label className="block text-sm font-medium text-stone-700">邮箱</label>
                <Input
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="user@example.com"
                  className="h-13 rounded-2xl border-stone-200 bg-white px-4"
                />
              </div>
              {mode === "reset" ? (
                <div className="space-y-2">
                  <label className="block text-sm font-medium text-stone-700">重置 token</label>
                  <Input
                    value={resetToken}
                    onChange={(event) => setResetToken(event.target.value)}
                    placeholder="先留空提交以申请重置 token"
                    className="h-13 rounded-2xl border-stone-200 bg-white px-4"
                  />
                </div>
              ) : null}
              <div className="space-y-2">
                <label className="block text-sm font-medium text-stone-700">{mode === "reset" ? "新密码" : "密码"}</label>
                <Input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="至少 8 位"
                  className="h-13 rounded-2xl border-stone-200 bg-white px-4"
                />
              </div>
              {mode === "register" || mode === "reset" ? (
                <div className="space-y-2">
                  <label className="block text-sm font-medium text-stone-700">确认密码</label>
                  <Input
                    type="password"
                    value={confirmPassword}
                    onChange={(event) => setConfirmPassword(event.target.value)}
                    placeholder="再次输入密码"
                    className="h-13 rounded-2xl border-stone-200 bg-white px-4"
                  />
                </div>
              ) : null}
              {mode === "reset" && !resetToken.trim() ? (
                <p className="rounded-xl bg-stone-50 p-3 text-xs leading-5 text-stone-500">
                  先输入邮箱并留空 token 提交，系统会发送找回密码邮件；开发环境可能直接返回 token。
                </p>
              ) : null}
              {capabilities?.email_verification_required ? (
                <p className="rounded-xl bg-amber-50 p-3 text-xs leading-5 text-amber-700">
                  当前环境要求邮箱验证后才能登录。验证链接会发送到注册邮箱。
                </p>
              ) : null}
            </div>
          )}

          <Button className="h-13 w-full rounded-2xl bg-stone-950 text-white hover:bg-stone-800" onClick={() => void handleSubmit()} disabled={isSubmitting}>
            {isSubmitting ? <LoaderCircle className="size-4 animate-spin" /> : null}
            {mode === "register" ? (capabilities?.email_verification_required ? "注册并发送验证邮件" : "注册并登录") : mode === "reset" ? (resetToken.trim() ? "重置密码" : "申请重置") : "登录"}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
