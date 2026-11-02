"""D12 · 错误分类：把「失败了」变成程序能处理的数据。

    python probe.py --port 1161                    # 正常设备
    python probe.py --port 1162 --community bad    # community 错（全超时）
    python probe.py --port 1163 --gen-err          # 设备在 sysDescr 上回错误码

D8 到今天，失败一律是这一个样子：

    raise ProbeError(f"{host}:{port} {oid} 采集失败：{reason}")

它对人够用，对程序不够用。上层想知道「要不要重试」「要不要换 community」
「要不要报设备故障」，只能去 `if "超时" in str(exc)` —— 改一个措辞，判定全崩。
今天要把它换成**枚举 + 结构体**。

三个 TODO，约 55 行：
    1. classify        三层错误字段 → 一个 ErrorKind
    2. get_raw         发一次 GET，永不抛异常，失败也给数据
    3. collect_device  采 4 个项目，任何一项失败都不影响其它项

已经写好的（不用改）：
    ErrorKind          7 个类别 + 每个类别的「下一步该干什么」
    Failure            一条失败：发生在哪、什么类别、原始原因
    CollectionResult   一次采集的全部结果（成功 + 失败）
    verdict/exit_code  三态判定 → 0 / 1 / 2
    render / main      打印和 CLI

今天真正要理解的一件事，比三个 TODO 都重要：

    **SNMPv2c 没有「认证失败」这个错误。**

    community 写错，设备不是回一个「认证错误」，而是**直接把这个包丢掉**。
    客户端看到的和「网线拔了」一模一样：超时。
    所以分类里没有 AUTH 这一项 —— 不是我们偷懒，是协议这么定的。
    （SNMPv3 有 usmStatsWrongDigests，那是另一个故事。）

    推论：报告里写「超时」时，原因可能是网络、可能是 community、也可能是
    ACL 拦了 161/udp。所以 ErrorKind.TIMEOUT 的 hint 必须把三种可能都列出来，
    让看报告的人知道下一步该试什么 —— 这是分类的意义：
    **不是把错误命名，是给出下一步动作。**
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# pysnmp 的 asyncio 后端在 worker 线程的 loop 关闭时会残留 timeout watchdog task，
# 打印 'Task was destroyed but it is pending'——纯噪音，与采集结果无关，静音。
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore", category=ResourceWarning)

# Windows 默认用 ProactorEventLoop：pysnmp 6.x 的 asyncio transport 在解释器退出时，
# 会因 loop 已关闭而报 "RuntimeError: Event loop is closed"（纯退出噪音，不影响采集）。
# 改用 SelectorEventLoop 规避。
if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from pysnmp.hlapi import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    getCmd,
)
from pysnmp.proto import errind, rfc1905
from pysnmp.proto.rfc1902 import OctetString

from ifwalk import OID_IF_DESCR, walk
from snmpio import ProbeError, fmt_ticks

OID_SYSDESCR = "1.3.6.1.2.1.1.1.0"
OID_SYSUPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYSNAME = "1.3.6.1.2.1.1.5.0"


# ---------------------------------------------------------------- 已写好
class ErrorKind(str, Enum):
    """七种失败类别。继承 str 是为了能直接和字符串比、能直接进 JSON。"""

    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    BAD_OID = "bad_oid"
    TOO_BIG = "too_big"
    DEVICE_ERROR = "device_error"
    NO_DATA = "no_data"
    PROTOCOL = "protocol"

    @property
    def hint(self) -> str:
        """这一类失败，下一步该干什么。

        这是整个 D12 的核心：**分类的产出必须是一个动作，不是一个名词。**
        「超时」不构成信息，「超时：先确认 community，再 ping，最后查 ACL」
        才是运维能用的东西。
        """
        return {
            ErrorKind.TIMEOUT: "设备没应答。按这个顺序排查：community 对不对 → ping 通不通 → "
            "161/udp 有没有被 ACL 或防火墙拦。SNMPv2c 认证失败也长这样，无法与网络故障区分。",
            ErrorKind.UNREACHABLE: "地址或端口不对：DNS 解析失败、UDP 端口不可达、或设备没起 SNMP 服务。",
            ErrorKind.BAD_OID: "设备不认识这个 OID（noSuchName）。MIB 不支持该节点，或者 OID 拼错了。",
            ErrorKind.TOO_BIG: "应答超过设备能发的最大报文（tooBig）。一次少请求几个 OID，别硬塞。",
            ErrorKind.DEVICE_ERROR: "设备自己出错了（genErr / noAccess / resourceUnavailable …）。"
            "查设备日志和 ACL 配置，重试通常没用。",
            ErrorKind.NO_DATA: "OID 存在，但这一格没有值（NoSuchObject / NoSuchInstance）。"
            "接口刚删、刚创建、或固件少回一行 —— 不是设备故障，是数据不完整。",
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
    """一次采集的全部结果。

    成功和失败**分开装**，不合并成一个 Optional：
    合并之后「这项没采」和「这项采到了空值」就分不开了 —— D11 在 CSV 上
    讨论过同一个问题（None 不能写成 "-"），这里是它在内存里的版本。
    """

    host: str
    values: dict[str, Any] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.values) + len(self.failures)

    @property
    def verdict(self) -> str:
        """OK / DEGRADED / FAILED 三态。

        只有两种状态是不够的：五台设备里四台正常、一台只采到一半，
        巡检系统要能把这台单独捞出来看，而不是混在「失败」里一起重试。
        """
        if not self.failures:
            return "OK"
        if not self.values:
            return "FAILED"
        return "DEGRADED"

    @property
    def exit_code(self) -> int:
        """退出码三态，用的是运维 check 插件的老约定：

            0  一切正常      → nagios/monitor 不动作
            2  部分成功      → 发告警，别自动重启
            1  彻底失败      → 可能整机掉线，走升级流程

        0/1 之外的第三个值必须有，否则「一半坏了」和「全坏了」在自动化里
        触发同一套动作 —— 这是把人工判断塞进脚本时最常踩的坑。
        """
        return {"OK": 0, "DEGRADED": 2, "FAILED": 1}[self.verdict]


# ---------------------------------------------------------------- TODO 1
def classify(
    indication: object | None,
    status: object | None,
    value: object | None,
) -> ErrorKind | None:
    """三层错误字段 → 一个类别。返回 None 表示「这一次没出错」。

    为什么参数是三个而不是一个异常：
        pysnmp **不抛异常**。它把三个不同层次的结果同时放进返回值的四元组：

            (error_indication, error_status, error_index, var_binds)

            indication  协议栈自己的错误 —— 异常**对象**，但是被 return 出来的，
                        不是 raise 出来的。典型值：errind.RequestTimedOut
            status      设备在 PDU 的 error-status 格里写的判决 —— 一个整数。
                        0 表示没问题，1 = tooBig，2 = noSuchName，5 = genErr
            value       varbind 的值 —— 可能是 NoSuchObject / NoSuchInstance
                        这两个**对象**伪装成的「值」

        三层各自的类型完全不同：一个异常对象、一个整数、一个伪装成值的对象。
        所以判据也完全不同 —— 这就是为什么字符串匹配必然出错。

    判据（按这个优先级往下走，先命中的返回）：

        1. indication 不为空：
               isinstance(indication, errind.RequestTimedOut)  → TIMEOUT
               isinstance(indication, errind.EmptyResponse)   → PROTOCOL
               其它 ErrorIndication                            → UNREACHABLE
        2. status 不为空且 int(status) != 0：
               int(status) == 1  → TOO_BIG        (tooBig)
               int(status) == 2  → BAD_OID        (noSuchName)
               其它               → DEVICE_ERROR   (5 genErr / 6 noAccess / …)
        3. value 是 NoSuchObject 或 NoSuchInstance 的实例 → NO_DATA
        4. 三个都空 → None

    注意第 1 步里 EmptyResponse 要单独拎出来：它是「包收到了、里面是空的」，
    属于协议层问题，和「一个字节都没收到」的超时不是一回事。
    漏掉这一条，一台被端口探测工具扫过的设备会被判成 UNREACHABLE。

    ⚠️ 绝对不能这么写：

            if "超时" in str(indication):  kind = TIMEOUT     # 错
            if "timeout" in str(indication): kind = TIMEOUT   # 也错

        pysnmp 的原话是 "No SNMP response received before timeout" ——
        既没有「超时」也没有 "timeout"（有 timeout，但大小写/措辞随版本变）。
        而按类型判断永远不会因为文案改动而失效。
    """
    if indication is not None:
        if isinstance(indication, errind.RequestTimedOut):
            return ErrorKind.TIMEOUT
        if isinstance(indication, errind.EmptyResponse):
            return ErrorKind.PROTOCOL
        return ErrorKind.UNREACHABLE
    if status is not None and int(status) != 0:
        code = int(status)
        if code == 1:
            return ErrorKind.TOO_BIG
        if code == 2:
            return ErrorKind.BAD_OID
        return ErrorKind.DEVICE_ERROR
    if isinstance(value, (rfc1905.NoSuchObject, rfc1905.NoSuchInstance)):
        return ErrorKind.NO_DATA

    return None


# ---------------------------------------------------------------- TODO 2
def get_raw(
    host: str,
    oid: str,
    *,
    community: str = "public",
    port: int = 161,
    timeout: float = 1.0,
) -> tuple[str | None, Failure | None]:
    """发一次 GET。**永不抛异常**：成功给 (值, None)，失败给 (None, Failure)。

    返回二元组而不是抛异常，是今天的设计决策：
        调用方要在循环里采 4 个项目，其中任何一个失败都不能中断后面的。
        用返回值，失败的证据（Failure）**必须被放进某个筐里**；
        用异常，一个宽容的 `except Exception: pass` 就能让整项悄悄消失，
        而报告看起来还是完整的。

    四步：

      1. 在 try 里调 getCmd(...)，参数和 D8 一样：
             SnmpEngine()                       ← 每次新建！别用模块级全局
             CommunityData(community)
             UdpTransportTarget((host, port), timeout=timeout, retries=0)
             ContextData()
             ObjectType(ObjectIdentity(oid))
         try 包住它是因为这里可能抛**真的**异常（比如底层 socket 出错），
         这类异常在四元组里没有位置，只能靠 except 兜住 → 归 PROTOCOL。

      2. 把 indication / status / 第一个 varbind 的值交给 classify。
         var_binds 可能是空列表（超时时就是空的）—— 取不到值就传 None。

      3. classify 返回了 kind → 造一个 Failure 返回：
             Failure(stage=oid, kind=kind, detail=<人能看懂的原因>)
         detail 怎么写：
             indication  → str(indication)
             status      → f"{status.prettyPrint()}({int(status)})"   → "genErr(5)"
             value       → type(value).__name__                        → "NoSuchInstance"
             var_binds 空 → "空响应：var_binds 是空的"
         **必须带上具体原因**，否则报告里只有「timeout」四个字母，
         看的人还得再跑一遍才知道是哪台设备哪个 OID。

      4. 没出错 → 把值转成文本返回：
             OctetString → value.asOctets().decode("utf-8", "replace")
             其它类型     → value.prettyPrint()
         和 D10 的 walk 里同一个道理：中文接口名用 prettyPrint 会变十六进制。

    提示：`status` 即使存在也可能等价于 0（univ.Integer(0)）。
         判「有没有错」要用 `int(status) != 0`，不要直接 `if status` ——
         那样写能跑，但读代码的人不知道你在判什么。
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
    return value, None


# ---------------------------------------------------------------- TODO 3
def collect_device(
    host: str,
    *,
    community: str = "public",
    port: int = 161,
    timeout: float = 1.0,
) -> CollectionResult:
    """采一台设备的 4 个项目，成功进 values，失败进 failures。

    4 个项目固定是这 4 个（stage 名字就用左边这一列，验收脚本按它取值）：

        stage     怎么采
        sysDescr  get_raw(OID_SYSDESCR)
        uptime    get_raw(OID_SYSUPTIME)
        sysName   get_raw(OID_SYSNAME)
        ifTable   walk(host, OID_IF_DESCR, ...)     ← 这一项是 WALK，不是 GET

    做法：

      1. result = CollectionResult(host=host)
      2. 前三个走一个循环，逐个调 get_raw：
             值  → result.values[stage] = 值
             失败 → result.failures.append(Failure(stage=stage,
                                                   kind=failure.kind,
                                                   detail=failure.detail))
         注意 get_raw 返回的 Failure.stage 是 OID 字符串，这里要**换成
         项目名**（sysDescr）。报告是给运维看的，OID 他记不住。
      3. 第四个 ifTable 单独写，用 try 包住 walk(...)：
             成功 → result.values["ifTable"] = len(rows)     （存**接口个数**）
             抛 ProbeError → append 一条 Failure(
                                stage="ifTable",
                                kind=ErrorKind.PROTOCOL,
                                detail=str(exc))
         为什么这里只能归 PROTOCOL 而不是更精确的类别：
             D10 的 walk 已经把 error_status 压成一句话了，根因丢了。
             这是今天唯一一处将就 —— D14 合并时会把 walk 也改成结构化返回。
             能在注释里承认这种将就，比假装分类很精确要好。

    ⚠️ 不变量：len(values) + len(failures) == 4，永远成立（result.total 就是它）。
        某一项失败不是「从结果里消失」，而是「换一个筐」。这条不变量是
        证明「漏采」和「采集失败」没被混淆的唯一手段 —— 验收 C4/C5 就查它。

    ⚠️ 别短路：「第一项就超时了，后面三项不用试了吧」是错的想法。
        四项失败原因可能不同（sysDescr 超时、ifTable 却正常），短路之后
        报告里只有一条失败，运维以为整台设备掉线了。
        短路优化的是 3 次超时（3 秒），代价是把诊断信息砍掉 3/4。
    """
    result = CollectionResult(host=host)

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
        result.failures.append(
            Failure(stage="ifTable", kind=ErrorKind.PROTOCOL, detail=str(exc))
        )

    return result


# ---------------------------------------------------------------- 已写好
def render(result: CollectionResult) -> None:
    """打印一份人能读的报告。展示层的事全在这里，collect 不管格式化。"""
    print(f"{result.host:<16} {result.verdict:<9} {len(result.values)}/{result.total} 项成功")
    for stage, value in result.values.items():
        print(f"    {stage:<9} {value}")
    for failure in result.failures:
        print(f"    [!] {failure}")


def load_devices(path: str) -> list[dict]:
    """读 devices.json，返回设备清单。字段缺省用 .get 兜底。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("devices", [])


def scan_one(args) -> int:
    """D12 原来的单台逻辑，原样搬过来，行为不变。"""
    result = collect_device(
        args.host,
        community=args.community,
        port=args.port,
        timeout=args.timeout,
    )
    raw_uptime = result.values.get("uptime")
    if isinstance(raw_uptime, str):
        try:
            result.values["uptime"] = fmt_ticks(int(raw_uptime))
        except ValueError:
            pass
    render(result)
    return result.exit_code


def _collect_one(dev: dict) -> CollectionResult:
    """线程 worker：pysnmp 的同步 getCmd 其实是 asyncio 封装，内部调
    asyncio.get_event_loop()。主线程默认有事件循环，但线程池的工作线程
    没有——直接在线程里跑 collect_device 会报 'no current event loop'。
    所以每个 worker 入口先给当前线程装一个事件循环；结束前 cancel 掉
    pysnmp 残留的 timeout watchdog task 再关 loop，消除
    'Task was destroyed but it is pending' 噪音。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return collect_device(
            dev["host"],
            community=dev.get("community", "public"),
            port=dev.get("port", 161),
            timeout=dev.get("timeout", 0.5),
        )
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.close()


def scan_config(path: str, *, workers: int = 16) -> int:
    """读配置，并发扫一整批，退出码取所有设备里「最严重」的那个。

    D13 是「串行扫一批」，今天换成**线程池并发**：多台设备的网络等待
    同时发生，而不是一台一台排队等超时。

    为什么用线程而不是进程/协程：
        collect_device 绝大部分时间在等 UDP 超时（网络 I/O）。CPython 的
        GIL 在 I/O 等待时会释放，所以多线程在这里是真并行，不是假并行；
        只有纯 CPU 密集才需要多进程。walk / getCmd 都是 I/O 等待，
        线程池正好。

    一个并发专属的坑（D13 没有，今天才有）：
        pysnmp 的**同步** getCmd 是 asyncio 的封装，内部调
        asyncio.get_event_loop()。主线程默认有事件循环，但线程池工作线程
        没有——直接在线程里跑 collect_device 会炸。worker `_collect_one`
        必须先给当前线程装一个事件循环。这正是「不改 collect_device 一行」
        的体现：环境差异在并发封装层消化，核心逻辑不变。

    一条铁律：**线程里只采不打印**。render 放到主线程，按 devices 原始
    顺序统一渲染——多个线程同时 print 会让报告交错成一团乱麻，
    而且打印本身有共享锁，并发 print 反而可能更慢。
    """
    devices = load_devices(path)
    results: list[CollectionResult] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_collect_one, dev) for dev in devices]
        for fut in futs:                      # futs 保序 → 结果顺序 = 配置顺序
            results.append(fut.result())

    # 主线程统一收尾：转 uptime + 渲染 + 聚合
    for r in results:
        raw_uptime = r.values.get("uptime")
        if isinstance(raw_uptime, str):
            try:
                r.values["uptime"] = fmt_ticks(int(raw_uptime))
            except ValueError:
                pass
        render(r)

    PRIORITY = {"OK": 0, "DEGRADED": 1, "FAILED": 2}
    worst_verdict = max((r.verdict for r in results), key=lambda v: PRIORITY[v])
    worst = {"OK": 0, "DEGRADED": 2, "FAILED": 1}[worst_verdict]

    ok = sum(1 for r in results if r.verdict == "OK")
    degraded = sum(1 for r in results if r.verdict == "DEGRADED")
    failed = sum(1 for r in results if r.verdict == "FAILED")
    print(f"\n汇总：{len(results)} 台 → OK {ok} / DEGRADED {degraded} / FAILED {failed}，退出码 {worst}")
    return worst


def main() -> int:
    parser = argparse.ArgumentParser(description="采一台或多台设备并给出错误分类")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1161, help="假设备用 1161/1162/1163")
    parser.add_argument("--community", default="public")
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("--config", help="devices.json 路径，传了就扫整批")
    args = parser.parse_args()

    if args.config:
        return scan_config(args.config)
    return scan_one(args)


if __name__ == "__main__":
    sys.exit(main())
