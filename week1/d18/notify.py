"""D17：通知渠道。send() 永不抛异常 —— 状态机不能因为网络抖动崩掉。

三个渠道各有明确用途：
    console  人工跑的时候看
    file     追加到 out/alerts.log（一行一条 JSON），验收靠它断言
    webhook  urllib POST JSON，3 秒超时，失败重试 1 次

不引入 requests：为了一个 POST 加依赖，换环境就得再装一次。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_LOG = "out/alerts.log"


def send(
    event: dict,
    *,
    channel: str = "console",
    webhook: str | None = None,
    log_path: str = DEFAULT_LOG,
    timeout: float = 3.0,
) -> tuple[bool, str]:
    """发一条事件，返回 (成功?, 说明)。失败只返回，不抛。"""
    if channel == "console":
        print(f"[通知] {event.get('kind', '?'):<11} {event.get('device', '?'):<20} {event.get('reason', '')}")
        ack_url = event.get("ack_url")
        if ack_url:
            print(f"        确认：{ack_url}")
        return True, "console"

    if channel == "file":
        path = Path(log_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            return False, f"写日志失败 {path}：{exc}"
        return True, str(path)

    if channel == "webhook":
        if not webhook:
            return False, "channel=webhook 但没给 --webhook URL"
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        return _post(webhook, body, timeout)

    return False, f"未知渠道 {channel!r}"


def _post(url: str, body: bytes, timeout: float, retries: int = 1) -> tuple[bool, str]:
    """POST 一次，失败重试 retries 次。只重试 1 次是刻意的：

    渠道挂半小时靠 outbox 下次补发，比在一次运行里死等强；
    重试太多次只会把调度周期拖长，让下一次巡检迟到。
    """
    last = ""
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return True, f"HTTP {getattr(response, 'status', 0)}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(0.5)
    return False, last


def delivery_ok(results: list[tuple[bool, str]]) -> bool:
    """一批发送全部成功才算成功。"""
    return bool(results) and all(ok for ok, _ in results)
