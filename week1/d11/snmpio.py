"""D9 的地基：D8 已经写好的那部分（snmp_get + fmt_ticks），原样搬过来。

这个文件**已经写好了，你不用改**。今天你在它上面盖楼（health.py）。

分层是故意的：
    snmpio.py   负责「怎么把一个包发出去、解回来」   ← 协议层
    health.py   负责「解回来的字符串能不能信」        ← 数据层（你今天写）
"""  # noqa: DOC001

# pyright: reportMissingImports=false
# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

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

    三道门：error_indication（网络层）/ error_status（设备拒绝）/ 空结果，
    外加 NoSuchObject —— 它是「看起来像正常值的错误信息」，必须按类型判。
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

    _, value = var_binds[0]
    if isinstance(value, (NoSuchObject, NoSuchInstance)):
        raise ProbeError(f"{host}:{port} {oid} 该 OID 在设备上不存在")
    return str(value.prettyPrint())


def fmt_ticks(ticks: int) -> str:
    """把 SNMP 的 TimeTicks 转成人能读的时长（单位是 1/100 秒，不是秒）。

    12345678 → "1 day, 10:17:36.78"
    6000     → "00:01:00.00"
    """
    total = ticks / 100
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    clock = f"{int(hours):02d}:{int(minutes):02d}:{seconds:05.2f}"
    return f"{int(days)} day, {clock}" if days else clock
