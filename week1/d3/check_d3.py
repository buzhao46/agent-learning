"""D3 自动验收：7 项，跑它就知道过没过。

    cd week1/d3
    python check_d3.py

退出码 0 = 必做项全过；非 0 = 还有 ❌。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).parent
LOG_FILE = ROOT / "out" / "run.log"
DEVICES = json.loads((ROOT / "devices.json").read_text(encoding="utf-8"))["devices"]
NAMES = [d["name"] for d in DEVICES]


# ---------------------------------------------------------------- 工具
def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "loader.py", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env(),
        timeout=30,
        check=False,
    )


def reset_log() -> None:
    if LOG_FILE.exists():
        LOG_FILE.unlink()


def log_text() -> str:
    return LOG_FILE.read_text(encoding="utf-8") if LOG_FILE.exists() else ""


def printed_devices(stdout: str) -> list[str]:
    """表格里实际出现了哪几台设备（按出现顺序）。"""
    return [n for n in NAMES if n in stdout]


def has_traceback(proc: subprocess.CompletedProcess[str]) -> bool:
    return "Traceback (most recent call last)" in (proc.stderr or "")


# ---------------------------------------------------------------- 检查项
def check_help() -> tuple[bool, str]:
    """--help 里三个参数都要出现。"""
    proc = run_cli("--help")
    if proc.returncode != 0:
        return False, f"python loader.py --help 退出码 {proc.returncode}，应该 0"
    out = proc.stdout or ""
    missing = [f for f in ("--device", "--limit", "--verbose") if f not in out]
    if missing:
        return False, f"--help 里缺少：{', '.join(missing)}"
    return True, "--help 能列出 --device / --limit / --verbose"


def check_limit_basic() -> tuple[bool, str]:
    """--limit 2 只输出 2 台。"""
    reset_log()
    proc = run_cli("--limit", "2")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return False, f"--limit 2 退出码 {proc.returncode}（应 0）；stderr：{tail[-1] if tail else '无'}"
    shown = printed_devices(proc.stdout or "")
    if len(shown) != 2:
        return False, f"--limit 2 应只输出 2 台，实际 {len(shown)} 台：{shown}"
    return True, f"--limit 2 -> 只输出 {shown}"


def check_limit_over() -> tuple[bool, str]:
    """--limit 超过总数不算错，全输出。"""
    reset_log()
    proc = run_cli("--limit", "99")
    if proc.returncode != 0:
        return False, f"--limit 99 退出码 {proc.returncode}，应该 0（超界不算错）"
    shown = printed_devices(proc.stdout or "")
    if len(shown) != len(NAMES):
        return False, f"--limit 99 应输出全部 {len(NAMES)} 台，实际 {len(shown)} 台"
    return True, "--limit 超界时全量输出，不报错"


def check_limit_invalid() -> tuple[bool, str]:
    """--limit 0 / -1 要明确报错（退出码 2），而不是崩堆栈或静默输出空表。"""
    for bad in ("0", "-1"):
        reset_log()
        proc = run_cli("--limit", bad)
        if proc.returncode != 2:
            return False, f"--limit {bad} 退出码 {proc.returncode}，应是 2（参数非法）"
        if has_traceback(proc):
            return False, f"--limit {bad} 打出了 Traceback —— 用 parser.error() 报错，别让异常冒出去"
        err = (proc.stderr or "").lower()
        if "unrecognized" in err:
            return False, f"--limit {bad} 被当成未知参数了 —— 说明 --limit 还没加进 argparse"
        if "limit" not in err and "n" not in err:
            return False, f"--limit {bad} 报错信息里没提 limit，用户不知道错在哪：{err.strip()[:80]}"
    return True, "--limit 0 / -1 都被拦下：退出码 2 + 明确提示，无堆栈"


def check_verbose_on() -> tuple[bool, str]:
    """带 --verbose 时日志里要有 DEBUG。"""
    reset_log()
    proc = run_cli("--verbose")
    if proc.returncode != 0:
        return False, f"--verbose 退出码 {proc.returncode}，应该 0"
    if "| DEBUG" not in log_text():
        return False, "带 --verbose 时 run.log 里没有 DEBUG —— 级别没切到 DEBUG，或没写 log.debug(...)"
    return True, "--verbose -> 日志出现 DEBUG 级别"


def check_verbose_off() -> tuple[bool, str]:
    """不带 --verbose 时不能有 DEBUG（默认 INFO）。"""
    reset_log()
    proc = run_cli()
    if proc.returncode != 0:
        return False, f"不带参数时退出码 {proc.returncode}，应该 0"
    if "| DEBUG" in log_text():
        return False, "不带 --verbose 也打了 DEBUG —— 级别应该保持 INFO"
    return True, "默认只到 INFO，DEBUG 被正确过滤"


def check_combo() -> tuple[bool, str]:
    """【加练】--device 与 --limit 组合不崩，且 --limit 只影响输出条数。"""
    reset_log()
    proc = run_cli("--device", NAMES[0], "--limit", "1")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return False, f"--device {NAMES[0]} --limit 1 退出码 {proc.returncode}；{tail[-1] if tail else ''}"
    shown = printed_devices(proc.stdout or "")
    if shown != [NAMES[0]]:
        return False, f"应只输出 {NAMES[0]}，实际 {shown}"
    return True, "--device 与 --limit 可以组合使用"


CHECKS: list[tuple[str, str, Callable[[], tuple[bool, str]], bool]] = [
    ("C1 --help 三个参数", "都能在帮助里看到", check_help, True),
    ("C2 --limit 生效", "--limit 2 只输出 2 台", check_limit_basic, True),
    ("C3 --limit 超界", "超出总数时全量输出", check_limit_over, True),
    ("C4 --limit 非法值", "0 / -1 明确报错，退出码 2", check_limit_invalid, True),
    ("C5 --verbose 开", "日志出现 DEBUG", check_verbose_on, True),
    ("C6 --verbose 关", "默认只到 INFO", check_verbose_off, True),
    ("C7 加练·参数组合", "--device + --limit 一起用", check_combo, False),
]


def main() -> int:
    print("=" * 64)
    print("D3 验收：argparse 加 --limit / --verbose")
    print("=" * 64)

    passed = required_failed = optional_failed = 0
    for label, detail, fn, required in CHECKS:
        try:
            ok, msg = fn()
        except Exception as exc:  # noqa: BLE001 - 任何意外都算失败，别让验收脚本崩
            ok, msg = False, f"验收时抛异常：{type(exc).__name__}: {exc}"
        mark = "✅" if ok else ("❌" if required else "⚠️ ")
        tag = "" if required else "　[加练]"
        print(f"\n{mark} {label}{tag}")
        print(f"    {detail}")
        if not ok:
            print(f"    → {msg}")
        if ok:
            passed += 1
        elif required:
            required_failed += 1
        else:
            optional_failed += 1

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}　必做失败 {required_failed}　加练失败 {optional_failed}")
    print(
        "结论：必做项还没全过 —— 先跑 python loader.py --help，看你的参数加进去没有。"
        if required_failed
        else "结论：全绿。D3 完成，明天进 D4（拆成 models.py + cli.py）。"
    )
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
