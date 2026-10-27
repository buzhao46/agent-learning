"""D8 · 第一次真采集：用 pysnmp 从设备读回 sysDescr / sysName / sysUpTime。

    python getprobe.py --port 1161
    python getprobe.py --host 10.0.0.1 --community public     # 真机

三个 TODO 在下面，全部写完大约 40 行。

**先用两分钟看懂「返回值长什么样」**，这是今天唯一的新知识：

    error_indication, error_status, error_index, var_binds = getCmd(...)

    - error_indication  网络层失败（超时、DNS 失败）。正常时是 None
    - error_status      设备明确拒绝（如 OID 不存在、权限不足）。正常时是 0
    - error_index       出问题的是第几个 varbind（从 1 开始，不是 0）
    - var_binds         成功时的结果，形状是 [(OID, 值), ...]

四个都要看。**只看第一个就会漏掉后两个**，这是 SNMP 代码最常见的 bug。
"""

from __future__ import annotations

import argparse
import sys

from pysnmp.hlapi import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    getCmd,
)
from pysnmp.proto.rfc1905 import NoSuchInstance, NoSuchObject

# 标准 MIB-II 系统组，这三条几乎所有设备都支持
OID_SYSDESCR = "1.3.6.1.2.1.1.1.0"  # 设备型号 / 固件版本，字符串
OID_SYSUPTIME = "1.3.6.1.2.1.1.3.0"  # 开机时长，单位是 1/100 秒
OID_SYSNAME = "1.3.6.1.2.1.1.5.0"  # 设备主机名，字符串

# SnmpEngine 内部管着 socket 和一堆状态，建一次就够，别每发一个请求建一个
_ENGINE = SnmpEngine()


class ProbeError(RuntimeError):
    """采集失败：超时、连不上、设备拒绝、OID 不存在，都归到这里。

    消息里必须带上是哪台设备、什么原因——巡检报告里「core-sw-01 采集超时」
    和「采集失败」是天壤之别，前者能直接定位，后者只能再查一遍。
    """


def snmp_get(
    host: str,
    oid: str,
    *,
    community: str = "public",
    port: int = 161,
    timeout: float = 1.0,
) -> str:
    """发一次 GET，返回值的字符串形式。

    host      目标 IP，字符串（"10.0.0.1"）
    oid       要读的 OID，点分字符串
    community SNMPv2c 的团体名，默认 "public"
    port      目标端口，真机是 161；本地假设备用 1161，所以要能改
    timeout   等多久算超时（秒）

    要做的事：
      1. 调 getCmd(...)，参数顺序是 (engine, CommunityData, UdpTransportTarget,
         ContextData, ObjectType(ObjectIdentity(oid)))
         - CommunityData(community)      v2c 直接传团体名就行
         - UdpTransportTarget((host, port), timeout=timeout, retries=0)
         - retries=0 很重要：默认是 5，意味着超时会等 6 倍时间，
           测试里看不出来，真机上会让人以为程序卡死
      2. 拿到四个返回值，逐个判空：
         - error_indication 不是 None → 网络层失败
         - error_status 不是 0（或不是假值）→ 设备拒绝
         - var_binds 是空的 → 也要报错，别返回空字符串
         注意 error_status 可能是 int，也可能是 Integer32，
         所以别写 error_status.prettyPrint()，用 str(error_status) 或 f-string 拼
      3. 结果形如 [(OID, 值)]，遍历它，把「值」用 .prettyPrint() 变成字符串
      4. 所有失败路径都要 raise ProbeError，消息里带上 host、port 和原因

    ★ 陷阱：不存在的 OID，设备回的 error_status 是 0（一切正常），
      但 varbind 里的值不是字符串，而是一个叫 NoSuchObject 的「特殊标记」。
      它的 prettyPrint() 会返回 "No Such Object currently exists at this OID"。
      如果直接把它当正常值返回，你就把一句英文错误信息当成设备型号存进了数据库。
      判据：值对象的类型。可以 from pysnmp.proto.rfc1905 import NoSuchObject, NoSuchInstance
      然后 isinstance 判断；也可以偷懒看 type(值).__name__。
    """
    try:
        error_indication, error_status, error_index, var_binds = getCmd(
            _ENGINE,
            CommunityData(community),
            UdpTransportTarget((host, port), timeout=timeout, retries=0),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
    except Exception as exc:
        raise ProbeError(f"{host}:{port} {oid} 请求异常：{exc}") from exc
    
    if error_indication:
        raise ProbeError(f"{host}:{port} {oid} 网络层失败：{error_indication}")
    if error_status:
        raise ProbeError(f"{host}:{port} {oid} 设备拒绝: {error_status}")
    if not var_binds:
        raise ProbeError(f"{host}:{port} {oid} 空响应: {var_binds}")

    __, value = var_binds[0]
    if isinstance(value, (NoSuchObject, NoSuchInstance)):
        raise ProbeError(f"{host}:{port} {oid} 该 OID 在设备上不存在")
    return str(value.prettyPrint())


def fmt_ticks(ticks: int) -> str:
    """把 SNMP 的 TimeTicks 转成人能读的时长。

    ★ 今天第二个坑：sysUpTime 的单位**不是秒**，是「百分之一秒」。
      12345678 不是 1.4 亿秒，而是 123456.78 秒（约 34 小时）。
      忘掉这个 /100，所有设备的开机时长都会大 100 倍——而且看起来还挺像真的。

    约定输出格式（check 会逐字符比对）：
      ticks=12345678 → "1 day, 10:17:36.78"
      ticks=6000     → "00:01:00.00"

      - 秒的部分保留两位小数
      - 有整天时，天数单独放前面加 "day, "，后面时:分:秒都用两位补零
      - 不足一天时不写天数，直接 时:分:秒
    """
    total = ticks / 100
    days, rest = divmod(total, 24 * 60 * 60)
    hours, rest = divmod(rest, 60 * 60)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{int(days)} day, {int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{ticks % 100:02d}"
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{ticks % 100:02d}"


def probe_health(
    host: str,
    *,
    community: str = "public",
    port: int = 161,
    timeout: float = 1.0,
) -> dict[str, str]:
    """读三样东西，打包成一个字典返回。

    返回的键固定是这三个（值为字符串，都已经格式化好）：
        {"sys_descr": ..., "sys_name": ..., "uptime": ...}

    uptime 必须是 fmt_ticks 处理过的人类可读形式，不是原始数字。
    任何一步失败都让 ProbeError 往上抛，不要 try/except 吞掉——
    错误分类是 D12 的事，今天先让它响亮地失败。
    """
    def get(oid: str) -> str:
        return snmp_get(host, oid, community=community, port=port, timeout=timeout)
    
    return {
        "sys_descr": get(OID_SYSDESCR),
        "sys_name": get(OID_SYSNAME),
        "uptime": fmt_ticks(int(get(OID_SYSUPTIME))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="读一台设备的 MIB-II 系统组")
    parser.add_argument("--host", default="127.0.0.1", help="目标 IP")
    parser.add_argument("--port", type=int, default=161, help="目标端口（假设备用 1161）")
    parser.add_argument("--community", default="public", help="SNMPv2c 团体名")
    parser.add_argument("--timeout", type=float, default=1.0, help="单次超时（秒）")
    args = parser.parse_args()

    target = f"{args.host}:{args.port}"
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

    print(f"设备 {target}")
    print(f"  型号  {health['sys_descr']}")
    print(f"  名称  {health['sys_name']}")
    print(f"  运行  {health['uptime']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
