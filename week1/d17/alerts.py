"""D17：告警状态机。纯函数：旧状态 + 本次观测 → (新状态, 事件列表)。

这是今天真正的产出。判据只有一句话：
    **同一个状态只通知一次，状态变化才产生事件。**

与 D16 的 history.decide 的分工：
    decide   回答「这台设备现在坏不坏」（无状态，可以每次都算）
    step     回答「该不该再喊一次」（有状态，靠持久化记住上次喊到哪）

design note（与 README 里的四状态表略有出入，这里是有意的）：
    指引里把 OK / ALERT / STALE / UNKNOWN 画成互斥的四个状态。实际实现把
    **stale 拆成一个独立的布尔字段**，status 仍只保留 OK / ALERT / UNKNOWN。
    原因：「设备坏了」和「监控瞎了」是两个正交的事实，用互斥状态表达会出现
    「进了 STALE 就丢失 ALERT 记录」的问题 —— 数据一恢复，你不知道该不该发
    恢复通知。拆开之后两个事实并存，转换逻辑也不必再记 prior_alert。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

EVENT_OPEN = "alert_open"
EVENT_CLOSE = "alert_close"
EVENT_STALE = "stale"

ST_OK = "OK"
ST_ALERT = "ALERT"
ST_UNKNOWN = "UNKNOWN"


@dataclass
class Observation:
    """状态机的输入：一台设备本次的结论（由 history.Trend 映射而来）。"""

    label: str
    verdict: str  # "ALERT" / "OK" / "UNKNOWN"
    consecutive_bad: int = 0
    consecutive_ok: int = 0
    last_seen: str | None = None


@dataclass
class Event:
    """一条状态跃迁。只在跃迁时产生，所以「重复告警」在源头就不可能发生。"""

    kind: str
    label: str
    old_status: str | None
    new_status: str
    reason: str
    created_at: str

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.label}:{self.created_at}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "device": self.label,
            "old_status": self.old_status,
            "new_status": self.new_status,
            "reason": self.reason,
            "created_at": self.created_at,
            "suppressed": False,
            "attempts": 0,
        }


def observe(trend) -> Observation:
    """Trend → Observation。verdict 的映射只此一处，别在别处再判断一遍。"""
    if trend.status == "ALERT":
        verdict = "ALERT"
    elif trend.status == "OK":
        verdict = "OK"
    else:
        verdict = "UNKNOWN"
    return Observation(
        label=trend.label,
        verdict=verdict,
        consecutive_bad=trend.consecutive_bad,
        consecutive_ok=trend.consecutive_ok,
        last_seen=trend.last_seen,
    )


def _age_minutes(stamp: str | None, now: datetime) -> float | None:
    """last_seen 距今多少分钟。解析不了就返回 None（当作「不知道」，不当作 0）。"""
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return (now - moment).total_seconds() / 60


def _alert_reason(obs: Observation) -> str:
    if obs.consecutive_bad > 0:
        return f"连续 {obs.consecutive_bad} 次非 OK"
    return "非 OK"


def step(
    prev: dict | None,
    obs: Observation,
    *,
    recover: int = 2,
    stale_after: int = 30,
    now: datetime | None = None,
) -> tuple[dict, list[Event]]:
    """旧状态 + 本次观测 → (新状态, 事件列表)。空列表 = 什么都不用发。

    顺序不能换：先判数据新鲜度，再判 verdict。
    一份 3 小时前的「OK」不能证明设备现在好，先把它标成 stale 再谈状态。
    """
    now = now or datetime.now()
    stamp = now.isoformat(timespec="seconds")
    prev = prev or {}
    old = prev.get("status")

    state = {
        "status": old or ST_UNKNOWN,
        "since": prev.get("since", stamp),
        "stale": bool(prev.get("stale", False)),
        "consecutive_bad": obs.consecutive_bad,
        "consecutive_ok": obs.consecutive_ok,
        "last_seen": obs.last_seen or prev.get("last_seen"),
        "last_notified_at": prev.get("last_notified_at"),
    }
    events: list[Event] = []

    # ---- 1. 数据新鲜度（独立于 verdict，只在首次变 stale 时发一条）
    age = _age_minutes(state["last_seen"], now)
    if age is not None and age > stale_after:
        if not state["stale"]:
            state["stale"] = True
            events.append(
                Event(
                    EVENT_STALE,
                    obs.label,
                    old,
                    state["status"],
                    f"{age:.0f} 分钟没有新数据（阈值 {stale_after} 分钟）",
                    stamp,
                )
            )
    elif age is not None:
        state["stale"] = False

    # ---- 2. verdict 跃迁
    if obs.verdict == "ALERT":
        if old != ST_ALERT:
            state["status"] = ST_ALERT
            state["since"] = stamp
            state["last_notified_at"] = stamp
            events.append(Event(EVENT_OPEN, obs.label, old, ST_ALERT, _alert_reason(obs), stamp))
        # 已经是 ALERT：只更新计数，不产生事件 —— 今天所有降噪都靠这一条
    elif obs.verdict == "OK":
        if old == ST_ALERT:
            if obs.consecutive_ok >= recover:
                state["status"] = ST_OK
                state["since"] = stamp
                state["last_notified_at"] = stamp
                events.append(
                    Event(
                        EVENT_CLOSE,
                        obs.label,
                        ST_ALERT,
                        ST_OK,
                        f"连续 {obs.consecutive_ok} 次 OK（阈值 {recover}）",
                        stamp,
                    )
                )
            # 否则维持 ALERT：但 consecutive_ok 已经写进 state，下次会接着往上数。
            # 漏掉这一句，恢复计数每次从 0 开始，永远恢复不了。
        else:
            state["status"] = ST_OK
    # UNKNOWN：状态一律不动。设备失联时最新报告里根本没有它，
    # 若把 UNKNOWN 写回去，下一次抖动就会被当成「恢复」，产生假的 alert_close。

    return state, events


def is_silenced(label: str, now: datetime, silences: list[dict]) -> tuple[bool, str]:
    """静默窗口判定。静默抑制的是**通知**，不是状态。

    silences 形如 [{"device": "10.0.0.9:161" 或 "*", "until": "2026-11-04T02:00:00",
                    "reason": "更换板卡"}]
    until 解析不了就忽略这条（宁可漏静默，也不要永久静默）。
    """
    for item in silences:
        if not isinstance(item, dict):
            continue
        device = str(item.get("device", "*"))
        if device not in ("*", label):
            continue
        try:
            until = datetime.fromisoformat(str(item.get("until", "")))
        except (TypeError, ValueError):
            continue
        if until.tzinfo is not None:
            until = until.astimezone().replace(tzinfo=None)
        if now < until:
            return True, str(item.get("reason") or "静默中")
    return False, ""
