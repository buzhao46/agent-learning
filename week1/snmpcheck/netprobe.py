"""snmpcheck 真采集层（netprobe）。

把 d17 已验证的真 pysnmp 采集逻辑（错误分类 + 真 GET + WALK）合并进 snmpcheck 包，
作为 `agent` 子命令的工具后端。

与同包的 `probe.py`（D7 模拟采集，对着确定性 CPU 公式）区分：本模块是对真实 SNMP agent
（或内置 `fake_agent.py`）发 UDP 报文的真采集，会真的超时、真的拿到接口表。

对外最常用的是三个函数：
    collect_device(host, ...)      采系统组（sysDescr/uptime/sysName）+ 接口数
    collect_interfaces(host, ...)  采整张接口表（名/状态/入字节），按行号合并
    ProbeError / ErrorKind         采集失败的统一异常与七类失败分类
"""

from __future__ import annotations

import asyncio
import logging
import sys
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pysnmp.hlapi import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    getCmd,
    walkCmd,
)
from pysnmp.proto import errind, rfc1905

# ---------------------------------------------------------------- 噪音与事件循环
# pysnmp 的 asyncio 后端在 worker 线程退出时会残留 timeout watchdog task，
# 打印 'Task was destroyed but it is pending'——纯噪音，静音。
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore", category=ResourceWarning)

# Windows 默认 ProactorEventLoop：pysnmp 在解释器退出时会因 loop 已关闭报
# "Event loop is closed"。改用 SelectorEventLoop 规避。必须在任何 asyncio 使用之前设置。
if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ---------------------------------------------------------------- OID 常量
OID_SYSDESCR = "1.3.6.1.2.1.1.1.0"
OID_SYSUPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYSNAME = "1.3.6.1.2.1.1.5.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"  # 接口名，字符串
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"  # 运行状态，整数枚举
OID_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"  # 累计入字节，Counter32

# ifOperStatus 的 RFC 2863 枚举。真实设备七种值都会出现，别当 0/1 处理。
OPER_STATUS = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}


# ---------------------------------------------------------------- 异常
class ProbeError(RuntimeError):
    """采集失败：超时 / 不可达 / 设备拒绝 / OID 不存在，都归到这里。

    消息必须带上「哪台设备、哪个端口、什么原因」，否则报告里只有「采集失败」
    四个字，看的人还得再跑一遍才能定位。
    """


# ---------------------------------------------------------------- 错误分类
class ErrorKind(str, Enum):
    """七种失败类别。继承 str 是为了能直接进 JSON、能直接和字符串比。"""

    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    BAD_OID = "bad_oid"
    TOO_BIG = "too_big"
    DEVICE_ERROR = "device_error"
    NO_DATA = "no_data"
    PROTOCOL = "protocol"

    @property
    def hint(self) -> str:
        """分类的产出必须是一个动作，不是一个名词。

        「超时」不构成信息，「超时：先确认 community → ping → ACL」才是运维能用的东西。
        """
        return {
            ErrorKind.TIMEOUT: "设备没应答。按这个顺序排查：community 对不对 → ping 通不通 → "
            "161/udp 有没有被 ACL 或防火墙拦。SNMPv2c 认证失败也长这样，无法与网络故障区分。",
            ErrorKind.UNREACHABLE: "地址或端口不对：DNS 解析失败、UDP 端口不可达、或设备没起 SNMP 服务。",
            ErrorKind.BAD_OID: "设备不认识这个 OID（noSuchName）。MIB 不支持该节点，或 OID 拼错。",
            ErrorKind.TOO_BIG: "应答超过设备能发的最大报文（tooBig）。一次少请求几个 OID，别硬塞。",
            ErrorKind.DEVICE_ERROR: "设备自己出错了（genErr / noAccess / resourceUnavailable …）。"
            "查设备日志和 ACL，重试通常没用。",
            ErrorKind.NO_DATA: "OID 存在，但这一格没值（NoSuchObject / NoSuchInstance）。"
            "接口刚删、刚创建、或固件少回一行 —— 不是故障，是数据不完整。",
            ErrorKind.PROTOCOL: "报文层面没对上：版本不匹配、对端不是 SNMP 服务、或解析异常。",
        }[self]


@dataclass(frozen=True)
class Failure:
    """一条失败。stage 是「哪个采集项」，不是「哪个 OID」——报告要按项说话。"""

    stage: str
    kind: ErrorKind
    detail: str

    def __str__(self) -> str:
        return f"{self.stage}: {self.kind.value}（{self.detail}）\n      → {self.kind.hint}"


@dataclass
class CollectionResult:
    """一次系统组采集的全部结果。成功和失败分开装，不合并成 Optional。

    合并之后「这项没采」和「这项采到了空值」就分不开了。
    """

    host: str
    port: int = 161
    values: dict[str, Any] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    uptime_ticks: int | None = None

    @property
    def total(self) -> int:
        return len(self.values) + len(self.failures)

    @property
    def verdict(self) -> str:
        """OK / DEGRADED / FAILED 三态。

        只有两种状态不够：一台设备里四项采到三项，巡检系统要把这台单独捞出来，
        而不是混进「失败」里一起重试。
        """
        if not self.failures:
            return "OK"
        if not self.values:
            return "FAILED"
        return "DEGRADED"

    @property
    def exit_code(self) -> int:
        """退出码三态，运维 check 插件老约定：0 正常 / 2 部分 / 1 彻底。"""
        return {"OK": 0, "DEGRADED": 2, "FAILED": 1}[self.verdict]


def classify(
    indication: object | None,
    status: object | None,
    value: object | None,
) -> ErrorKind | None:
    """三层错误字段 → 一个类别。返回 None 表示「这一次没出错」。

    pysnmp **不抛异常**，把三个不同层次的结果放进返回四元组：
        (error_indication, error_status, error_index, var_binds)
        indication  协议栈错误（异常对象，被 return 出来）—— RequestTimedOut 等
        status      设备在 PDU 的 error-status 格写的判决 —— 整数
        value       varbind 的值 —— 可能是 NoSuchObject / NoSuchInstance 伪装成的「值」

    判据（按优先级）：
        1. indication 不为空 → TIMEOUT / PROTOCOL(EmptyResponse) / UNREACHABLE
        2. status 不为空且 int(status) != 0 → TOO_BIG / BAD_OID / DEVICE_ERROR
        3. value 是 NoSuchObject/NoSuchInstance → NO_DATA
        4. 三个都空 → None

    ⚠️ pysnmp 6.1.4 实测：无错时 status 是原生 int(0)，出错时是 Integer。
        只能 `int(status) != 0` 判，不能无条件 `.prettyPrint()`。
    """
    if indication is not None:
        if isinstance(indication, errind.RequestTimedOut):
            return ErrorKind.TIMEOUT
        if isinstance(indication, errind.EmptyResponse):
            return ErrorKind.PROTOCOL
        return ErrorKind.UNREACHABLE
    if isinstance(status, int) and status != 0:
        code = status
        if code == 1:
            return ErrorKind.TOO_BIG
        if code == 2:
            return ErrorKind.BAD_OID
        return ErrorKind.DEVICE_ERROR
    if isinstance(value, (rfc1905.NoSuchObject, rfc1905.NoSuchInstance)):
        return ErrorKind.NO_DATA
    return None


def fmt_ticks(ticks: int) -> str:
    """把 SNMP 的 TimeTicks 转成人能读的时长（单位是 1/100 秒，不是秒）。"""
    total = ticks / 100
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    clock = f"{int(hours):02d}:{int(minutes):02d}:{seconds:05.2f}"
    return f"{int(days)} day, {clock}" if days else clock


def _value_text(value: object) -> str:
    """把 pysnmp 的 ASN.1 值对象（OctetString / TimeTicks …）转成文本。

    OctetString 用 asOctets().decode（中文接口名若用 prettyPrint 会变十六进制）；
    其它类型用 prettyPrint。下游（工具层 / JSON 序列化）统一拿 str。
    """
    from pysnmp.proto.rfc1902 import OctetString

    if isinstance(value, OctetString):
        return value.asOctets().decode("utf-8", "replace")
    # pysnmp 无类型 stub：非 OctetString 的 ASN.1 值用 prettyPrint 取可读文本
    return value.prettyPrint()  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 单次 GET
def get_raw(
    host: str,
    oid: str,
    *,
    community: str = "public",
    port: int = 161,
    timeout: float = 1.0,
) -> tuple[str | None, Failure | None]:
    """发一次 GET。**永不抛异常**：成功给 (值, None)，失败给 (None, Failure)。

    返回二元组而非抛异常是故意的：调用方要在循环里采多个项目，任何一个失败都不能
    中断后面的。失败证据（Failure）必须被放进某个筐里，而不是被 `except: pass` 吞掉。

    ⚠️ 每次 GET 都新建 SnmpEngine（实测复用同一个 engine 做多次 GET 在不同上下文会卡）。
    """
    indication, status, _index, var_binds = getCmd(
        SnmpEngine(),
        CommunityData(community),
        UdpTransportTarget((host, port), timeout=timeout, retries=0),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
    )
    value = var_binds[0][1] if var_binds else None
    kind = classify(indication, status, value)
    if kind is not None:
        if indication is not None:
            detail = str(indication)
        elif status is not None and int(status) != 0:
            detail = f"{status.prettyPrint()}({int(status)})"
        elif value is not None:
            detail = type(value).__name__
        else:
            detail = "空响应：var_binds 是空的"
        return None, Failure(stage=oid, kind=kind, detail=detail)
    if value is None:
        return None, None
    # 把 pysnmp 值对象转成文本，保证下游（工具层 / JSON 序列化）拿到的是 str
    return _value_text(value), None


# ---------------------------------------------------------------- WALK
def walk(
    host: str,
    base_oid: str,
    *,
    community: str = "public",
    port: int = 161,
    timeout: float = 1.0,
    max_rows: int = 200,
) -> list[tuple[str, str]]:
    """从 base_oid 往下走，返回 [(完整 OID, 文本值), ...]。

    关键选项 lexicographicMode=False：走到离开 base_oid 子树就停，否则会一路走到整棵
    MIB 末尾。max_rows 是防「设备回一个不递增的 OID 导致原地打转」的保险。

    出错怎么办（设计决策）：
        第一行还没拿到就出错 → 抛 ProbeError（设备级失败）
        已经拿到若干行后出错 → 打印 warning 就停，把已有的行返回（半张表也是数据）

    ⚠️ 每次 WALK 都要自己的 SnmpEngine。复用同一个 engine 跑第二次 walkCmd 会**永久卡死**。
    """
    from pysnmp.proto.rfc1902 import OctetString

    rows: list[tuple[str, str]] = []
    engine = SnmpEngine()
    target = UdpTransportTarget((host, port), timeout=timeout, retries=0)
    obj = ObjectType(ObjectIdentity(base_oid))
    iterator = walkCmd(
        engine,
        CommunityData(community),
        target,
        ContextData(),
        obj,
        lexicographicMode=False,
    )
    for error_indication, error_status, _error_index, var_binds in iterator:
        if error_indication or error_status:
            reason = error_indication or error_status
            if not rows:
                raise ProbeError(f"{host}:{port} {base_oid} 采集失败：{reason}")
            logging.getLogger(__name__).warning(
                "%s:%s %s 读到第 %d 行后中断：%s", host, port, base_oid, len(rows), reason
            )
            break
        if len(rows) >= max_rows:
            break
        for name, value in var_binds:
            text = (
                value.asOctets().decode("utf-8", "replace")
                if isinstance(value, OctetString)
                else value.prettyPrint()
            )
            rows.append((str(name), text))
    return rows


def _if_index(oid: str, base: str) -> int:
    """从 "1.3.6.1.2.1.2.2.1.2.3" 取出行号 3（base 是 "1.3.6.1.2.1.2.2.1.2"）。"""
    tail = oid[len(base) + 1 :]
    return int(tail.split(".")[0])


def collect_interfaces(
    host: str,
    *,
    community: str = "public",
    port: int = 161,
    timeout: float = 1.0,
    max_rows: int = 200,
) -> list[dict[str, Any]]:
    """采整张接口表，返回按 index 排序的 [{index, name, status, in_octets}, ...]。

    三列（descr/oper/in_octets）按**行号字典**合并，绝不 zip——真实设备少回一行时
    zip 会从那一行起全部错位，而且错得很安静。
    """
    descr = walk(host, OID_IF_DESCR, community=community, port=port, timeout=timeout, max_rows=max_rows)
    status_rows = walk(host, OID_IF_OPER, community=community, port=port, timeout=timeout, max_rows=max_rows)
    octets = walk(
        host, OID_IF_IN_OCTETS, community=community, port=port, timeout=timeout, max_rows=max_rows
    )
    name_by_index = {_if_index(oid, OID_IF_DESCR): text for oid, text in descr}
    status_by_index = {
        _if_index(oid, OID_IF_OPER): OPER_STATUS.get(int(text), f"未知({text})")
        for oid, text in status_rows
    }
    octets_by_index = {_if_index(oid, OID_IF_IN_OCTETS): int(text) for oid, text in octets}
    return [
        {
            "index": index,
            "name": name,
            "status": status_by_index.get(index, "未知"),
            "in_octets": octets_by_index.get(index),
        }
        for index, name in sorted(name_by_index.items())
    ]


# ---------------------------------------------------------------- 系统组采集
def collect_device(
    host: str,
    *,
    community: str = "public",
    port: int = 161,
    timeout: float = 1.0,
) -> CollectionResult:
    """采一台设备的系统组（sysDescr / uptime / sysName）+ 接口数量。

    返回 CollectionResult：成功进 values，失败进 failures。verdict / exit_code 由 property 算。
    """
    log = logging.getLogger(__name__)
    result = CollectionResult(host=host, port=port)

    for stage, oid in (
        ("sysDescr", OID_SYSDESCR),
        ("uptime", OID_SYSUPTIME),
        ("sysName", OID_SYSNAME),
    ):
        text, failure = get_raw(host, oid, community=community, port=port, timeout=timeout)
        if failure is None:
            result.values[stage] = text
        else:
            result.failures.append(
                Failure(stage=stage, kind=failure.kind, detail=failure.detail)
            )
    try:
        rows = walk(host, OID_IF_DESCR, community=community, port=port, timeout=timeout)
        result.values["ifTable"] = len(rows)
    except ProbeError as exc:
        result.failures.append(Failure(stage="ifTable", kind=ErrorKind.PROTOCOL, detail=str(exc)))

    raw_uptime = result.values.get("uptime")
    if raw_uptime is not None:
        try:
            result.uptime_ticks = int(raw_uptime)
            result.values["uptime"] = fmt_ticks(result.uptime_ticks)
        except (TypeError, ValueError):
            pass  # 脏值（如 "N/A"）留 None，上层按缺值处理

    log.debug("%s:%d %s，%d/%d 项成功", host, port, result.verdict, len(result.values), result.total)
    return result
