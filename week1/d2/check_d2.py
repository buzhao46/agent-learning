"""D2 自动验收：跑它就知道过没过。

    cd week1/d2
    python check_d2.py

退出码 0 = 必做项全过；非 0 = 还有 ❌。
每次运行都会先删掉 out/run.log，保证看到的是这一次的结果。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).parent
LOADER = ROOT / "loader.py"
LOG_FILE = ROOT / "out" / "run.log"
TMP_COUNT = ROOT / "_tmp_count.json"


# ---------------------------------------------------------------- 工具
def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 否则 Windows 下 ✓ 这类字符会 UnicodeEncodeError
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


def levels_in_log() -> set[str]:
    text = log_text()
    return {lv for lv in ("DEBUG", "INFO", "WARNING", "ERROR") if f"| {lv}" in text or f"| {lv:<8}" in text}


def load_tree() -> ast.Module:
    return ast.parse(LOADER.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 检查项
def check_setup_logger() -> tuple[bool, str]:
    """setup_logger() 能返回 logger，且挂上了指向 out/run.log 的文件 handler。

    在子进程里做：如果在验收进程内 import loader，logger 的 FileHandler 会一直占着
    out/run.log，Windows 下后续 reset_log() 删不掉文件（PermissionError）。
    """
    code = (
        "import json, logging, sys\n"
        "sys.path.insert(0, '.')\n"
        "import loader\n"
        "lg = loader.setup_logger()\n"
        "print(json.dumps({\n"
        "    'name': getattr(lg, 'name', ''),\n"
        "    'is_logger': isinstance(lg, logging.Logger),\n"
        "    'level': getattr(lg, 'level', -1),\n"
        "    'files': [h.baseFilename for h in getattr(lg, 'handlers', [])\n"
        "              if isinstance(h, logging.FileHandler)],\n"
        "    'streams': len([h for h in getattr(lg, 'handlers', [])\n"
        "                    if isinstance(h, logging.StreamHandler)]),\n"
        "}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env(),
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "NotImplementedError" in err:
            return False, "setup_logger() 还是 NotImplementedError —— 按 docstring 的 7 步补上"
        tail = err.splitlines()[-1] if err else "(无输出)"
        return False, f"setup_logger() 调用失败：{tail}"

    try:
        info = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, f"setup_logger() 没有返回可序列化的 logger：{proc.stdout!r}"

    if not info["is_logger"]:
        return False, "setup_logger() 应返回 logging.Logger"
    if not info["files"]:
        return False, "logger 上没有 FileHandler —— 第 6 步没写，日志不会落文件"
    if Path(info["files"][0]).resolve() != LOG_FILE.resolve():
        return False, f"日志文件路径不对：{info['files'][0]}，期望 {LOG_FILE}"
    if info["streams"] < 1:
        return False, "logger 上没有 StreamHandler —— 第 5 步没写，屏幕上看不到日志"
    if info["level"] > 20:  # logging.INFO
        return False, f"logger.level={info['level']} 太高，log.info 会被过滤 —— 第 3 步 setLevel"
    return True, f"logger={info['name']}，handler={len(info['files']) + info['streams']} 个（文件+控制台）"


def check_info_level() -> tuple[bool, str]:
    """正常跑一次，日志文件里应有 INFO，且表格仍在 stdout。"""
    reset_log()
    proc = run_cli()
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        reason = tail[-1] if tail else "(stderr 为空)"
        return False, f"正常路径退出码应为 0，实际 {proc.returncode}；stderr 末行：{reason}"
    if "共 3 台" not in proc.stdout:
        return False, "stdout 里没有表格 —— print_table() 的 print 是不是被误删了？"
    if "INFO" not in log_text():
        extra = "" if log_text() else "（out/run.log 根本没生成，看第 6 步）"
        return False, f"out/run.log 里没有 INFO 记录{extra}"
    return True, "out/run.log 有 INFO；表格仍走 stdout（报告与日志已分离）"


def check_warning_level() -> tuple[bool, str]:
    """count 与设备数对不上 -> WARNING，且程序不崩。"""
    data = json.loads((ROOT / "devices.json").read_text(encoding="utf-8"))
    data["count"] = 99
    TMP_COUNT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        reset_log()
        proc = run_cli("--file", TMP_COUNT.name)
        if "NameError" in proc.stderr:
            return False, "NameError: still 有地方用了未定义的 log —— 用 get_log() 取 logger"
        if "WARNING" not in log_text():
            return False, "count 不一致时日志里没有 WARNING"
        return True, "count 不一致 -> WARNING，程序继续跑完"
    finally:
        TMP_COUNT.unlink(missing_ok=True)


def check_error_level() -> tuple[bool, str]:
    """文件不存在 -> ERROR + 退出码 2。"""
    reset_log()
    proc = run_cli("--file", "nope.json")
    if proc.returncode != 2:
        return False, f"文件不存在时退出码应为 2，实际 {proc.returncode}"
    if "ERROR" not in log_text():
        return False, "文件不存在时 out/run.log 里没有 ERROR（第1/2处 TODO 还没改？）"
    return True, "文件不存在 -> ERROR + 退出码 2"


def check_three_levels() -> tuple[bool, str]:
    """把三次运行的结果合起来，日志文件里应出现 3 个级别。"""
    seen: set[str] = set()
    reset_log()
    run_cli()
    seen |= levels_in_log()

    data = json.loads((ROOT / "devices.json").read_text(encoding="utf-8"))
    data["count"] = 99
    TMP_COUNT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        reset_log()
        run_cli("--file", TMP_COUNT.name)
        seen |= levels_in_log()
    finally:
        TMP_COUNT.unlink(missing_ok=True)

    reset_log()
    run_cli("--file", "nope.json")
    seen |= levels_in_log()

    need = {"INFO", "WARNING", "ERROR"}
    if not need <= seen:
        return False, f"缺少级别：{sorted(need - seen)}（已有 {sorted(seen) or '无'}）"
    return True, "日志里三个级别都齐了：INFO / WARNING / ERROR"


def check_no_process_print() -> tuple[bool, str]:
    """print 只允许出现在 print_table() 里，过程信息必须走日志。"""
    tree = load_tree()
    bad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "print_table":
            continue
        # 把 print_table 里所有 print 调用行号记下来
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "print":
                bad.append(sub.lineno)
    allowed = set(bad)

    leftover: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            and node.lineno not in allowed
        ):
            leftover.append(node.lineno)
    if leftover:
        lines = ", ".join(str(n) for n in sorted(leftover))
        return False, f"还有 {len(leftover)} 处 print 没换（第 {lines} 行）—— 找到 TODO(你·第N处)"
    return True, "过程信息已全部走日志，print 只留在 print_table()"


def check_no_fstring_log() -> tuple[bool, str]:
    """【加练】日志参数应该用 %s 占位，而不是 f-string。"""
    tree = load_tree()
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("debug", "info", "warning", "error", "exception", "critical"):
            continue
        if any(isinstance(a, ast.JoinedStr) for a in node.args):
            offenders.append(node.lineno)
    if offenders:
        lines = ", ".join(str(n) for n in sorted(offenders))
        return False, f"第 {lines} 行用 f-string 传日志 —— 改成 log.info(\"... %s\", value)"
    return True, "日志全部用 %s 占位，级别过滤时不会白拼字符串"


CHECKS: list[tuple[str, str, Callable[[], tuple[bool, str]], bool]] = [
    ("C1 setup_logger 可用", "返回 logger，挂好文件 handler", check_setup_logger, True),
    ("C2 INFO 落地", "正常跑一次，run.log 有 INFO", check_info_level, True),
    ("C3 WARNING 落地", "count 不一致，run.log 有 WARNING", check_warning_level, True),
    ("C4 ERROR 落地", "文件不存在，run.log 有 ERROR", check_error_level, True),
    ("C5 三个级别齐全", "日志文件里能看到 3 个级别", check_three_levels, True),
    ("C6 print 已清空", "过程信息不再用 print", check_no_process_print, True),
    ("C7 加练·%s 占位", "日志不用 f-string", check_no_fstring_log, False),
]


def main() -> int:
    print("=" * 64)
    print("D2 验收：print -> logging（分 3 级 + 落盘 out/run.log）")
    print("=" * 64)

    passed = required_failed = optional_failed = 0
    for label, detail, fn, required in CHECKS:
        try:
            ok, msg = fn()
        except Exception as exc:  # noqa: BLE001 - 任何意外都算失败，不能让验收脚本崩
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
        "结论：必做项还没全过，接着改 —— 先跑 python loader.py 看它报什么。"
        if required_failed
        else "结论：全绿。D2 完成，明天进 D3（加 --limit / --verbose 参数）。"
    )
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
