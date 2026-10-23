"""日志配置：谁来 import 都拿到同一个logger，且只在第一次真正使用时才建 handler."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

HERE = Path(__file__).parent
LOG_FILE = HERE / "out" / "run.log"

def setup_logger(level: int = logging.INFO, logfile: Path | None = LOG_FILE) -> logging.Logger:
    """建一个日志器：控制台一份+文件一份。
    按这6步写（每一步都有它的理由，写完要能将出来）:
        1) logger = logging.getLogger("snmpcheck")
            ——用名字取，不用logging.getLogger()。同一个名字重复取回的是同一个对象，
                 这是【重复调用会不会打印两遍】的关键。
            ——名字也会出现在日志里（%(name)s)， 线上多模块时一眼看出是谁打的。
        2) if logger.handlers: return logger
            守卫。不加这两行，函数被调用两次就会挂两个handler，
            同一行日志打印两遍。
        3) logger.setLevel(level)
            不设置的话默认是warning，你的log.info会消失

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
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",  "%H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger

_LOG: logging.Logger | None = None


def get_log(level: int = logging.INFO) -> logging.Logger:
    global _LOG
    if _LOG is None:
        _LOG = setup_logger(level)
    return _LOG
