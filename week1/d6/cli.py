"""D6 · 命令行入口：解析参数、采集、输出带耗时的表格。

    python cli.py
    python cli.py --attempts 1
    python cli.py --device core-rtr-02 -v
"""


import argparse
import logging
import sys
from pathlib import Path

from log_setup import get_log
from models import DEFAULT_FILE, load_devices, Device, SnmpError
from probe import collect_all

def print_table(devices: list[Device], elapsed: dict[str, float] | None = None) -> None:
    """【已写好 · 这个函数里的 print 不用改】

    「报告的最终产物」继续用 print 是对的：它是要给人看/给管道用的程序输出，
    不该混进日志级别。判据很简单——
        · 过程信息（加载了几台、哪里出错、谁重试了）-> log
        · 最终产物（表格、json、csv 的内容）        -> print

    D6 新增：给了 elapsed（设备名 -> 秒数）就多打一列耗时。
    采集失败的设备也要显示耗时——「8 秒后才失败」和「0.1 秒就失败」含义完全不同，
    前者是一直在超时重试，后者是立刻被拒绝（多半是认证问题）。
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


# ============================================================ 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="D6 · 带重试与计时的设备巡检")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="设备清单 json 路径")
    parser.add_argument("--device", help="只看某台设备，例如 core-sw-01")
    parser.add_argument("--limit", type=int, help="只输出前N台设备")
    parser.add_argument("-v", "--verbose", action="store_true", help="打开DEBUG日志")
    parser.add_argument("--attempts", type=int, default=3, help="单台设备最多尝试几次（含第一次）")
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error(f"--attempts必须是 >=1 的整数，收到{args.attempts}")

    if args.limit is not None and args.limit < 1:
        parser.error(f"--limit必须是 >= 1 的整数，收到{args.limit}")

    level = logging.DEBUG if args.verbose else logging.INFO
    log = get_log(level)

    try:
        devices = load_devices(Path(args.file))
        # 【已写好·示范】为什么用 %s 而不是 f-string：级别被过滤时不会白白拼字符串
        log.info("从 %s 加载 %d 台设备", Path(args.file).name, len(devices))
    except SnmpError as exc:
        log.error("设备清单读取失败：%s", exc)
        return 2

    picked = devices
    if args.device:
        picked = [d for d in devices if d.name == args.device]
        if not picked:
            log.error("设备不存在：%s", args.device)
            return 2

    if args.limit is not None and args.limit < len(picked):
        log.warning("只输出前 %d 台（实际 %d 台）", args.limit, len(picked))
        picked = picked[: args.limit]

    for d in picked:
        log.debug("设备 %s: status=%s cpu=%s", d.name, d.status, d.cpu)

    results = collect_all(picked, attempts=args.attempts)
    print_table(
        [r.device for r in results],
        {r.device.name: r.elapsed for r in results},
    )

    log.info("巡检结束，共输出 %d 台", len(picked))  # 【已写好】
    return 0


if __name__ == "__main__":
    sys.exit(main())
