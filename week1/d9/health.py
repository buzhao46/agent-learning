"""D9 · 把采集结果装进 dataclass，并且只让「可信的数字」进去。

    python health.py --port 1161        # 正常设备
    python health.py --port 1162        # 脏 uptime 那台（fake_agent.py --uptime-bad）

四个 TODO，约 45 行：
    1. safe_int      把 SNMP 回的字符串变成「可信的整数」，或者 None
    2. DeviceHealth  uptime_text / to_dict
    3. build_health  纯函数：三个原始字符串 → DeviceHealth（不碰网络）
    4. probe_health  发三个包 → build_health（返回 DeviceHealth，不再是 dict）

今天唯一的新知识是「脏值」：设备回 ""、回 "-1"、回 "4294967295"、
回 "N/A"，意思全都是「我没这个数据」。int() 会把它们变成 0、-1、
4294967295 —— 每一个看起来都完全正常，于是进了报告、进了告警。
所以数字不能裸着用，必须先过 safe_int。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field

from snmpio import OID_SYSDESCR, OID_SYSNAME, OID_SYSUPTIME, ProbeError, fmt_ticks, snmp_get

# 设备固件里两个经典的「我没数据」常量：
#   2147483647 = 2**31 - 1（有符号 32 位上限）
#   4294967295 = 2**32 - 1（Counter32 上限）
# 它们不是真数据，但长得比真数据还真。
SENTINELS = frozenset({2147483647, 4294967295})

# 只放行「可选负号 + 纯数字」。白名单，不靠 try/except。
_INT_RE = re.compile(r"-?\d+")


def safe_int(
    value: object,
    *,
    default: int | None = None,
    allow_negative: bool = False,
) -> int | None:
    """把 SNMP 回的值变成一个可信的整数；不可信就返回 default（默认 None）。

    value          设备回的东西：字符串、int、或者任何乱七八糟的类型
    default        不可信时返回什么。**默认 None，不是 0** —— 0 是真值
                   （uptime=0 表示刚重启），拿它表示「没采到」等于撒谎
    allow_negative 是否放行负数。默认不放行：SNMP 计数器是无符号的，
                   负数只可能是「不支持」的哨兵（-1）。但温度 MIB 会回 -5，
                   那种调用点要显式打开这个开关

    为什么不用 `try: int(v) except ValueError`：
        那样会把 "1_000" 变成 1000（Python 认下划线）、把 "+42" 变成 42。
        解析外部数据要白名单，只放行你确认过的形状。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip() # 1. 去首尾空白
        if not re.fullmatch(r"-?\d+", text):
            return default
        number = int(text)

    else:
        return default
    
    if number < 0 and not allow_negative:
        return default
    if number in SENTINELS:
        return default

    return number


@dataclass(frozen=True)
class DeviceHealth:
    """一台设备的一次采集快照。**frozen**：采到什么就是什么，不许随手改。

    要改请用 dataclasses.replace(h, ...) —— 返回新对象，原对象不动。
    """

    host: str
    sys_name: str
    sys_descr: str
    uptime_ticks: int | None = None  # None = 没采到 / 采到的不可信，不是 0
    collected_at: float = field(default_factory=time.time)

    @property
    def uptime_text(self) -> str:
        """人能读的开机时长；采不到时是「未知」，不是「00:00:00.00」。"""

        if self.uptime_ticks is None:
            return "未知"
        return fmt_ticks(self.uptime_ticks)

    def to_dict(self) -> dict[str, object]:
        """给 D11 落盘用的行：原始 ticks 和格式化文本都要留着。

        只留文本的话，将来没法排序/筛选；只留 ticks 的话，报告里没法直接看。
        """
        return {
            "host": self.host,
            "sys_name": self.sys_name,
            "sys_descr": self.sys_descr,
            "uptime_ticks": self.uptime_ticks,
            "uptime_text": self.uptime_text,
        }


def build_health(host: str, raw: dict[str, str]) -> DeviceHealth:
    """把「三个 OID 的原始字符串」组装成 DeviceHealth。**纯函数，不碰网络。**

    raw 的键：sys_descr / sys_name / sys_uptime

    为什么单独拆一个函数：解析逻辑从此可以脱离设备测试——
    想验证「设备回 N/A 时怎么办」，不需要真有一台设备回 N/A，
    直接 build_health("x", {"sys_uptime": "N/A"}) 就行。
    """
    return DeviceHealth(
        host=host,
          sys_name=raw.get("sys_name", "").strip(),
        sys_descr=raw.get("sys_descr", "").strip(),
        uptime_ticks=safe_int(raw.get("sys_uptime")),
    )


def probe_health(
    host: str,
    *,
    community: str = "public",
    port: int = 161,
    timeout: float = 1.0,
) -> DeviceHealth:
    """发三个 GET，返回 DeviceHealth（不再是 dict）。

    ★ 一个今天必须自己做的决定：
      sysUpTime 采不到时，是让整台设备失败，还是只让这一个字段为空？

      选后者。整机失败是「设备级」判断（不可达 / community 错），
      字段缺失是「字段级」问题，不该混为一谈。所以只给 uptime 兜一层，
      sys_descr / sys_name 采不到一定抛。

      注意别写 `try: 三个都采 except: pass` —— 那会让「设备根本连不上」
      也变成一个空壳 DeviceHealth，是最难查的一类 bug。
    """
    raw = {
        "sys_descr": snmp_get(host, OID_SYSDESCR, community=community, port=port, timeout=timeout),
        "sys_name": snmp_get(host, OID_SYSNAME, community=community, port=port, timeout=timeout),
    }
    try:
        raw["sys_uptime"] = snmp_get(host, OID_SYSUPTIME, community=community, port=port, timeout=timeout)
    except ProbeError:
        raw["sys_uptime"] = "" 

    return build_health(host, raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="读一台设备的系统组，输出 DeviceHealth")
    parser.add_argument("--host", default="127.0.0.1", help="目标 IP")
    parser.add_argument("--port", type=int, default=161, help="目标端口（假设备用 1161）")
    parser.add_argument("--community", default="public", help="SNMPv2c 团体名")
    parser.add_argument("--timeout", type=float, default=1.0, help="单次超时（秒）")
    args = parser.parse_args()

    try:
        health = probe_health(
            args.host,
            community=args.community,
            port=args.port,
            timeout=args.timeout,
        )
    except ProbeError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1

    print(f"设备 {health.host}")
    print(f"  型号    {health.sys_descr}")
    print(f"  名称    {health.sys_name}")
    print(f"  运行    {health.uptime_text}  (ticks={health.uptime_ticks})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
