"""D6 验收：重试与计时。

七项：
  C1  Timer 能测出真实耗时
  C2  Timer 在异常路径上也计时，且不吞异常
  C3  retry_call 首次成功只调 1 次
  C4  retry_call 失败一次后成功，返回结果
  C5  retry_call 耗尽次数后抛出**最后一次**的异常
  C6  cli 输出每台设备都有耗时（今天的验收标准）
  C7  [加练] --attempts 是「总尝试次数」，不是「额外重试次数」
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEVICES_FILE = ROOT / "devices.json"

# 理想情况下用 week1/.venv 里的 python（D19 之后要装依赖，统一在 venv 里跑更省事）
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"


def interpreter() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def make_env() -> dict[str, str]:
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONUTF8"] = "1"
    # 让临时目录里的检查脚本也能 import 到 resilience / probe / models
    e["PYTHONPATH"] = str(ROOT) + os.pathsep + e.get("PYTHONPATH", "")
    return e


def run(args: list[str], *, cwd: Path = ROOT, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter(), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=timeout,
    )


def tail(text: str | None, n: int = 500) -> str:
    """报错时只给最后几行，一屏放得下才有用。"""
    body = (text or "").strip()
    if not body:
        return "(空)"
    lines = body.splitlines()
    return "\n".join(lines[-8:])[:n]


def device_names() -> list[str]:
    data = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    return [d["name"] for d in data["devices"]]


# ============================================================ 准备检查
def ready() -> tuple[bool, str]:
    need = ("resilience.py", "probe.py", "cli.py", "models.py", "log_setup.py")
    missing = [n for n in need if not (ROOT / n).exists()]
    if missing:
        return False, f"缺文件：{', '.join(missing)}"
    src = (ROOT / "resilience.py").read_text(encoding="utf-8")
    if re.search(r"^\s*raise NotImplementedError", src, re.M):
        return False, "resilience.py 里还有 raise NotImplementedError —— 骨架占位还没换成真正的实现"
    return True, "五个文件就位，resilience.py 已无占位"


# ============================================================ 子进程内跑 Timer / retry_call
# 为什么不在本进程里直接 import：她的实现可能写出死循环、疯狂重试、往 stdout 打东西。
# 放到子进程里跑，最坏情况只是这一项失败，验收脚本自己不会跟着崩。
_PROBE_SCRIPT = textwrap.dedent(
    '''
    import json
    import sys
    import time

    out = {}


    def err(exc):
        return f"{type(exc).__name__}: {exc}"


    try:
        from resilience import Timer, retry_call
    except Exception as exc:
        print(json.dumps({"import_error": err(exc)}))
        sys.exit(0)


    # ---------------- C1：正常路径计时 ----------------
    try:
        t = Timer()
        with t:
            time.sleep(0.06)
        out["c1"] = {
            "ok": 0.05 <= t.elapsed < 1.0,
            "elapsed": t.elapsed,
        }
    except Exception as exc:
        out["c1"] = {"ok": False, "msg": err(exc)}

    # ---------------- C2：异常路径也要计时，且异常不能被吃掉 ----------------
    try:
        t2 = Timer()
        caught = False
        try:
            with t2:
                time.sleep(0.03)
                raise ValueError("故意抛的")
        except ValueError:
            caught = True
        out["c2"] = {
            "ok": caught and t2.elapsed >= 0.02,
            "caught": caught,
            "elapsed": t2.elapsed,
        }
    except Exception as exc:
        out["c2"] = {"ok": False, "msg": err(exc)}

    # ---------------- C3：一次就成功，不该重试 ----------------
    try:
        calls = []

        def ok_once():
            calls.append(1)
            return "yes"

        result = retry_call(ok_once, attempts=3, delay=0.01)
        out["c3"] = {"ok": result == "yes" and len(calls) == 1, "calls": len(calls), "result": result}
    except Exception as exc:
        out["c3"] = {"ok": False, "msg": err(exc)}

    # ---------------- C4：失败一次后成功，要返回结果 ----------------
    try:
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("第一次失败")
            return "second"

        result = retry_call(flaky, attempts=3, delay=0.01)
        out["c4"] = {"ok": result == "second" and len(calls) == 2, "calls": len(calls), "result": result}
    except Exception as exc:
        out["c4"] = {"ok": False, "msg": err(exc)}

    # ---------------- C5：一直失败，要抛最后一次的异常 ----------------
    try:
        calls = []

        def always_fail():
            calls.append(1)
            raise RuntimeError(f"第{len(calls)}次失败")

        msg = None
        wrong_type = None
        try:
            retry_call(always_fail, attempts=3, delay=0.001)
        except RuntimeError as exc:
            msg = str(exc)
        except Exception as exc:
            wrong_type = type(exc).__name__

        out["c5"] = {
            "ok": wrong_type is None and len(calls) == 3 and msg is not None and "第3次" in msg,
            "calls": len(calls),
            "msg": msg,
            "wrong_type": wrong_type,
        }
    except Exception as exc:
        out["c5"] = {"ok": False, "msg": err(exc)}

    print(json.dumps(out, ensure_ascii=False))
    '''
)

_probe_cache: dict[str, object] | None = None


def probe_results() -> dict:
    global _probe_cache  # noqa: PLW0603 - 一次验收只跑一次子进程，缓存结果即可
    if _probe_cache is not None:
        return _probe_cache

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "_d6_probe.py"
        script.write_text(_PROBE_SCRIPT, encoding="utf-8")
        proc = run([str(script)], cwd=ROOT, timeout=90)

    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        _probe_cache = {
            "fatal": f"检查脚本没有任何输出。退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"
        }
        return _probe_cache
    try:
        _probe_cache = json.loads(lines[-1])
    except json.JSONDecodeError:
        _probe_cache = {
            "fatal": f"检查脚本最后一行不是 JSON：\n{lines[-1][:300]}\nstderr：\n{tail(proc.stderr)}"
        }
    return _probe_cache


def need(key: str) -> tuple[bool, str, dict]:
    data = probe_results()
    if "fatal" in data:
        return False, str(data["fatal"]), {}
    if "import_error" in data:
        return False, f"resilience.py 导入失败：{data['import_error']}", {}
    item = data.get(key)
    if not isinstance(item, dict):
        return False, f"没有拿到 {key} 的结果", {}
    return bool(item.get("ok")), "", item


# ============================================================ 七项检查
def check_c1() -> tuple[bool, str]:
    ok, msg, item = need("c1")
    if msg:
        return False, msg
    if not ok:
        got = item.get("elapsed")
        if "msg" in item:
            return False, f"跑 Timer 出错：{item['msg']}"
        return False, (
            f"sleep(0.06) 后 elapsed={got!r}，期望 0.05~1.0 之间。"
            "常见原因：__exit__ 里没算 elapsed，或者用了错误的时间单位（毫秒当秒）"
        )
    return True, f"sleep 0.06 → 测出 {float(item['elapsed']) * 1000:.1f}ms"


def check_c2() -> tuple[bool, str]:
    ok, msg, item = need("c2")
    if msg:
        return False, msg
    if not ok:
        if "msg" in item:
            return False, f"跑 Timer 出错：{item['msg']}"
        if not item.get("caught"):
            return False, (
                "with 块里抛出的 ValueError 没有被你在外面接住 —— "
                "说明 __exit__ 返回了 True，把异常吞掉了。__exit__ 应该返回 None（即 False）让异常继续传播"
            )
        return False, f"异常路径上 elapsed={item.get('elapsed')!r}，期望 >= 0.02。__exit__ 里要无条件算耗时，不能只在成功时算"
    return True, "异常照常抛出，同时记下了耗时"


def check_c3() -> tuple[bool, str]:
    ok, msg, item = need("c3")
    if msg:
        return False, msg
    if not ok:
        if "msg" in item:
            return False, f"调用 retry_call 出错：{item['msg']}"
        return False, (
            f"首次就成功的函数被调用了 {item.get('calls')} 次，返回值 {item.get('result')!r}。"
            "期望：只调 1 次、原样返回结果"
        )
    return True, "首次成功 → 调用 1 次，结果原样返回"


def check_c4() -> tuple[bool, str]:
    ok, msg, item = need("c4")
    if msg:
        return False, msg
    if not ok:
        if "msg" in item:
            return False, f"调用 retry_call 出错：{item['msg']}"
        return False, (
            f"第 2 次才成功的函数：调用 {item.get('calls')} 次、返回值 {item.get('result')!r}。"
            "期望：调用 2 次、返回 'second'。注意重试成功后要 return 返回值，不能只是不抛异常就算完"
        )
    return True, "第 2 次成功 → 调用 2 次，返回 'second'"


def check_c5() -> tuple[bool, str]:
    ok, msg, item = need("c5")
    if msg:
        return False, msg
    if not ok:
        if "msg" in item:
            return False, f"调用 retry_call 出错：{item['msg']}"
        if item.get("wrong_type"):
            return False, f"抛出的异常类型变成了 {item['wrong_type']}，应该原样抛出最后一次的 RuntimeError"
        calls, got = item.get("calls"), item.get("msg")
        if calls != 3:
            return False, (
                f"attempts=3 时函数被调用了 {calls} 次，期望 3 次。"
                "attempts 是「总共试几次」，不是「额外重试几次」。"
                "写成 range(attempts + 1) 或「先试1次再重试attempts次」都会变成 4 次，这就是最常见的 off-by-one"
            )
        return False, (
            f"抛出的异常消息是 {got!r}，里面没有「第3次」。"
            "说明你抛的是第一次的异常；应该保留最后一次的（用 last_exc 一路记下来，循环结束后 raise last_exc）"
        )
    return True, "耗尽 3 次后抛出「第3次失败」，调用次数正好 3"


def check_c6() -> tuple[bool, str]:
    proc = run(["cli.py"])
    if proc.returncode != 0:
        return False, f"python cli.py 退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"

    names = device_names()
    rows = [ln for ln in proc.stdout.splitlines() if any(ln.startswith(n) for n in names)]
    if len(rows) != len(names):
        return False, (
            f"表格里应有 {len(names)} 行设备，实际识别到 {len(rows)} 行。\n"
            "完整 stdout：\n" + tail(proc.stdout, 800)
        )

    no_ms = [r for r in rows if not re.search(r"\d+\.\d+ms", r)]
    if no_ms:
        return False, "下面这些设备行没有耗时列：\n" + "\n".join(no_ms) + "\n（main 里要把 {设备名: 秒数} 传给 print_table 的第二个参数）"

    values = []
    for row in rows:
        m = re.search(r"([\d.]+)ms", row)
        assert m is not None  # noqa: S101 - 上一步已经确认每行都有
        value = float(m.group(1))
        if value <= 0:
            return False, f"耗时算出来是 {value}ms，应该 > 0。检查 Timer 的 elapsed 是不是没真正计算"
        values.append(value)

    return True, "三台设备耗时：" + " / ".join(f"{v:.1f}ms" for v in values)


def check_c7() -> tuple[bool, str]:
    """加练：验证 --attempts 的语义是「总尝试次数」。"""
    counted: dict[int, int] = {}
    for attempts in (1, 2, 3):
        proc = run(["cli.py", "--attempts", str(attempts)])
        if "unrecognized arguments" in (proc.stderr or ""):
            return False, "cli.py 还不认识 --attempts 参数（第 3 步要加，注意别写成 --attempt）"
        if proc.returncode != 0:
            return False, f"--attempts {attempts} 退出码 {proc.returncode}\nstderr：\n{tail(proc.stderr)}"
        counted[attempts] = (proc.stderr or "").count("采集失败")

    expect = {1: 2, 2: 1, 3: 0}
    if counted != expect:
        detail = "、".join(f"attempts={k} → {v} 台失败" for k, v in counted.items())
        guess = ""
        if counted.get(1) == 1:
            guess = "\n看起来 attempts=1 时第 2 台设备就成功了 —— 说明你把 attempts 当成「额外重试次数」（共试 2 次）。"
        elif counted.get(3) not in (0, None):
            guess = "\nattempts=3 时还有设备失败 —— 检查 collect_all 拿到的 attempts 是不是被写死成默认值了。"
        return False, f"期望 attempts 1/2/3 分别失败 2/1/0 台，实际：{detail}{guess}"

    return True, "attempts 1→2 台失败、2→1 台、3→0 台，语义是「总尝试次数」"


CHECKS: list[tuple[str, str, object, bool]] = [
    ("C1", "Timer 能测出真实耗时", check_c1, True),
    ("C2", "Timer 异常路径也计时", check_c2, True),
    ("C3", "retry_call 首发命中不重试", check_c3, True),
    ("C4", "retry_call 失败一次后成功", check_c4, True),
    ("C5", "retry_call 耗尽抛最后一次异常", check_c5, True),
    ("C6", "cli 每台设备都有耗时", check_c6, True),
    ("C7", "--attempts 是总尝试次数", check_c7, False),
]


def main() -> int:
    print("=" * 64)
    print("D6 验收：重试与计时")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    ok_ready, info = ready()
    print(f"准备：{info}" if ok_ready else f"准备：⚠️  {info}")

    passed = required_failed = optional_failed = 0
    for label, desc, fn, required in CHECKS:
        print(f"\n{label}  {desc}" + ("" if required else "　[加练]"))
        if not ok_ready and label in ("C1", "C2", "C3", "C4", "C5"):
            print("    ⏭  跳过（准备检查没过）")
            if required:
                required_failed += 1
            else:
                optional_failed += 1
            continue
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
        print("结论：还没到 7/7。C1–C5 是纯函数级验证，先把它们弄绿，C6/C7 才好查。")
    else:
        print("结论：全绿。Timer 和 retry_call 都能用了，而且 attempts 的语义没踩 off-by-one。D6 完成。")
    return 0 if required_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
