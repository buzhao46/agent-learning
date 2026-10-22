"""D2 · 换掉 print：过程信息走 logging，报告输出仍用 print

D1 的成果（数据外置）已经搬过来了，load_devices 等函数都是你写好的版本。
今天只做一件事：**让日志系统真正存在**。

本文件已写好 80%，你只需要：
  1) 补 setup_logger()（~15 行，核心任务）
  2) 把 main() 里标了 TODO(你·第N处) 的 print 改成日志

跑法：
    cd week1/d2
    python loader.py                 # 正常：终端只显示表格，日志另存 out/run.log
    python loader.py --file nope.json  # 触发 ERROR
    python check_d2.py               # 自动验收，跑它就知道过没过
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

Status = Literal["up", "down"]

HERE = Path(__file__).parent
DEFAULT_FILE = HERE / "devices.json"
LOG_FILE = HERE / "out" / "run.log"


class SnmpError(RuntimeError):
    """配置错误 / 设备不存在都抛这个，调用方按需捕获。"""


# ============================================================ 日志（你的任务）
def setup_logger(level: int = logging.INFO, logfile: Path | None = LOG_FILE) -> logging.Logger:
    """建一个日志器：控制台一份 + 文件一份。**【你的任务 · 约 15 行】**

    按这 6 步写（每一步都有它的理由，写完要能讲出来）：

      1) logger = logging.getLogger("snmpcheck")
         —— 用名字取，不用 logging.getLogger()。同一个名字重复取回的是同一个对象，
            这是「重复调用会不会打印两遍」的关键。
         —— 名字也会出现在日志里（%(name)s），线上多模块时一眼看出谁打的。

      2) if logger.handlers: return logger
         —— 守卫。不加这两行，函数被调用两次就会挂两个 handler，
            同一行日志打印两遍。这是新手第一大坑。

      3) logger.setLevel(level)
         —— 不设的话默认是 WARNING，你的 log.info 会"消失"。
            （是消失不是报错，所以特别难查 —— 第二大坑）

      4) 做一个格式器：
         fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%H:%M:%S")
         —— 时间 + 级别 + logger 名 + 正文。级别用 -8s 左对齐，日志才对得整齐。

      5) console = logging.StreamHandler(sys.stderr)  # 注意是 stderr，不是 stdout
         console.setFormatter(fmt)
         logger.addHandler(console)
         —— 为什么用 stderr？因为 stdout 要留给表格这种「程序输出」，
            这样 `python loader.py > report.txt` 时报告干净、日志仍然在屏幕上看得到。

      6) if logfile:
             logfile.parent.mkdir(parents=True, exist_ok=True)   # out/ 可能不存在
             fh = logging.FileHandler(logfile, encoding="utf-8")  # 不写 encoding，Windows 下中文乱码
             fh.setFormatter(fmt)
             logger.addHandler(fh)
         —— mkdir 一定要写，否则第一次跑直接 FileNotFoundError。

      7) return logger
    """
    logger = logging.getLogger("snmpcheck")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logger.addHandler(console)
    if logfile:
        logfile.parent.mkdir(parents=True,exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


_LOG: logging.Logger | None = None


def get_log() -> logging.Logger:
    """【已写好·建议读懂】延迟初始化：第一次真正要用日志时才建 logger。

    好处：模块能被 import 而不触发副作用（写文件、加 handler），
    测试和验收脚本才好控制。这也是"不要在 import 时干重活"的一般原则。
    """
    global _LOG
    if _LOG is None:
        _LOG = setup_logger()
    return _LOG


# ============================================================ 数据模型
@dataclass
class Device:
    name: str
    ip: str
    status: Status = "up"
    cpu: int = 0
    tags: list[str] = field(default_factory=list)

    def is_healthy(self, cpu_limit: int = 80) -> bool:
        return self.status == "up" and self.cpu < cpu_limit

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Device:
        """json 里的一条 dict -> Device（D1 你写好的版本）。"""
        status = cast(Status, raw.get("status", "up"))
        if status not in ("up", "down"):
            raise SnmpError(f"非法状态：{status!r}")
        return cls(
            name=raw["name"],
            ip=raw["ip"],
            status=status,
            cpu=int(raw.get("cpu", 0)),
            tags=list(raw.get("tags", [])),
        )


# ============================================================ 读写
def load_devices(path: Path = DEFAULT_FILE) -> list[Device]:
    """读 devices.json（D1 你写好的版本）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SnmpError(f"配置文件不存在：{path}") from exc
    data = json.loads(text)
    if data["count"] != len(data["devices"]):
        # 【已写好·示范】这行原来写的是 log.warning(...)，而 log 根本不存在，
        # 一旦 count 对不上就 NameError 崩掉。这就是「日志系统没建好」的真实代价。
        # 用 %s 占位而不是 f-string：级别不够时不会白拼字符串（高频日志差别很大）。
        get_log().warning(
            "devices.json 里 count=%s，实际 %s 台", data["count"], len(data["devices"])
        )
    return [Device.from_dict(raw) for raw in data["devices"]]


def save_devices(devices: list[Device], path: Path = DEFAULT_FILE) -> None:
    """把内存里的设备写回 json（D1 已写好）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"count": len(devices), "devices": [d.to_dict() for d in devices]}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


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
    args = parser.parse_args()

    # 【已写好】先建 logger。你还没写 setup_logger 时，这里给一行提示而不是一屏堆栈。
    # 注意这里用的是 sys.stderr.write 而不是 print/log —— 日志系统此刻还不存在，
    # 这是唯一"还没法用日志"的地方，所以 C6 检查不会把它算成漏改的 print。
    try:
        log = get_log()
    except NotImplementedError as exc:
        sys.stderr.write(f"[TODO] {exc}\n       补完后重跑：python loader.py\n")
        log.error("设备清单读取失败： %s", exc)
        return 3

    try:
        devices = load_devices(Path(args.file))
        # 【已写好·示范】为什么用 %s 而不是 f-string：级别被过滤时不会白白拼字符串
        log.info("从 %s 加载 %d 台设备", Path(args.file).name, len(devices))
    except SnmpError as exc:
        # print(f"[ERROR] {exc}")  # TODO(你·第1处)：改成 log.error(...)
        log.error("设备清单读取失败： %s", exc)
        return 2

    picked = devices
    if args.device:
        picked = [d for d in devices if d.name == args.device]
        if not picked:
            # print(f"[ERROR] 设备不存在：{args.device}")  # TODO(你·第2处)：改成 log.error(...)
            log.error("设备不存在：%s", args.device)
            return 2

    print_table(picked)  # 【已写好】报告输出，保留 print
    log.info("巡检结束，共输出 %d 台", len(picked))  # 【已写好】
    return 0


if __name__ == "__main__":
    sys.exit(main())
