"""D17：告警状态持久化（默认 out/alert_state.json）+ 一个小型 outbox。

为什么必须落盘：
    D16 的 history.decide 是无状态的纯函数 —— 只要连续失败还 >= 阈值，
    每次跑都返回 ALERT。把它的返回值直接接上通知，就是每 5 分钟一条。
    要「只在状态变化时发一次」，就得有个地方记住「上一次是什么状态」。

文件布局：
    {
      "schema": 1,
      "updated_at": "...",
      "devices": {"host:port": {status, since, stale, consecutive_*, last_seen, ...}},
      "pending": [ {事件}, ... ]        ← outbox：没送出去的事件先存着
    }

两个关键决策：
    1. key 用 "host:port" 字符串：JSON 的 key 只能是字符串，tuple 存不进去。
       解析时用 rsplit(":", 1)，别用 split(":") —— IPv6 地址里全是冒号。
    2. 坏文件先备份再初始化，不静默覆盖：那是唯一能复盘「为什么告警没发出来」的现场。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from report import write_json_atomic

SCHEMA_VERSION = 1
MAX_PENDING = 100


def empty_state(now: datetime) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "updated_at": now.isoformat(timespec="seconds"),
        "devices": {},
        "groups": {},
        "pending": [],
    }


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.stem}.bad-{stamp}{path.suffix}")
    try:
        shutil.copy2(path, target)
    except OSError as exc:
        print(f"[警告] 备份损坏的状态文件失败：{exc}")
    return target


def load_state(path: str, *, now: datetime) -> tuple[dict, str]:
    """读状态。返回 (state, 说明)。任何异常都降级成「首次运行」，不抛。"""
    file = Path(path)
    if not file.exists():
        return empty_state(now), "首次运行（状态文件不存在）"

    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        backup = _backup(file)
        return empty_state(now), f"状态文件损坏，已备份为 {backup.name}，本次按首次运行：{exc}"

    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        backup = _backup(file)
        return empty_state(now), f"schema 不认识，已备份为 {backup.name}，本次按首次运行"

    data.setdefault("devices", {})
    data.setdefault("groups", {})
    data.setdefault("pending", [])
    if not isinstance(data["groups"], dict):
        data["groups"] = {}
    if not isinstance(data["devices"], dict):
        data["devices"] = {}
    if not isinstance(data["pending"], list):
        data["pending"] = []
    return data, f"已读取 {len(data['devices'])} 台设备、{len(data['pending'])} 条待发事件"


def save_state(state: dict, path: str, *, now: datetime) -> Path:
    """原子写：tmp + os.replace，不会留半截 JSON。"""
    state["schema"] = SCHEMA_VERSION
    state["updated_at"] = now.isoformat(timespec="seconds")
    return write_json_atomic(Path(path), state)


def load_groups(path: str | None) -> dict:
    """读分组拓扑。文件不存在或坏掉都按「无分组」处理，不抛。"""
    if not path:
        return {"groups": []}
    file = Path(path)
    if not file.exists():
        return {"groups": []}
    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[警告] 分组文件读取失败，按无分组处理：{exc}")
        return {"groups": []}
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return {"groups": []}
    return data


def enqueue(state: dict, event: dict) -> None:
    """事件进 outbox。上限 MAX_PENDING，超出丢最旧的并告警。

    不设上限的话，通知渠道坏一周，状态文件能涨到几十 MB，
    而且下次恢复时会把几百条过期事件一次性糊到人脸上。
    """
    pending = state.setdefault("pending", [])
    pending.append(event)
    if len(pending) > MAX_PENDING:
        dropped = len(pending) - MAX_PENDING
        del pending[:dropped]
        print(f"[警告] 待发事件超过 {MAX_PENDING} 条，丢弃最旧的 {dropped} 条")
