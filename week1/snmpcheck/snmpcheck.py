"""snmpcheck · 设备巡检命令行工具（D7 成型版）

用法（D19 打包之后可以省掉 `python snmpcheck.py` 前缀，直接 `snmpcheck list`）：

    python snmpcheck.py list
    python snmpcheck.py list --limit 2
    python snmpcheck.py status
    python snmpcheck.py status --attempts 1 -v
    python -m snmpcheck list

两个子命令：
    list     只列设备清单，不采集。快，用来确认配置读对了
    status   逐台采集（带重试）+ 输出状态与耗时

今天要写的是本文件最后四个东西：
    cmd_list / cmd_status / build_parser / main
前面那些「已写好」的不要动。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from log_setup import get_log
from models import DEFAULT_FILE, Device, SnmpError, load_devices
from probe import collect_all

PROG = "snmpcheck"


# ============================================================ 已写好：输出与工具
def print_table(devices: list[Device], elapsed: dict[str, float] | None = None) -> None:
    """【已写好 · 不用改】

    「报告的最终产物」继续用 print 是对的：它是要给人看/给管道用的程序输出，
    不该混进日志级别。判据很简单——
        · 过程信息（加载了几台、哪里出错、谁重试了）-> log
        · 最终产物（表格、json、csv 的内容）        -> print

    给了 elapsed（设备名 -> 秒数）就多打一列耗时；不给就是纯清单。
    """
    head = f"\n{'设备':<14}{'IP':<14}{'状态':<8}{'CPU':>5}  健康"
    width = 52
    if elapsed is not None:
        head += f"{'耗时':>10}"
        width = 62
    print(head)
    print("-" * width)

    for d in devices:
        flag = "✓" if d.is_healthy() else "✗"
        row = f"{d.name:<14}{d.ip:<14}{d.status:<8}{d.cpu:>5}  {flag}"
        if elapsed is not None:
            sec = elapsed.get(d.name)
            row += f"{'-':>11}" if sec is None else f"{sec * 1000:>9.1f}ms"
        print(row)

    bad = [d.name for d in devices if not d.is_healthy()]
    print(f"\n共 {len(devices)} 台，异常 {len(bad)} 台：{', '.join(bad) or '无'}")


def positive_int(text: str) -> int:
    """【已写好 · 不用改】给 argparse 的 type 用：只接受 >= 1 的整数。

    D3–D6 你是在 parse_args() 之后手动判断 `if args.limit < 1: parser.error(...)`。
    那种写法有两个毛病：
      1. 每加一个整数参数就要多写一段校验
      2. 哪些参数校验过了、哪些漏了，全靠记

    改成 type 回调之后，校验跟着参数定义走，不会再漏；而且 argparse 会自动把
    参数名和传入值拼进错误信息里。这是 argparse 里最值得早点学会的一招。
    """
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"必须是 >= 1 的整数，收到 {value}")
    return value


def load_and_filter(args: argparse.Namespace) -> list[Device] | None:
    """【已写好 · 不用改】读设备清单，再按 --device / --limit 筛一遍。

    返回 None 表示出错（调用方直接 return 2）；否则返回筛好的列表。

    为什么把这段抽出来：`list` 和 `status` 都需要它。同一段逻辑复制两遍，
    改的时候必然漏掉一处。两个子命令共享的东西，就该是一个函数。
    """
    log = get_log()
    try:
        devices = load_devices(Path(args.file))
        log.info("从 %s 加载 %d 台设备", Path(args.file).name, len(devices))
    except SnmpError as exc:
        log.error("设备清单读取失败：%s", exc)
        return None

    picked = devices
    if args.device:
        picked = [d for d in devices if d.name == args.device]
        if not picked:
            log.error("设备不存在：%s", args.device)
            return None

    if args.limit is not None and args.limit < len(picked):
        log.warning("只输出前 %d 台（实际 %d 台）", args.limit, len(picked))
        picked = picked[: args.limit]

    return picked


# ============================================================ 你要写的四个
def cmd_list(args: argparse.Namespace) -> int:
    """① `list` 子命令：只列设备清单，不采集。

    要求：
        - 调 load_and_filter(args)，拿到 None 就 return 2
        - 调 print_table(picked)，**只传一个参数**（不采集，自然没有耗时）
        - 成功 return 0

    大约 6 行。
    """
    picked = load_and_filter(args)
    if picked is None:
        return 2
    print_table(picked)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """② `status` 子命令：逐台采集（带重试），输出状态 + 耗时。

    要求：
        - 调 load_and_filter(args)，拿到 None 就 return 2
        - 调 collect_all(picked, attempts=args.attempts) 拿 list[ProbeResult]
        - 调 print_table 时把耗时字典一起传进去（照抄 D6 最后那段写法）
        - 最后 log.info 一句「巡检结束，共输出 N 台」
        - 成功 return 0

    大约 10 行。这段基本是 D6 的 main 后半截搬过来。
    """
    log = get_log()
    picked = load_and_filter(args)
    if picked is None:
        return 2

    results = collect_all(picked,attempts=args.attempts)
    print_table(
        [r.device for r in results],
        {r.device.name: r.elapsed for r in results},
    )
    log.info("巡检结束，共输出 %d 台", len(picked))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """搭出带 list / status 两个子命令的解析器。"""
    parser = argparse.ArgumentParser(prog=PROG, description="设备巡检工具：先确认配置，再采一遍")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="只列设备清单，不采集")
    p_list.add_argument("--file", default=str(DEFAULT_FILE), help="设备清单 json 路径")
    p_list.add_argument("--device", help="只看某台设备，例如 core-sw-01")
    p_list.add_argument("--limit", type=positive_int, help="最多采几台")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="采集并输出状态与耗时")
    p_status.add_argument("--file", default=str(DEFAULT_FILE), help="设备清单 json 路径")
    p_status.add_argument("--device", help="只看某台设备，例如 core-sw-01")
    p_status.add_argument("--limit", type=positive_int, help="只采集前N台设备")
    p_status.add_argument("--attempts", type=positive_int, default=3, help="单台最多尝试几次（含第一次）")
    p_status.add_argument("-v", "--verbose", action="store_true", help="打开DEBUG日志")
    p_status.set_defaults(func=cmd_status)

    return parser


def main() -> int:
    """④ 入口：解析参数 → 建 logger → 分发。

    要求：
        - parser = build_parser()
        - args = parser.parse_args()
        - 按 args 里的 verbose 决定日志级别，然后 get_log(level)
        - return args.func(args)

    有一个坑要注意：`list` 子命令**没有**定义 `-v`，所以 `args.verbose` 在
    走 list 的时候根本不存在，直接访问会 AttributeError。
    想想怎么安全地取一个「可能不存在的属性」。（D3 那个 args.v / args.verbose 的坑，
    本质是同一类问题：**你不能假设 Namespace 里一定有某个属性**。）

    顺序也有讲究：必须**先建 logger、再调 func**。因为 load_and_filter 和
    collect_all 内部都会 get_log()，logger 没初始化就会拿到默认配置
    （级别是 WARNING，INFO 全丢，你什么都看不到）。
    """
    parser = build_parser()
    args = parser.parse_args()
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    get_log(level)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
