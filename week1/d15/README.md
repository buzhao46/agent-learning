# D15 · 结果落盘与增量巡检：把屏幕上的报告变成可追溯的文件

**今天一句话**：D14 是「并发扫完，报告打在屏幕上」，今天把 `list[CollectionResult]` **落成带时间戳的 JSON 报告**，并支持**增量巡检**——读回上一份报告，只重扫上次非 OK 的设备，再把结果合并回完整报告。结构化数据的复利继续。

预计 30–40 分钟。新增一个 `report.py`，`probe.py` 的 `scan_config` 增加落盘与增量两个分支。

---

## 一、今天的目录

先把 D14 的代码复制过来：

```powershell
Copy-Item F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d14\*.py F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d15\ -Force
Copy-Item F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d14\devices.json F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d15\ -Force
```

```
week1/d15/
├── probe.py         ← D14 成品，scan_config 增加落盘 + 增量
├── report.py        ← 新增：序列化 / 原子写 / 读回上一份
├── ifwalk.py        ← 原样搬
├── snmpio.py        ← 原样搬
├── fake_agent.py    ← 原样搬
├── devices.json     ← 配置
└── README.md        ← 这份
```

> `out/` 已在 `.gitignore` 里，报告文件不会被误提交。

---

## 二、先看 D14 缺什么

D14 跑完，`CollectionResult` 只活了一次 `render`。程序退出，结果就没了：

- 明天想知道「这台设备是不是一直失败」，没有历史可比。
- 昨天 50 台里 3 台掉了，今天想只重扫那 3 台，得靠人眼从屏幕里挑。
- 想把报告交给别人（或另一个系统），只能截图。

**落盘不是「把 print 换成写文件」**，而是给结果定义一个**契约**：schema 版本、汇总、每台设备的 verdict / 失败类别 / 下一步提示。D12 的结构化，到这一天终于变成文件。

---

## 三、TODO 1：把 CollectionResult 序列化成 dict

在 `probe.py` 里加（它认识 `CollectionResult` / `Failure`，所以放这里，避免循环依赖）：

```python
def result_to_dict(result: CollectionResult) -> dict:
    """一次采集结果 → JSON 可写结构。

    三个要点：
    1. verdict / exit_code 是 property，直接取，别在别处重算；
    2. failures 的 kind 用 .value（ErrorKind 继承 str，但显式转更稳）；
    3. hint 一起写进去 —— D12 说「分类的产出是动作」，落盘后
       别人读 JSON 也能直接看到下一步该干什么。
    """
    return {
        "host": result.host,
        "verdict": result.verdict,
        "exit_code": result.exit_code,
        "values": result.values,
        "failures": [
            {
                "stage": f.stage,
                "kind": f.kind.value,
                "detail": f.detail,
                "hint": f.kind.hint,
            }
            for f in result.failures
        ],
    }
```

> `values` 目前都是字符串（sysDescr / 格式化后的 uptime / sysName），能直接进 JSON。以后若塞了 pysnmp 对象，`json.dump` 会抛 `TypeError: not JSON serializable`——见卡点表。

---

## 四、TODO 2：新增 report.py（汇总 + 原子写 + 读回）

新建 `report.py`。它**只操作 dict，不 import probe**（否则和 `probe → report` 循环依赖）。

```python
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
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return write_json_atomic(Path(out_dir) / f"scan_{stamp}.json", payload)


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
```

三个关键点（和 D11 `save.py` 同一套）：

- `ensure_ascii=False`：否则中文变 `\uXXXX`，人没法看。
- `tmp + os.replace`：原子替换，产物永远能读。
- `schema` 版本号：三个月后打开旧文件，知道字段含义。

---

## 五、TODO 3：scan_config 增加落盘 + 增量

`probe.py` 顶部导入 report 的函数：

```python
from report import build_payload, load_last_report, save_report
```

把 `scan_config` 改成（原来的并发采集逻辑不动）：

```python
def _dev_key(dev: dict) -> tuple[str, int]:
    """设备身份是 (host, port)：同一台机器上的两个 agent 是两台设备。
    旧报告没有 port 字段，按 161 兜底。"""
    try:
        port = int(dev.get("port", 161))
    except (TypeError, ValueError):
        port = 161
    return (str(dev.get("host", "")), port)


def scan_config(path: str, *, workers: int = 16,
                report_dir: str = "out", incremental: bool = False) -> int:
    devices = load_devices(path)

    last = None
    if incremental:
        last = load_last_report(report_dir)
        if last is not None:
            failed_keys = {_dev_key(d) for d in last["devices"] if d["verdict"] != "OK"}
            devices = [d for d in devices if _dev_key(d) in failed_keys]
            if not devices:
                print("上次没有失败设备，跳过巡检。")
                return 0
            print(f"增量巡检：重扫上次非 OK 的 {len(devices)} 台")
        else:
            print("没有找到上一份报告，本次按全量扫描。")

    results: list[CollectionResult] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_collect_one, dev) for dev in devices]
        for fut in futs:
            results.append(fut.result())

    for r in results:
        raw = r.values.get("uptime")
        if isinstance(raw, str):
            try:
                r.values["uptime"] = fmt_ticks(int(raw))
            except ValueError:
                pass
        render(r)

    new_devices = []
    for dev, r in zip(devices, results):
        item = result_to_dict(r)
        item["port"] = _dev_key(dev)[1]   # 落盘带端口，下次增量才能对上号
        new_devices.append(item)
    if incremental and last is not None:
        # 关键：把本次结果覆盖回上一份「完整」报告，别只写子集
        merged = {_dev_key(d): d for d in last["devices"]}
        for d in new_devices:
            merged[_dev_key(d)] = d
        devices_payload = list(merged.values())
    else:
        devices_payload = new_devices

    payload = build_payload(devices_payload)
    report_path = save_report(payload, report_dir)
    s = payload["summary"]
    print(f"\n汇总：{s['total']} 台 → OK {s['ok']} / DEGRADED {s['degraded']} / FAILED {s['failed']}，退出码 {s['exit_code']}")
    print(f"报告已写入 {report_path}")
    return s["exit_code"]
```

`main` 加两个参数：

```python
parser.add_argument("--report-dir", default="out", help="报告输出目录")
parser.add_argument("--incremental", action="store_true", help="只重扫上一份报告里非 OK 的设备")
...
if args.config:
    return scan_config(args.config, report_dir=args.report_dir, incremental=args.incremental)
```

### 为什么增量必须「合并」而不是「只写本次子集」

如果增量报告只写重扫的 3 台，那它就是新的「上一份报告」；下次增量只会在 3 台里找失败，另外 47 台永远被遗忘。**增量必须把结果合并回完整报告**，历史才连续。这是 D15 最容易写错的一点。

---

## 六、自检

先起两台假设备（正常 / 部分失败）：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d15
..\.venv\Scripts\python.exe fake_agent.py --port 1161
..\.venv\Scripts\python.exe fake_agent.py --port 1162 --gen-err --no-such-name
```

全量扫描（`devices.json` 里含可达 + 不可达）：

```powershell
..\.venv\Scripts\python.exe probe.py --config devices.json --report-dir out
```

看生成的报告：

```powershell
Get-ChildItem out\scan_*.json | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content (Get-ChildItem out\scan_*.json | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
```

预期：`summary` 里 `exit_code` 为 1（有 FAILED），`devices` 覆盖配置里的全部设备，失败项带 `kind` 和 `hint`。

制造失败后跑增量（比如停掉一台假设备，或把某台临时改成不可达地址）：

```powershell
..\.venv\Scripts\python.exe probe.py --config devices.json --report-dir out --incremental
```

预期：只重扫上次非 OK 的那几台，打印「增量巡检：重扫 … 台」，新报告合并后仍是完整设备数。

---

## 七、跑验收

实现时配一个 `check_d15.py`，验以下各项：

| 项 | 查什么 |
|---|---|
| B1 | 全量扫描生成 `out/scan_*.json`，`schema` / `summary` / `devices` 齐全，设备数 == 配置数 |
| B2 | 报告里 `verdict` / `failures.kind` / `hint` 与屏幕输出一致 |
| B3 | `--incremental` 只重扫上次非 OK 的设备，且合并后报告设备数仍完整 |
| B4 | 全部 OK 时增量跳过，退出码 0 |
| B5 | 单台模式（`--port`）行为不变 |

---

## 八、卡点表

| 现象 | 原因 | 改法 |
|---|---|---|
| `TypeError: Object of type ... is not JSON serializable` | `values` 里有非 JSON 类型（pysnmp 对象 / TimeTicks） | 序列化前转 `str`；或给 `json.dump` 传 `default=` |
| 报告半截 / `json.load` 崩 | 直接覆盖写，进程中途被中断 | `tmp + os.replace` 原子替换（D11 同款） |
| 中文变 `\uXXXX` | `ensure_ascii` 默认 True | `ensure_ascii=False` |
| 增量后报告设备数变少 | 增量只写了本次重扫的子集 | 合并回上一份完整报告再落盘 |
| `KeyError: 'verdict'` | 读到了旧 schema 的报告 | 校验 `schema`，不匹配就回退全量 |
| 首次运行找不到报告 | `glob` 为空 | `load_last_report` 返回 `None`，按全量扫描并提示 |
| `out` 目录不存在 | 首次运行 | `mkdir(parents=True, exist_ok=True)` |
| 同一秒跑两次覆盖文件 | 时间戳只到秒 | 文件名加毫秒，或存在则加后缀 |
| 报告里没有失败提示 | 只写了 `kind` 没写 `hint` | `result_to_dict` 里把 `hint` 一起落盘 |

---

## 九、今天记住的三条

1. **落盘是契约**：`schema` + `summary` + `devices`，不是把 `list` 一 dump 了事；读的人（包括明天的你）不看代码也知道字段含义。
2. **原子写**：`tmp + os.replace`，代价两行，换来「产物永远是完整可读的」。
3. **增量 = 读回 → 筛非 OK → 重扫 → 合并**：闭环，不是覆盖子集；否则历史会断。

---

## 十、明天（D16）预告

单份报告能看了，下一步是**多份报告之间的比对与趋势**：把同一台设备的多次巡检聚合，看 uptime 变化、连续失败次数、接口 flapping，再据此做「连续 N 次失败才告警」的降噪。今天的 `schema` 版本 + `generated_at` 就是为明天准备的——到时候只需按设备分组，不用改落盘格式。
