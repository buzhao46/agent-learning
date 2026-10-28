"""D10 · WALK 接口表：一次读一列，再把三列拼成一张表。

    python ifwalk.py --port 1161              # 4 个接口
    python ifwalk.py --port 1161 --max-rows 2 # 只看前 2 行

昨天（D9）用 getCmd 读三个固定 OID —— 那是「我知道我要什么」。
今天要读一张**行数事先不知道**的表：接口有几个、每行叫什么，只有问了设备才知道。
这就得靠 WALK：从某一列的开头往下走，一直走到离开这棵子树。

四个 TODO，约 60 行：
    1. walk              一直 GETNEXT，把一列读回来（今天的核心）
    2. if_index          从 "…2.2.1.2.3" 里取出行号 3
    3. build_interfaces  三列按行号合并成 Interface 列表
    4. fmt_table         打印成对齐的表格

一个必须先知道的事实：pysnmp 6.1.4 里 `nextCmd` 只走**一步**（一次 GETNEXT），
真正走完整张表的是 `walkCmd`。这和网上 4.x/5.x 的教程正好相反 ——
照旧教程写 `for x in nextCmd(...)`，在 6.1.4 下会直接报错。
"""  # noqa: DOC001

from __future__ import annotations

import argparse
import logging
import sys
import unicodedata
from dataclasses import dataclass

from pysnmp.hlapi import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    walkCmd,
)
from pysnmp.proto.rfc1902 import OctetString

from snmpio import ProbeError

# 三列各自的「列 OID」（不带行号）。WALK 就从这里往下走。
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"  # 接口名，字符串
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"  # 运行状态，整数枚举
OID_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"  # 累计入字节，Counter32

# ifOperStatus 的取值是枚举，不是「1 表示好、2 表示坏」这么随意。
# RFC 2863 的定义，真实设备七种值都会出现。
OPER_STATUS = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}

# ⚠️ 每次 WALK 都要用它自己的 SnmpEngine（就写在 walkCmd 的第一个参数里）。
# 实测：同一个 SnmpEngine 跑第二次 walkCmd 会**永久卡住**，不报错、不超时，
# 就是不动了 —— 你必须 Ctrl+C 才退出。pysnmp 6.1.4 的 dispatcher 没清干净。
# 而 D9 的 snmp_get 复用同一个 engine 做多次 GET 是没问题的，两种情况不一样。
#
# 所以这个文件里刻意不给你全局 _ENGINE。别为了"省一点开销"把它提上来。


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Interface:
    """一个接口的一行数据。缺的字段就是 None / "未知"，不要用 0 冒充。"""

    index: int
    name: str
    status: str
    in_octets: int | None


# ---------------------------------------------------------------- 显示宽度
def disp_width(text: str) -> int:
    """字符串在终端里占几格。

    len("上联口") == 3，但它在中文字体下占 6 格 —— 用 len() 对齐必然错位。
    East Asian Width 为 W(宽) / F(全角) 的字符按 2 格算。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格（不是按 len）。"""
    return text + " " * max(0, width - disp_width(text))


# ---------------------------------------------------------------- 1. WALK
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

    要做的四件事：
      1. 用 walkCmd 起一个生成器，参数顺序和 getCmd 一模一样：
             SnmpEngine()   ← 每次新建一个，见文件开头那段警告
             CommunityData / UdpTransportTarget / ContextData /
             ObjectType(ObjectIdentity(base_oid))
      2. 传一个关键选项（写在 walkCmd(...) 的关键字参数里）：
             lexicographicMode=False   走到离开 base_oid 子树就停
         不传它会一路走到整棵 MIB 的末尾 —— 本机的假设备有 20 个 OID，
         真设备有上万条，你会拿到一张根本不想要的大表。
         max_rows 不在这里传，walkCmd 没这个参数，行数上限你自己数。
      3. 每拿到一个 (name, val)，取文本值：
             OctetString  → val.asOctets().decode("utf-8", "replace")
             其它类型      → val.prettyPrint()
         为什么不能一律 prettyPrint()：中文接口名会被它输出成 0xe4b88ae8...
         十六进制串，报告里就没法看了；而 Integer 用 asOctets() 又拿不到数字。
      4. 行数到 max_rows 就停。这是防「设备回一个不递增的 OID 导致原地打转」
         的唯一保险 —— 真实网络里出过这种事，程序会一直转下去直到你 Ctrl+C。

    出错怎么办（这条是今天的设计决策，想清楚）：
        第一行还没拿到就出错  → 抛 ProbeError（设备级失败：不可达 / community 错）
        已经拿到若干行后出错  → 打印一句 warning 就停，把已有的行返回
        半张表也是数据，不该因为最后一行坏了就全丢。

    注意 generation 的错误要从四元组里取：error_indication / error_status。
    """
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
            # 半张表也是数据：已经拿到若干行就只告警，不丢掉已有结果
            logger.warning(
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


# ---------------------------------------------------------------- 2. 取行号
def if_index(oid: str, base: str) -> int:
    """从 "1.3.6.1.2.1.2.2.1.2.3" 取出行号 3（base 是 "1.3.6.1.2.1.2.2.1.2"）。

    行号永远是 OID 里 base 之后的那一段。别写死 -1 ——
    ipAdEntAddr 这种表的行号有 4 段（10.20.30.1），-1 只取到最后一段。
    """
    tail = oid[len(base) + 1 :]
    return int(tail.split(".")[0])


# ---------------------------------------------------------------- 3. 合并
def build_interfaces(
    descr: list[tuple[str, str]],
    status: list[tuple[str, str]],
    octets: list[tuple[str, str]],
) -> list[Interface]:
    """三列按行号合并，返回按 index 排好序的 Interface 列表。

    这是今天最容易写错的一步。**绝对不能 zip(descr, status)**：
    设备少回一行（接口刚创建、处于 notPresent），zip 就从那一行开始全部错位，
    而且错得很安静 —— 名字是 A 的接口配上了 B 的状态，报告照样打得出来。

    正确做法：先用 if_index 把三列各自变成 {行号: 值} 的字典，再按行号取。

    缺数据怎么办：
        status 里没有这个行号  → status="未知"（不是 "down"！猜错会误报告警）
        octets 里没有这个行号  → in_octets=None
        行号只以 descr 为准（接口名是这张表的主键，没有名字的行没有意义）
    """
    name_by_index = {if_index(oid,OID_IF_DESCR): text for oid, text in descr}
    status_by_index = { if_index(oid, OID_IF_OPER):   OPER_STATUS.get(int(text), f"未知({text})") for oid, text in status }
    octets_by_index = { if_index(oid, OID_IF_IN_OCTETS): int(text) for oid, text in octets }
    return [ 
        Interface(
            index=index,
            name=name,
            status=status_by_index.get(index, "未知"),
            in_octets=octets_by_index.get(index),  
        )
        for index, name in sorted(name_by_index.items())
    ]


# ---------------------------------------------------------------- 4. 打印
def fmt_table(rows: list[Interface]) -> str:
    """拼出对齐的表格，返回整段文本（不自己 print，方便别人复用/写文件）。

    要长成这个样子（列宽 = 该列最长内容的显示宽度，列间两个空格）：

        索引  接口名       状态  入字节
        ----  -----------  ----  ----------
        1     GE0/0/1      up    1284993201
        2     GE0/0/2      up    88120442
        3     上联口-核心  down  4294967100
        4     XGE0/0/4     up    512004

    第二行是短横线，长度等于该列列宽。表头也算进列宽："接口名" 显示宽度 6，
    而 "上联口-核心" 是 11，所以这一列宽 11（不是 6，也不是 12）。

    pad() 已经写好了，直接用它补齐；不要用 text.ljust(width) ——
    ljust 数的是字符个数，中文会被少补一半空格，表格立刻歪掉。

    缺数据怎么打：in_octets 为 None 时打 "-"。
    """
    header = ("索引", "接口名", "状态", "入字节")
    body = [
        (str(row.index), row.name, row.status, "-" if row.in_octets is None else str(row.in_octets))
        for row in rows
    ]
    # 列宽 = 这一列（含表头）里最长的那个的显示宽度
    width = [max(disp_width(row[i]) for row in (header, *body)) for i in range(len(header))]

    def line(cells):
        return "  ".join(pad(cell, width[i]) for i, cell in enumerate(cells)).rstrip()

    out = [line(header), line(tuple("-" * w for w in width))]
    out.extend(line(row) for row in body)
    return "\n".join(out)


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="WALK 一台设备的接口表")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=161, help="假设备用 1161")
    parser.add_argument("--community", default="public")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--max-rows", type=int, default=200, help="每列最多读多少行")
    args = parser.parse_args()

    try:
        descr = walk(
            args.host,
            OID_IF_DESCR,
            community=args.community,
            port=args.port,
            timeout=args.timeout,
            max_rows=args.max_rows,
        )
        status = walk(
            args.host,
            OID_IF_OPER,
            community=args.community,
            port=args.port,
            timeout=args.timeout,
            max_rows=args.max_rows,
        )
        octets = walk(
            args.host,
            OID_IF_IN_OCTETS,
            community=args.community,
            port=args.port,
            timeout=args.timeout,
            max_rows=args.max_rows,
        )
    except ProbeError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1

    rows = build_interfaces(descr, status, octets)
    print(fmt_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
