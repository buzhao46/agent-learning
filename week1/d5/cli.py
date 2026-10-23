"""D4 · 命令行入口：解析参数、编排流程、输出表格。

    python cli.py --limit 2 --verbose
    python cli.py --device core-sw-01
"""


import argparse
import logging
import sys
from pathlib import Path

from log_setup import get_log
from models import DEFAULT_FILE, load_devices, Device, SnmpError


def print_table(devices: list[Device]) -> None:
    """【已写好 · 这个函数里的 print 不用改】

    「报告的最终产物」继续用 print 是对的：它是要给人看/给管道用的程序输出，
    不该混进日志级别。判据很简单——
        · 过程信息（加载了几台、哪里出错、谁重试了）-> log
        · 最终产物（表格、json、csv 的内容）        -> print
    """
    print(f"\n{'设备':<14}{'IP':<14}{'状态':<8}{'CPU':>5}  健康")
    print("-" * 52)
    for d in devices:
        flag = "✓" if d.is_healthy() else "✗"
        print(f"{d.name:<14}{d.ip:<14}{d.status:<8}{d.cpu:>5}  {flag}")
    bad = [d.name for d in devices if not d.is_healthy()]
    print(f"\n共 {len(devices)} 台，异常 {len(bad)} 台：{', '.join(bad) or '无'}")


# ============================================================ 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="D2 · 日志版设备巡检")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="设备清单 json 路径")
    parser.add_argument("--device", help="只看某台设备，例如 core-sw-01")
    parser.add_argument("--limit", type=int, help="只输出前N台设备")
    parser.add_argument("-v", "--verbose", action="store_true", help="打开DEBUG日志")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error(f"--limit必须是 >= 1 的整数，收到{args.limit}")

    # 【已写好】先建 logger。你还没写 setup_logger 时，这里给一行提示而不是一屏堆栈。
    # 注意这里用的是 sys.stderr.write 而不是 print/log —— 日志系统此刻还不存在，
    # 这是唯一"还没法用日志"的地方，所以 C6 检查不会把它算成漏改的 print。
    
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

    print_table(picked)  # 【已写好】报告输出，保留 print
    log.info("巡检结束，共输出 %d 台", len(picked))  # 【已写好】
    return 0


if __name__ == "__main__":
    sys.exit(main())
