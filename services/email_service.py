from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
from typing import Mapping
from urllib.parse import quote

from services.config import config


def _clean(value: object) -> str:
    return str(value or "").strip()


def _env_bool(value: object, default: bool = False) -> bool:
    raw = _clean(value)
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class EmailDeliveryResult:
    sent: bool
    provider: str
    message: str = ""

    def as_public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "sent": self.sent,
            "provider": self.provider,
        }
        if self.message:
            payload["message"] = self.message
        return payload


class EmailService:
    """Small provider abstraction for account verification/recovery emails.

    Supported providers:
    - console: writes the email body to stderr; useful for local/dev.
    - smtp: sends through any SMTP service, including SES/QQ/163/Postmark SMTP.
    - resend: sends through Resend's HTTPS API using stdlib urllib.
    """

    def __init__(self, env: Mapping[str, str] | None = None, *, config_obj: object | None = None):
        self.env = env if env is not None else os.environ
        self.config = config_obj if config_obj is not None else config

    def _env(self, name: str, default: str = "") -> str:
        return _clean(self.env.get(name, default))

    @property
    def provider(self) -> str:
        return (self._env("EMAIL_PROVIDER", "console") or "console").lower()

    @property
    def from_email(self) -> str:
        return (
            self._env("EMAIL_FROM")
            or self._env("SMTP_FROM")
            or self._env("RESEND_FROM")
            or "ChatGPT2API <no-reply@example.com>"
        )

    @property
    def explicit_from_email(self) -> str:
        return self._env("EMAIL_FROM") or self._env("SMTP_FROM") or self._env("RESEND_FROM")

    @property
    def public_base_url(self) -> str:
        value = (
            self._env("APP_PUBLIC_URL")
            or self._env("PUBLIC_APP_URL")
            or self._env("CHATGPT2API_BASE_URL")
            or _clean(getattr(self.config, "base_url", ""))
        )
        return value.rstrip("/")

    def action_url(self, path: str, token: str) -> str:
        normalized_path = "/" + path.strip("/")
        query = f"token={quote(_clean(token), safe='')}"
        base = self.public_base_url
        return f"{base}{normalized_path}?{query}" if base else f"{normalized_path}?{query}"

    def status(self) -> dict[str, object]:
        provider = self.provider
        configured, message = self._provider_configured(provider)
        return {
            "provider": provider,
            "configured": configured,
            "message": message,
            "public_base_url_configured": bool(self.public_base_url),
            "from_configured": bool(self.explicit_from_email),
        }

    def is_configured(self) -> bool:
        configured, _ = self._provider_configured(self.provider)
        return configured

    def _provider_configured(self, provider: str) -> tuple[bool, str]:
        if provider == "console":
            return True, "console email logging is enabled"
        if provider == "disabled":
            return False, "email delivery is disabled"
        if provider == "smtp":
            host = self._env("SMTP_HOST")
            sender = self._env("EMAIL_FROM") or self._env("SMTP_FROM")
            if not host or not sender:
                return False, "SMTP_HOST and EMAIL_FROM/SMTP_FROM are required"
            return True, "SMTP sender is configured"
        if provider == "resend":
            api_key = self._env("RESEND_API_KEY") or self._env("EMAIL_API_KEY")
            sender = self._env("EMAIL_FROM") or self._env("RESEND_FROM")
            if not api_key or not sender:
                return False, "RESEND_API_KEY and EMAIL_FROM/RESEND_FROM are required"
            return True, "Resend sender is configured"
        return False, f"unsupported EMAIL_PROVIDER: {provider}"

    def send_email(self, *, to: str, subject: str, text: str, html: str | None = None) -> EmailDeliveryResult:
        recipient = _clean(to).lower()
        if "@" not in recipient:
            raise ValueError("email recipient is invalid")
        provider = self.provider
        if provider == "console":
            self._send_console(to=recipient, subject=subject, text=text, html=html)
            return EmailDeliveryResult(True, provider, "logged to console")
        if provider == "disabled":
            return EmailDeliveryResult(False, provider, "email delivery disabled")
        if provider == "smtp":
            self._send_smtp(to=recipient, subject=subject, text=text, html=html)
            return EmailDeliveryResult(True, provider, "sent via SMTP")
        if provider == "resend":
            self._send_resend(to=recipient, subject=subject, text=text, html=html)
            return EmailDeliveryResult(True, provider, "sent via Resend")
        raise ValueError(f"unsupported EMAIL_PROVIDER: {provider}")

    def _send_console(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        print(
            "\n".join(
                [
                    "[email:console]",
                    f"to: {to}",
                    f"from: {self.from_email}",
                    f"subject: {subject}",
                    "",
                    text,
                    "",
                    html or "",
                ]
            ),
            file=sys.stderr,
        )

    def _build_message(self, *, to: str, subject: str, text: str, html: str | None) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.from_email
        message["To"] = to
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")
        return message

    def _send_smtp(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        host = self._env("SMTP_HOST")
        if not host:
            raise ValueError("SMTP_HOST is required")
        if not (self._env("EMAIL_FROM") or self._env("SMTP_FROM")):
            raise ValueError("EMAIL_FROM or SMTP_FROM is required")
        port = _safe_int(self._env("SMTP_PORT"), 587)
        username = self._env("SMTP_USERNAME")
        password = self._env("SMTP_PASSWORD")
        timeout = _safe_int(self._env("SMTP_TIMEOUT_SECONDS"), 10)
        use_ssl = _env_bool(self._env("SMTP_USE_SSL"), port == 465)
        use_starttls = _env_bool(self._env("SMTP_USE_TLS"), not use_ssl)
        message = self._build_message(to=to, subject=subject, text=text, html=html)

        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as smtp:
                if username or password:
                    smtp.login(username, password)
                smtp.send_message(message)
            return

        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            if use_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if username or password:
                smtp.login(username, password)
            smtp.send_message(message)

    def _send_resend(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        api_key = self._env("RESEND_API_KEY") or self._env("EMAIL_API_KEY")
        if not api_key:
            raise ValueError("RESEND_API_KEY is required")
        if not (self._env("EMAIL_FROM") or self._env("RESEND_FROM")):
            raise ValueError("EMAIL_FROM or RESEND_FROM is required")
        endpoint = self._env("RESEND_API_URL", "https://api.resend.com/emails")
        payload = {
            "from": self.from_email,
            "to": [to],
            "subject": subject,
            "text": text,
        }
        if html:
            payload["html"] = html
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = _safe_int(self._env("EMAIL_HTTP_TIMEOUT_SECONDS"), 10)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured provider endpoint
            status = int(getattr(response, "status", 200))
            if status >= 300:
                body = response.read(4096).decode("utf-8", errors="replace")
                raise RuntimeError(f"Resend API returned HTTP {status}: {body}")

    def send_email_verification(self, *, to: str, token: str, expires_at: str | None = None) -> EmailDeliveryResult:
        link = self.action_url("/verify-email", token)
        subject = self._env("EMAIL_VERIFY_SUBJECT", "验证你的邮箱")
        expires_line = f"\n有效期至：{expires_at}" if expires_at else ""
        text = (
            "你好，\n\n"
            "请点击下面的链接完成邮箱验证：\n"
            f"{link}\n\n"
            "如果链接无法打开，请复制下面的 token 到验证页面：\n"
            f"{token}"
            f"{expires_line}\n\n"
            "如果这不是你的操作，可以忽略这封邮件。"
        )
        html = self._render_action_html(
            title="验证你的邮箱",
            description="点击按钮完成邮箱验证，验证后即可继续登录和使用服务。",
            button_text="完成邮箱验证",
            link=link,
            token=token,
            expires_at=expires_at,
        )
        return self.send_email(to=to, subject=subject, text=text, html=html)

    def send_password_reset(self, *, to: str, token: str, expires_at: str | None = None) -> EmailDeliveryResult:
        link = self.action_url("/reset-password", token)
        subject = self._env("EMAIL_RESET_SUBJECT", "重置你的密码")
        expires_line = f"\n有效期至：{expires_at}" if expires_at else ""
        text = (
            "你好，\n\n"
            "请点击下面的链接重置密码：\n"
            f"{link}\n\n"
            "如果链接无法打开，请复制下面的 token 到重置页面：\n"
            f"{token}"
            f"{expires_line}\n\n"
            "如果这不是你的操作，可以忽略这封邮件。"
        )
        html = self._render_action_html(
            title="重置你的密码",
            description="点击按钮设置新密码。为了账号安全，旧会话会在重置成功后失效。",
            button_text="重置密码",
            link=link,
            token=token,
            expires_at=expires_at,
        )
        return self.send_email(to=to, subject=subject, text=text, html=html)


    def support_ticket_url(self, ticket_id: str) -> str:
        base = self.public_base_url
        path = f"/support?ticket={quote(_clean(ticket_id), safe='')}"
        return f"{base}{path}" if base else path

    def send_support_ticket_update(
        self,
        *,
        to: str,
        ticket_id: str,
        ticket_subject: str,
        update_message: str,
        event: str = "ticket_update",
    ) -> EmailDeliveryResult:
        link = self.support_ticket_url(ticket_id)
        subject_prefix = self._env("SUPPORT_TICKET_EMAIL_SUBJECT_PREFIX", "[Support]")
        subject = f"{subject_prefix} {ticket_subject or ticket_id}"[:180]
        safe_message = _clean(update_message)[:4000]
        text = (
            "Your support ticket has an update.\n\n"
            f"Ticket: {ticket_subject or ticket_id}\n"
            f"Event: {event}\n\n"
            f"{safe_message}\n\n"
            f"Open ticket: {link}\n"
        )
        html = f"""<!doctype html>
<html>
  <body style="margin:0;background:#f5f5f4;padding:24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1c1917;">
    <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:20px;padding:28px;border:1px solid #e7e5e4;">
      <h1 style="font-size:20px;margin:0 0 12px;">Support ticket update</h1>
      <p style="font-size:14px;line-height:1.7;color:#57534e;margin:0 0 12px;"><strong>{escape(ticket_subject or ticket_id)}</strong></p>
      <p style="white-space:pre-wrap;font-size:14px;line-height:1.7;color:#57534e;margin:0 0 20px;">{escape(safe_message)}</p>
      <p style="margin:24px 0;"><a href="{escape(link, quote=True)}" style="display:inline-block;background:#1c1917;color:#ffffff;text-decoration:none;border-radius:12px;padding:12px 18px;font-weight:600;">Open support ticket</a></p>
    </div>
  </body>
</html>"""
        return self.send_email(to=to, subject=subject, text=text, html=html)

    @staticmethod
    def _render_action_html(
        *,
        title: str,
        description: str,
        button_text: str,
        link: str,
        token: str,
        expires_at: str | None,
    ) -> str:
        escaped_link = escape(link, quote=True)
        escaped_token = escape(token)
        expires = f"<p style=\"color:#78716c;font-size:12px;\">有效期至：{escape(expires_at)}</p>" if expires_at else ""
        return f"""<!doctype html>
<html>
  <body style="margin:0;background:#f5f5f4;padding:24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1c1917;">
    <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:20px;padding:28px;border:1px solid #e7e5e4;">
      <h1 style="font-size:22px;margin:0 0 12px;">{escape(title)}</h1>
      <p style="font-size:14px;line-height:1.7;color:#57534e;margin:0 0 20px;">{escape(description)}</p>
      <p style="margin:24px 0;">
        <a href="{escaped_link}" style="display:inline-block;background:#1c1917;color:#ffffff;text-decoration:none;border-radius:12px;padding:12px 18px;font-weight:600;">{escape(button_text)}</a>
      </p>
      <p style="font-size:13px;line-height:1.6;color:#78716c;">如果按钮无法打开，请复制链接：<br><a href="{escaped_link}">{escaped_link}</a></p>
      <p style="font-size:13px;line-height:1.6;color:#78716c;">备用 token：<code style="background:#f5f5f4;border-radius:8px;padding:3px 6px;">{escaped_token}</code></p>
      {expires}
    </div>
  </body>
</html>"""


email_service = EmailService()
