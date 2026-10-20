"""常用 Python 片段库（可直接 import，也可整段抄走）

为什么给你这个文件？
    「会基础但很少写」的人，最大的问题不是不懂语法，而是**每次从空白文件开始，
    连读个 json、配个日志都要去搜**。搜一次 10 分钟，一天搜 5 次，正反馈就断了。

    这个文件把 90% 场景的样板代码固化下来。用的时候：
        from snippets import setup_logger, load_json, fmt_table

    跑一遍看效果：
        python snippets.py
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Self, TypeVar

T = TypeVar("T")

# ============================================================ 1. 日志（替代 print）
def setup_logger(name: str = "app", level: int = logging.INFO, logfile: str | None = None) -> logging.Logger:
    """一行配好日志。比 print 强的地方：有级别、有时间、能写文件、线上能关掉。

    用法：log = setup_logger("snmp"); log.info("采集 %s 完成", device)
    注意：用 %s 占位而不是 f-string，级别不够时不会白拼字符串。
    """
    logger = logging.getLogger(name)
    if logger.handlers:  # 避免重复添加 handler（重复调用会打印两遍）
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


# ============================================================ 2. JSON 读写
def load_json(path: str | Path, default: Any = None) -> Any:
    """读 json；文件不存在返回 default，而不是抛异常（配置文件场景更友好）。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p} 不是合法 JSON：{exc}") from exc


def save_json(data: Any, path: str | Path, indent: int = 2) -> Path:
    """写 json，自动建父目录。ensure_ascii=False 才不会被转成 \\uXXXX。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    return p


# ============================================================ 3. 表格输出
def fmt_table(rows: Sequence[dict[str, Any]], headers: Sequence[str] | None = None) -> str:
    """把 list[dict] 排成对齐的文本表格（终端里看数据必备，别再用 print(dict)）。

    中文对齐会因全角字符略有偏移，够用即可；要精确对齐用 rich 库。
    """
    if not rows:
        return "(空)"
    headers = list(headers or rows[0].keys())
    widths = []
    for h in headers:
        w = max(len(str(h)), *(len(str(r.get(h, ""))) for r in rows))
        widths.append(w)

    def line(ch: str = "-") -> str:
        return "+".join(ch * (w + 2) for w in widths)

    out = [line(), " | ".join(str(h).ljust(w) for h, w in zip(headers, widths)), line()]
    for r in rows:
        out.append(" | ".join(str(r.get(h, "")).ljust(w) for h, w in zip(headers, widths)))
    out.append(line())
    return "\n".join(out)


# ============================================================ 4. CSV 导出
def write_csv(rows: Sequence[dict[str, Any]], path: str | Path) -> Path:
    """导 CSV（Excel 能直接打开）。注意 encoding + newline='' 两个坑。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig = Excel 不乱码
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return p


# ============================================================ 5. 重试（网络采集必备）
def retry_call(fn: Callable[[], T], times: int = 3, delay: float = 0.5, backoff: float = 2.0) -> T:
    """失败重试，指数退避。SNMP/HTTP 采集必用。

    为什么不用装饰器？因为你还在打基础，函数式写法更好调试；
    等你熟了再改成 @retry 装饰器（见文件末尾注释）。
    """
    last: Exception | None = None
    for i in range(times):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 —— 重试层要捕所有异常
            last = exc
            if i < times - 1:
                time.sleep(delay * (backoff**i))
    raise RuntimeError(f"重试 {times} 次仍失败") from last


# ============================================================ 6. 环境变量
def env(name: str, default: str = "") -> str:
    """读环境变量并给默认值。配置不要写死在代码里。"""
    return os.getenv(name, default)


def env_int(name: str, default: int) -> int:
    """读整数环境变量；值非法时用默认值，别让程序因为一个配置崩掉。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ============================================================ 7. 分批处理
def chunked(items: Iterable[T], size: int) -> Iterable[list[T]]:
    """把大列表切块（比如 1000 台设备每 50 台一批，避免打爆网管）。"""
    buf: list[T] = []
    for it in items:
        buf.append(it)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


# ============================================================ 8. 安全转换
def safe_int(value: Any, default: int = 0) -> int:
    """SNMP 返回值经常是 'unknown' / None / b'123'，直接 int() 会炸。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============================================================ 9. 计时
class Timer:
    """with Timer() as t: ...  然后 print(t.seconds)。比手写 perf_counter 干净。"""

    def __enter__(self) -> Self:  # Self = 「返回自己的类型」，3.11+ 才有
        self._start = time.perf_counter()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.perf_counter() - self._start


# ============================================================ 10. 演示
if __name__ == "__main__":
    log = setup_logger("demo", logfile="out/demo.log")
    log.info("片段库自检开始")

    devices = [
        {"name": "core-sw-01", "cpu": 92, "status": "up"},
        {"name": "core-rtr-02", "cpu": 41, "status": "up"},
        {"name": "agg-sw-03", "cpu": -1, "status": "down"},
    ]
    print(fmt_table(devices))

    with Timer() as t:
        ok = retry_call(lambda: {"result": "ok"}, times=2)
    print(f"retry 返回 {ok}，耗时 {t.seconds:.3f}s")

    save_json({"devices": devices}, "out/snippets_demo.json")
    write_csv(devices, "out/snippets_demo.csv")
    print("已写出 out/snippets_demo.json 与 out/snippets_demo.csv")

    print("分批：", list(chunked(range(7), 3)))
    print("safe_int('unknown') =", safe_int("unknown"), "| env 缺省 =", env("NOT_EXIST", "fallback"))
    log.warning("这是一条 warning，注意它带级别和时间")
    log.error("这是一条 error")

# ============================================================ 附：装饰器版重试（熟了再用）
# def retry(times: int = 3, delay: float = 0.5):
#     def deco(fn):
#         def wrapper(*args, **kwargs):
#             for i in range(times):
#                 try:
#                     return fn(*args, **kwargs)
#                 except Exception:
#                     if i == times - 1:
#                         raise
#                     time.sleep(delay * 2 ** i)
#         return wrapper
#     return deco
#
# @retry(times=3)
# def collect(ip): ...
