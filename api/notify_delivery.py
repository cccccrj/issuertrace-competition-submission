"""Actually sending — and the rules that make it safe to have built at all.

Outward channels are the part of a notification feature that can hurt someone.
A misconfigured webhook target is a server-side request forgery; a signing key
in a response body is a leaked credential; a send triggered by a GET is a
cross-site request away from being triggered by anyone. So the constraints are
here, in one file, each one small enough to read and each one with a test:

* **Dry-run opens no socket.**  Not "sends to a sink" — does not call the
  transport at all.  It renders the exact bytes and stores them, so a
  demonstration can show what would go out with nothing going out.
* **Targets must match an allowlist**, defaulting to loopback.  This is what
  stops a subscription from being pointed at an internal address and used to
  probe the network from inside.
* **Secrets are resolved from the environment by name and never returned.**
  ``secret_ref`` names a variable; the value reaches the signature and nothing
  else.
* **Email in dry-run is a real RFC 5322 message**, written to disk. A rendered
  ``.eml`` is inspectable and provable; a f-string that looks like an email is
  neither.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from collector.collect import ROOT

#: Where outward targets are allowed to point. Loopback only unless an operator
#: widens it deliberately, because the safe default for a feature that makes
#: outbound requests is one that cannot reach anything interesting.
DEFAULT_ALLOWLIST = ("http://127.0.0.1:*", "http://localhost:*")
OUTBOX_DIR = ROOT / "data" / "state" / "outbox"
TIMEOUT_SECONDS = 10


class Refused(Exception):
    """A target or configuration the platform will not send to.

    ``str()`` is what a page may show; ``operator_detail`` names the rule and the
    variable behind it and goes only into the delivery record, which the API
    reports as a category (I-06).
    """

    def __init__(self, message: str, operator_detail: str | None = None):
        super().__init__(message)
        self.operator_detail = operator_detail

    def for_operator(self) -> str:
        return f"{self}（{self.operator_detail}）" if self.operator_detail else str(self)


def allowlist() -> tuple[str, ...]:
    raw = os.environ.get("NOTIFY_WEBHOOK_ALLOWLIST", "").strip()
    return tuple(part.strip() for part in raw.split(",") if part.strip()) or DEFAULT_ALLOWLIST


def check_target(url: str | None) -> str:
    """Refuse anything the allowlist does not name, with the rule in the message.

    A refusal that does not say which rule refused is a refusal nobody can fix.
    """
    if not url:
        raise Refused("订阅没有配置目标地址")
    patterns = allowlist()
    if not any(fnmatch.fnmatch(url, pattern) for pattern in patterns):
        # 名单本身是运维配置，不写进会回给页面的提示里（I-06）。
        raise Refused("目标地址不在允许推送的范围内，请联系管理员添加",
                      f"NOTIFY_WEBHOOK_ALLOWLIST={'，'.join(patterns)}：{url}")
    return url


def resolve_secret(secret_ref: str | None) -> str | None:
    """The signing key, by the name of the variable holding it.

    The name travels through the database and the API; the value never does.
    """
    if not secret_ref:
        return None
    value = os.environ.get(secret_ref)
    if not value:
        raise Refused("推送签名密钥未配置，请联系管理员", f"环境变量 {secret_ref} 未设置")
    return value


def sign(body: bytes, secret: str | None) -> dict[str, str]:
    if not secret:
        return {}
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Signature": f"sha256={mac}"}


def render_email(payload: dict[str, Any], to: str) -> EmailMessage:
    """A real message object, so what is stored is what would be sent."""
    message = EmailMessage()
    message["Subject"] = f"[风险预警] {payload.get('summary') or '有新的风险事项'}"
    message["To"] = to
    # 发件人由部署配置给出；默认值不再是 alerts@localhost。
    message["From"] = os.environ.get("NOTIFY_MAIL_FROM", "风险事项提醒 <noreply@example.invalid>")
    reasons = [item.get("label") if isinstance(item, dict) else str(item)
               for item in payload.get("reasons") or []]
    # 邮件正文只用中文标签（I-06）：此前印着公司内部标识、专题码和 high/SUPPORTED。
    lines = [
        payload.get("summary") or "",
        "",
        f"公司：{payload.get('company_name') or '—'}　专题：{payload.get('issue_label') or '—'}",
        f"风险：{payload.get('risk_level_label') or '—'}（{payload.get('risk_score')}）"
        f"　可信度：{payload.get('credibility_text') or '—'}",
        f"时间窗：{(payload.get('window') or {}).get('start')} — {(payload.get('window') or {}).get('end')}",
        f"触发原因：{'、'.join(reason for reason in reasons if reason) or '—'}",
        "",
        "核验依据：",
    ]
    for item in payload.get("evidence") or []:
        lines.append(f"  · {item.get('claim')}［{item.get('verdict_label') or '—'}］"
                     f"{item.get('document') or ''} 第 {item.get('page')} 页")
    message.set_content("\n".join(lines))
    return message


def deliver(subscription: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Send, or render without sending, and report which happened.

    Returns ``{status, http_status?, response?, error?}``. Never raises for a
    transport failure: a refused endpoint is a fact about one delivery, and one
    delivery must not stop the rest of the queue.
    """
    channel = subscription.get("channel")
    dry = bool(subscription.get("dry_run", 1))
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

    try:
        if channel == "inapp":
            # The inbox is written by the caller; there is nothing to transmit.
            return {"status": "dry_run" if dry else "sent", "response": "站内通知"}

        if channel == "email":
            message = render_email(payload, subscription.get("target") or "")
            if dry:
                OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
                name = f"{payload.get('alert_id', 'alert')}-{hashlib.sha256(body).hexdigest()[:8]}.eml"
                (OUTBOX_DIR / name).write_bytes(bytes(message))
                return {"status": "dry_run", "response": f"已渲染 {name}，未发送"}
            raise Refused("SMTP 未配置：本平台默认不发送真实邮件，请用 dry-run 或 webhook 演示")

        if channel == "webhook":
            target = check_target(subscription.get("target"))
            secret = resolve_secret(subscription.get("secret_ref"))
            headers = {"Content-Type": "application/json; charset=utf-8", **sign(body, secret)}
            if dry:
                # No socket is opened. The signature is computed so a reader can
                # verify the receiver would accept it, without anything leaving.
                return {"status": "dry_run",
                        "response": f"已渲染 {len(body)} 字节，签名 {headers.get('X-Signature', '（未配置密钥）')}，未发送"}
            request = urllib.request.Request(target, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return {"status": "sent", "http_status": response.status,
                        "response": response.read(200).decode("utf-8", "replace")}

        raise Refused(f"未知渠道：{channel}")

    except Refused as refusal:
        return {"status": "failed", "error": refusal.for_operator()}
    except urllib.error.HTTPError as failure:
        return {"status": "failed", "http_status": failure.code, "error": str(failure)}
    except Exception as failure:  # transport of any other kind
        return {"status": "failed", "error": f"{type(failure).__name__}: {failure}"[:400]}
