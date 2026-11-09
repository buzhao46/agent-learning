"""D19：ack 链接的签名 / 校验（HMAC），无第三方依赖。

为什么需要签名：ack 链接会被发到聊天 / 邮件里，谁都能点。不签名的话，
任何人都能随便确认任意告警，甚至写个脚本批量确认、把真实故障全「消音」。

做法：链接携带 device（或 group）+ sig，sig 用「密钥 + 设备名」算 HMAC。
服务端用同一密钥复算比对，没有密钥造不出合法的 sig —— 服务端无状态，不用存表。
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse

DEFAULT_SECRET = "change-me-in-prod"


def _sig(secret: str, label: str) -> str:
    return hmac.new(secret.encode("utf-8"), label.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def sign_device(secret: str, label: str) -> str:
    return _sig(secret, label)


def sign_group(secret: str, group: str) -> str:
    return _sig(secret, "group:" + group)


def verify_device(secret: str, label: str, sig: str | None) -> bool:
    if not sig:
        return False
    return hmac.compare_digest(sign_device(secret, label), sig)


def verify_group(secret: str, group: str, sig: str | None) -> bool:
    if not sig:
        return False
    return hmac.compare_digest(sign_group(secret, group), sig)


def device_ack_url(base_url: str, label: str, secret: str) -> str:
    return (
        f"{base_url.rstrip('/')}/ack"
        f"?device={urllib.parse.quote(label)}&sig={sign_device(secret, label)}"
    )


def group_ack_url(base_url: str, group: str, secret: str) -> str:
    return (
        f"{base_url.rstrip('/')}/ack"
        f"?group={urllib.parse.quote(group)}&sig={sign_group(secret, group)}"
    )
