"""D16 验收：多份报告的趋势与降噪告警。

B1  聚合正确：3 份报告 × 3 台设备 → 每台 samples=3，ok/degraded/failed 计数一致
B2  连续失败计数：从尾部往回数，遇到相反状态就停（OK,DEGRADED,FAILED → 2）
B3  降噪：同一份数据，阈值 2 → ALERT 退出码 1；阈值 3 → OK 退出码 0
B4  重启检测：uptime_ticks 变小 → restarted，跨重启不给 delta；缺值样本跳过不崩
B5  健壮性：坏 JSON 跳过、旧报告（无 port）按 161 归组，不抛异常
B6  历史不被覆盖：同一秒内两次 save_report 产出两个文件

说明：本脚本**不启动 SNMP**，直接构造 scan_*.json 喂给 trend.py。
趋势分析的正确性不该依赖「假设备能不能采到」——那是 D15 的事。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "trend.py"
VENV_PY = ROOT.parent / ".venv" / "Scripts" / "python.exe"

# 所有临时产物放 out/ 下（已在 .gitignore），用 mkdtemp 避免依赖递归删除。
(ROOT / "out").mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="_d16check_", dir=str(ROOT / "out")))

EXIT_OF = {"OK": 0, "DEGRADED": 2, "FAILED": 1}


def interpreter() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def make_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def run_trend(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter(), str(ENTRY), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=make_env(),
        timeout=timeout,
    )


def tail(text: str, n: int = 900) -> str:
    body = (text or "").strip()
    return "\n".join(body.splitlines()[-14:])[:n] if body else "(空)"


def device(
    host: str, port: int, verdict: str, uptime: int | None = None, if_count: int = 3
) -> dict:
    """一台设备的报告片段。结构与 probe.result_to_dict 保持一致。"""
    return {
        "host": host,
        "port": port,
        "verdict": verdict,
        "exit_code": EXIT_OF[verdict],
        "uptime_ticks": uptime,
        "values": {"ifTable": if_count, "sysName": "fake"},
        "failures": []
        if verdict == "OK"
        else [{"stage": "sysDescr", "kind": "timeout", "detail": "假失败", "hint": "检查链路"}],
    }


def summarize(devices: list[dict]) -> dict:
    verdicts = [d["verdict"] for d in devices]
    ok, degraded, failed = (
        verdicts.count("OK"),
        verdicts.count("DEGRADED"),
        verdicts.count("FAILED"),
    )
    worst = "FAILED" if failed else ("DEGRADED" if degraded else "OK")
    return {
        "total": len(devices),
        "ok": ok,
        "degraded": degraded,
        "failed": failed,
        "exit_code": EXIT_OF[worst],
    }


def write_report(directory: Path, seq: int, devices: list[dict]) -> Path:
    """造一份报告：generated_at 与文件名同时递增，模拟时间流逝。"""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "generated_at": f"2026-11-03T10:{seq:02d}:00",
        "summary": summarize(devices),
        "devices": devices,
    }
    path = directory / f"scan_20261103-{100000 + seq:06d}-000.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_json(directory: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], dict | None]:
    """跑 trend.py 并读回 --json 产物：断言看结构化数据，不解析终端表格。"""
    out_json = directory / "trend.json"
    proc = run_trend("--report-dir", str(directory), "--json", str(out_json), *args)
    data = json.loads(out_json.read_text(encoding="utf-8")) if out_json.exists() else None
    return proc, data


def by_label(report: dict) -> dict[str, dict]:
    return {f"{t['host']}:{t['port']}": t for t in report["trends"]}


def check_aggregate() -> tuple[bool, str]:
    directory = TMP / "b1"
    for seq in (1, 2, 3):
        write_report(
            directory,
            seq,
            [
                device("127.0.0.1", 1161, "OK", uptime=1000 + seq * 100),
                device(
                    "127.0.0.1", 1162, "OK" if seq != 2 else "DEGRADED", uptime=2000 + seq * 100
                ),
                device("10.0.0.9", 161, "FAILED"),
            ],
        )

    proc, data = run_json(directory, "--threshold", "3")
    out = (proc.stdout or "") + (proc.stderr or "")
    if data is None:
        return False, f"未生成趋势 JSON\n{tail(out)}"

    problems = []
    trends = by_label(data)
    if len(trends) != 3:
        problems.append(f"设备数 {len(trends)} != 3（host 与 port 是否当成同一台了？）")
    for label, trend in trends.items():
        if trend["samples"] != 3:
            problems.append(f"{label} samples={trend['samples']} != 3")
    if trends.get("127.0.0.1:1162", {}).get("degraded") != 1:
        problems.append("1162 的 degraded 计数不是 1")
    if trends.get("10.0.0.9:161", {}).get("failed") != 3:
        problems.append("10.0.0.9 的 failed 计数不是 3")
    if data["summary"]["total"] != 3:
        problems.append(f"summary.total={data['summary']['total']} != 3")
    if "127.0.0.1:1162" not in out:
        problems.append("终端表格里没有设备行")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, f"3 份报告聚合成 3 台设备，样本与计数一致（表格 {len(out.splitlines())} 行）"


def check_consecutive() -> tuple[bool, str]:
    directory = TMP / "b2"
    # D：OK → DEGRADED → FAILED → FAILED，从尾部数是 3 个非 OK
    # E：FAILED → OK → OK → OK，从尾部数是 3 个 OK
    sequence = [("OK", "FAILED"), ("DEGRADED", "OK"), ("FAILED", "OK"), ("FAILED", "OK")]
    for seq, (verdict_d, verdict_e) in enumerate(sequence, start=1):
        write_report(
            directory,
            seq,
            [
                device("10.0.0.10", 161, verdict_d),
                device("10.0.0.11", 161, verdict_e),
            ],
        )

    proc, data = run_json(directory, "--threshold", "1")
    out = (proc.stdout or "") + (proc.stderr or "")
    if data is None:
        return False, f"未生成趋势 JSON\n{tail(out)}"

    problems = []
    trends = by_label(data)
    d = trends.get("10.0.0.10:161", {})
    e = trends.get("10.0.0.11:161", {})
    if d.get("consecutive_bad") != 3:
        problems.append(
            f"10.0.0.10 连续非 OK 应为 3（OK,DEGRADED,FAILED,FAILED），实际 {d.get('consecutive_bad')}"
        )
    if d.get("consecutive_ok") != 0:
        problems.append(f"10.0.0.10 连续 OK 应为 0，实际 {d.get('consecutive_ok')}")
    if e.get("consecutive_bad") != 0:
        problems.append(f"10.0.0.11 连续非 OK 应为 0（已恢复），实际 {e.get('consecutive_bad')}")
    if e.get("consecutive_ok") != 3:
        problems.append(f"10.0.0.11 连续 OK 应为 3，实际 {e.get('consecutive_ok')}")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, "从尾部数连续段：非 OK 3 次、恢复后连续 OK 3 次，均正确"


def check_dedup() -> tuple[bool, str]:
    directory = TMP / "b3"
    # 同一台设备：OK → FAILED → FAILED，末尾连续 2 次非 OK
    for seq, verdict in enumerate(("OK", "FAILED", "FAILED"), start=1):
        write_report(directory, seq, [device("10.0.0.20", 161, verdict)])

    loose, loose_data = run_json(directory, "--threshold", "2")
    strict, strict_data = run_json(directory, "--threshold", "3")
    if loose_data is None or strict_data is None:
        return False, "未生成趋势 JSON"
    loose_trend = loose_data["trends"][0]
    strict_trend = strict_data["trends"][0]

    problems = []
    if loose_trend["status"] != "ALERT" or loose.returncode != 1:
        problems.append(
            f"阈值 2 应 ALERT + 退出码 1，实际 {loose_trend['status']} / {loose.returncode}"
        )
    if strict_trend["status"] != "OK" or strict.returncode != 0:
        problems.append(
            f"阈值 3 应 OK + 退出码 0（连续 2 次未到阈值），实际 {strict_trend['status']} / {strict.returncode}"
        )
    if "未到阈值" not in strict_trend["reason"]:
        problems.append(f"阈值 3 的 reason 没说清为何不告警：{strict_trend['reason']!r}")

    if problems:
        return False, "；".join(problems)
    return True, "同一数据：阈值 2 告警、阈值 3 抑制（连续失败降噪生效）"


def check_restart() -> tuple[bool, str]:
    directory = TMP / "b4"
    # 10.0.0.30：uptime 变小 → 重启；中间夹一条缺值样本，不能被当成 0
    # 10.0.0.31：uptime 单调变大 → 未重启，delta 正常
    write_report(
        directory,
        1,
        [
            device("10.0.0.30", 161, "OK", uptime=12345678),
            device("10.0.0.31", 161, "OK", uptime=1000),
        ],
    )
    write_report(
        directory,
        2,
        [
            device("10.0.0.30", 161, "OK", uptime=5000),
            device("10.0.0.31", 161, "OK", uptime=2000),
        ],
    )
    write_report(
        directory,
        3,
        [
            device("10.0.0.30", 161, "OK", uptime=None),
            device("10.0.0.31", 161, "OK", uptime=4000),
        ],
    )

    proc, data = run_json(directory, "--threshold", "1")
    out = (proc.stdout or "") + (proc.stderr or "")
    if data is None:
        return False, f"未生成趋势 JSON\n{tail(out)}"

    trends = by_label(data)
    restarted = trends.get("10.0.0.30:161", {})
    steady = trends.get("10.0.0.31:161", {})

    problems = []
    if restarted.get("restarted") is not True:
        problems.append(f"10.0.0.30 应检测到重启，实际 restarted={restarted.get('restarted')}")
    if restarted.get("uptime_delta") is not None:
        problems.append(f"跨重启不该给 delta，实际 {restarted.get('uptime_delta')}")
    if restarted.get("uptime_ticks") != 5000:
        problems.append(
            f"10.0.0.30 最新 uptime_ticks 应为 5000（缺值样本跳过），实际 {restarted.get('uptime_ticks')}"
        )
    if steady.get("restarted") is not False:
        problems.append("10.0.0.31 不该被判成重启")
    if steady.get("uptime_delta") != 3000:
        problems.append(
            f"10.0.0.31 delta 应为 3000（1000→4000），实际 {steady.get('uptime_delta')}"
        )

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, "uptime 变小判重启并抑制 delta；缺值样本跳过；单调增长 delta=3000"


def check_robust() -> tuple[bool, str]:
    directory = TMP / "b5"
    # 旧报告：没有 port 字段、没有 uptime_ticks
    legacy = {
        "schema": 1,
        "generated_at": "2026-11-03T09:00:00",
        "summary": {"total": 1, "ok": 1, "degraded": 0, "failed": 0, "exit_code": 0},
        "devices": [
            {
                "host": "10.0.0.9",
                "verdict": "OK",
                "exit_code": 0,
                "values": {"ifTable": 4},
                "failures": [],
            }
        ],
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "scan_20261103-090000.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )
    write_report(directory, 1, [device("10.0.0.9", 161, "OK", uptime=7000)])
    (directory / "scan_20261103-999999.json").write_text("{ 这不是 JSON", encoding="utf-8")

    proc, data = run_json(directory, "--threshold", "1")
    out = (proc.stdout or "") + (proc.stderr or "")
    if data is None:
        return False, f"坏报告把整段历史搞崩了\n{tail(out)}"

    problems = []
    if "跳过坏报告" not in out:
        problems.append("没有提示跳过坏报告")
    trends = by_label(data)
    legacy_trend = trends.get("10.0.0.9:161", {})
    if not legacy_trend:
        problems.append(f"旧报告（无 port）没按 161 归组，实际 keys={list(trends)}")
    elif legacy_trend.get("samples") != 2:
        problems.append(f"旧报告 + 新报告应合成 2 条样本，实际 {legacy_trend.get('samples')}")
    if proc.returncode not in (0, 2):
        problems.append(f"退出码应为 0 或 2，实际 {proc.returncode}")

    if problems:
        return False, "；".join(problems) + "\n" + tail(out)
    return True, "坏 JSON 跳过、旧报告按 (host,161) 归组，未抛异常"


def check_filename_granularity() -> tuple[bool, str]:
    sys.path.insert(0, str(ROOT))
    import report  # noqa: PLC0415 - 为了让 check 能直接验 save_report

    directory = TMP / "b6"
    payload = {"schema": 1, "generated_at": "2026-11-03T10:00:00", "summary": {}, "devices": []}
    # 连做 10 次：Windows 时钟粒度约 15.6ms，必然会撞上同一毫秒，
    # 这正是「毫秒 + 序号兜底」要覆盖的分支。
    paths = [report.save_report(payload, str(directory)) for _ in range(10)]
    names = [p.name for p in paths]
    if len(set(names)) != len(names):
        dup = sorted({n for n in names if names.count(n) > 1})
        return False, f"连续 10 次保存出现重名 {dup[:3]}，历史会被覆盖"
    if not all(p.exists() for p in paths):
        return False, "文件名唯一但有文件没落盘"
    return True, f"连续 10 次保存得到 10 个不同文件（示例 {names[0]}）"


CHECKS = [
    ("B1", "3 份报告聚合成 3 台设备", check_aggregate),
    ("B2", "连续非 OK / 连续 OK 从尾部计数", check_consecutive),
    ("B3", "连续 N 次才告警（降噪）", check_dedup),
    ("B4", "uptime 变小判重启，缺值不误判", check_restart),
    ("B5", "坏报告与旧报告不致命", check_robust),
    ("B6", "报告文件名毫秒级不覆盖", check_filename_granularity),
]


def main() -> int:
    print("=" * 64)
    print("D16 验收：多份报告的趋势与降噪告警")
    print("=" * 64)
    print(f"\n解释器：{interpreter()}")

    missing = [
        name for name in ("trend.py", "history.py", "report.py") if not (ROOT / name).exists()
    ]
    if missing:
        print(f"准备：[!] 缺文件：{', '.join(missing)}")
        return 1
    print("准备：OK")

    passed = 0
    for code, title, func in CHECKS:
        print(f"\n{code}  {title}")
        try:
            ok, detail = func()
        except Exception as exc:  # noqa: BLE001 - 验收脚本：任何异常都记成失败，别中断整轮
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"    [{'OK' if ok else 'NG'}] {detail}")
        passed += 1 if ok else 0

    total = len(CHECKS)
    print("\n" + "-" * 64)
    print(f"通过 {passed}/{total}")
    print("结论：全绿。趋势与降噪跑通了。" if passed == total else "结论：还没全绿。")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
