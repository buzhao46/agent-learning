"""D21：值班排班 —— 把「日期 + 班次 → 值班人」查出来，默认填到 ack 链接的 by= 上。

排班表是张纯 JSON，形如：

    {"roster": [
        {"date": "2026-11-09", "shift": "day",   "person": "张三"},
        {"date": "2026-11-09", "shift": "night", "person": "李四"},
        {"date": "2026-11-10", "shift": "day",   "person": "王五"}
    ]}

查询语义：
    - on_duty(roster_path, now, shift=None)
        * shift 不传 → 按当前小时推断（8–19 点算 day，其余 night）
        * 命中「今天 + 当班」那一行 → 返回 person
        * 没配排班表 / 今天没排班 → 返回 None（链接不带 by，回到 D19 行为）
    - by= 只是「谁点的」的预填，不是安全字段，不参与签名；
      点链接的人可以改 by=，服务端照样认（签名只锁 device/group）。

故意不兜底「找不到就填个默认人」：排班表缺失应当让链接退化成 D19 的样子，
而不是默默填一个错的人，误导谁该负责。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def load_roster(path: str | None) -> list[dict]:
    """读排班表；文件不存在 / 不是合法 JSON / 不是数组 → 返回空列表。"""
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[警告] 排班表读取失败，忽略：{exc}")
        return []
    if isinstance(data, dict):
        rows = data.get("roster", [])
    elif isinstance(data, list):
        rows = data
    else:
        print('[警告] 排班表应是 {"roster": [...]} 或数组，已忽略')
        return []
    return [r for r in rows if isinstance(r, dict)]


def default_shift(now: datetime) -> str:
    """按当前小时推断班次：8–19 点算 day，其余 night。"""
    return "day" if 8 <= now.hour < 20 else "night"


def on_duty(path: str | None, now: datetime, shift: str | None = None) -> str | None:
    """查当前值班人；查不到返回 None。"""
    rows = load_roster(path)
    if not rows:
        return None
    shift = shift or default_shift(now)
    date = now.strftime("%Y-%m-%d")
    for row in rows:
        if row.get("date") == date and row.get("shift") == shift:
            person = row.get("person")
            if person:
                return str(person)
    return None
