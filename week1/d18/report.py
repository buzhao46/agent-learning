from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
_PRIORITY = {"OK": 0, "DEGRADED": 1, "FAILED": 2}
_EXIT = {"OK": 0, "DEGRADED": 2, "FAILED": 1}


def summarize(devices: list[dict]) -> dict:
    ok = sum(1 for d in devices if d["verdict"] == "OK")
    degraded = sum(1 for d in devices if d["verdict"] == "DEGRADED")
    failed = sum(1 for d in devices if d["verdict"] == "FAILED")
    worst = max((d["verdict"] for d in devices), key=lambda v: _PRIORITY[v], default="OK")
    return {
        "total": len(devices),
        "ok": ok,
        "degraded": degraded,
        "failed": failed,
        "exit_code": _EXIT[worst],
    }


def build_payload(devices: list[dict]) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(devices),
        "devices": devices,
    }


def write_json_atomic(path: Path, payload: dict) -> Path:
    """先写 .tmp 再 os.replace —— 产物永远完整，不会出现半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def save_report(payload: dict, out_dir: str = "out") -> Path:
    # 秒级时间戳时，同一秒内的两次扫描会落到同一个文件名，后一次原子替换掉前一次
    # —— 历史直接丢一份。D16 要做趋势，历史不能被覆盖。
    #
    # 光加毫秒还不够：Windows 的系统时钟粒度约 15.6ms，连续两次调用完全可能拿到
    # 同一个毫秒。所以再兜一层「名字已被占用就加序号」，保证最终路径唯一。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    out = Path(out_dir)
    path = out / f"scan_{stamp}.json"
    seq = 1
    while path.exists():
        path = out / f"scan_{stamp}-{seq:03d}.json"
        seq += 1
    return write_json_atomic(path, payload)


def load_last_report(out_dir: str = "out") -> dict | None:
    files = sorted(Path(out_dir).glob("scan_*.json"))
    if not files:
        return None
    latest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        with latest.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[警告] 上一份报告读取失败，按全量处理：{exc}")
        return None
