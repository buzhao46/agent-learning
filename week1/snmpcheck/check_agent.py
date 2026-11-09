"""agent 子命令验收：真 pysnmp 采集 + 决策/解读 全链路跑通。

验证：
  A1  agent 子命令退出码 0
  A2  输出含「模式：」（说明进入了主循环，真采集后端已就绪）
  A3  至少发生了采集动作（trace 含 ACTION / 或 stdout 出现「采集完成」）
  A4  输出了最终诊断（含「诊断（解读层输出）」且诊断非空）
  A5  [加练] 多台设备（--ports 1161 1162）也能完整跑

无 LLM_API_KEY 时走 RulePolicy 兜底，仍能全绿；有 Key 时走 LLM（此处只验链路，不验模型质量）。
关键：验收把 LLM_API_KEY 清空，保证「规则链路」可被稳定复现。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"


def interpreter() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def make_env() -> dict[str, str]:
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONUTF8"] = "1"
    e["PYTHONPATH"] = str(ROOT) + os.pathsep + e.get("PYTHONPATH", "")
    e["LLM_API_KEY"] = ""  # 强制走规则兜底，验收不依赖外部 Key
    # 让 coverage 能追踪子进程（coverage run 时透传 COVERAGE_PROCESS_START）
    if "COVERAGE_PROCESS_START" in os.environ:
        e["COVERAGE_PROCESS_START"] = os.environ["COVERAGE_PROCESS_START"]
    return e


def run(args: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter(), "-m", "snmpcheck.snmpcheck", *args],
        cwd=str(ROOT.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=timeout,
    )


def tail(text: str | None, n: int = 800) -> str:
    body = (text or "").strip()
    if not body:
        return "(空)"
    return "\n".join(body.splitlines()[-12:])[:n]


# ============================================================ 检查
def check_a1() -> tuple[bool, str]:
    proc = run(["agent"])
    if proc.returncode != 0:
        return False, f"agent 退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"
    return True, "agent 退出码 0"


def check_a2() -> tuple[bool, str]:
    proc = run(["agent"])
    blob = (proc.stdout or "") + (proc.stderr or "")
    if "模式：" not in blob:
        return False, f"输出里没有「模式：」，主循环可能没进入\n{tail(blob)}"
    return True, "输出含「模式：」（进入主循环，真采集后端就绪）"


def check_a3() -> tuple[bool, str]:
    proc = run(["agent"])
    blob = (proc.stdout or "") + (proc.stderr or "")
    if "ACTION" not in blob and "采集完成" not in blob and "OBSERVE" not in blob:
        return False, f"看起来没有发生任何采集动作\n{tail(blob)}"
    return True, "至少发生了采集动作（ACTION / OBSERVE / 采集完成）"


def check_a4() -> tuple[bool, str]:
    proc = run(["agent"])
    if "诊断（解读层输出）：" not in (proc.stdout or ""):
        return False, f"没有输出最终诊断\n{tail(proc.stdout)}"
    # 诊断后应紧跟非空正文（跳过分隔线）
    after = (proc.stdout or "").split("诊断（解读层输出）：", 1)[1]
    lines = [ln for ln in after.splitlines() if ln.strip() and not set(ln.strip()) <= set("=-")]
    if not lines:
        return False, "诊断标题存在但没有正文"
    return True, f"输出了最终诊断（首行：{lines[0][:60]}）"


def check_a5() -> tuple[bool, str]:
    proc = run(["agent", "--ports", "1161", "1162"])
    if proc.returncode != 0:
        return False, f"多台设备 agent 退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"
    blob = (proc.stdout or "") + (proc.stderr or "")
    if "诊断（解读层输出）：" not in blob:
        return False, f"多台设备没有输出诊断\n{tail(blob)}"
    return True, "多台设备（1161+1162）也能完整跑出诊断"


CHECKS: list[tuple[str, str, object, bool]] = [
    ("A1", "agent 退出码 0", check_a1, True),
    ("A2", "输出含「模式：」进入主循环", check_a2, True),
    ("A3", "至少发生一次采集动作", check_a3, True),
    ("A4", "输出非空最终诊断", check_a4, True),
    ("A5", "多台设备也能跑", check_a5, False),
]


def main() -> int:
    print("=" * 64)
    print("agent 子命令验收：真 pysnmp 采集 + 决策/解读 全链路")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    passed = required_failed = optional_failed = 0
    for label, desc, fn, required in CHECKS:
        print(f"\n{label}  {desc}" + ("" if required else "　[加练]"))
        try:
            good, message = fn()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - 验收脚本自己绝不能崩
            good, message = False, f"验收时抛异常：{type(exc).__name__}: {exc}"
        print(f"    {'✅' if good else '❌'} {message}")
        if good:
            passed += 1
        elif required:
            required_failed += 1
        else:
            optional_failed += 1

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}　必做失败 {required_failed}　加练失败 {optional_failed}")
    if required_failed:
        print("结论：还没全绿，先过 A1–A4。")
    else:
        print("结论：全绿。snmpcheck 的 LLM Agent 子命令（真采集 + 决策 + 解读）跑通了。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
