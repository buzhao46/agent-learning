# D13 · 批量扫描：把 host 清单搬进配置文件

**今天一句话**：D12 是「一台设备采得准」，今天是「一批设备扫得动」——把 host 清单、community、超时从命令行参数搬进 `devices.json`，让 `probe.py` 读配置扫一整批。

预计 40–50 分钟。三个新东西（读 JSON / 循环 / 聚合退出码），约 40 行。

前 12 天其实已经把最难的活干完了：

- `collect_device` 已经会采一台、分类、给三态 verdict。
- `CollectionResult` 已经准备好装「很多台」——`list[CollectionResult]` 就是今天的全部数据模型。
- 退出码映射 `OK=0 / DEGRADED=2 / FAILED=1` 也已经写好了。

**今天不碰 `collect_device` / `get_raw` / `classify` 任何一行。** 只加一个「读配置 + 循环 + 汇总」的壳。D12 README 最后一句话就是为今天埋的伏笔：「到时候是 `list[CollectionResult]`，退出码取最坏的那个。」

---

## 一、今天的目录

```
week1/d13/
├── probe.py         ← D12 的成品，原样搬来（collect_device / render 都不用改）
├── ifwalk.py        ← D10 的成品，原样搬来（walk 要用）
├── snmpio.py        ← D9 的，原样搬来（ProbeError / fmt_ticks）
├── fake_agent.py    ← D12 的，原样搬来
├── devices.json     ← 今天新建：要扫的 host 清单
├── check_d13.py     ← 验收（不要改）
└── README.md        ← 这份
```

五个 `.py` 全部从 `d12` 复制过来，**不用改一行**（拷贝时把 D12 里我们修过的 `get_raw` 返回值 bug 也一并带过来即可）。

---

## 二、devices.json 长什么样

设计原则：**每台设备的差异（host / port / community / timeout）都进配置，代码里不留硬编码**。

```json
{
  "devices": [
    {"host": "127.0.0.1", "port": 1161, "community": "public", "timeout": 0.5},
    {"host": "127.0.0.1", "port": 1162, "community": "public", "timeout": 0.5},
    {"host": "10.0.0.99",  "port": 161,  "community": "secret", "timeout": 0.5}
  ]
}
```

- 假设备用 `1161`（正常）/ `1162`（`--gen-err --no-such-name`，部分失败）。
- 第三台 `10.0.0.99:161` 是个**不存在的地址**——用来验证「整台掉线」在批量里怎么表现。
- 读取时用 `dict.get(...)` 给默认值，少填字段不报错：
  `port` 默认 `161`，`community` 默认 `"public"`，`timeout` 默认 `0.5`。

---

## 三、第 0 步：亲眼看见「一个退出码不够」（10 分钟，别跳过）

开三个 PowerShell 窗口，`cd` 到 `d13`，各自起一台假设备：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d13

# 窗口 1：正常
..\.venv\Scripts\python.exe fake_agent.py --port 1161

# 窗口 2：部分失败（sysDescr 回 genErr、sysName 回 noSuchName）
..\.venv\Scripts\python.exe fake_agent.py --port 1162 --gen-err --no-such-name
```

然后分别扫这两台，看退出码：

```powershell
..\.venv\Scripts\python.exe probe.py --port 1161; echo "exit=$LASTEXITCODE"
..\.venv\Scripts\python.exe probe.py --port 1162; echo "exit=$LASTEXITCODE"
```

你会看到 `1161` 退出码 `0`（OK），`1162` 退出码 `2`（DEGRADED）。

**关键观察**：如果今天你写 `for d in devices: probe.main()`，退出码是「最后一个设备的」——
前面那台 `DEGRADED` 被后面那台 `OK` 覆盖，整个批次看起来「一切正常」。
**这是批量扫描第一个坑：退出码必须聚合，不能顺手被覆盖。**

---

## 四、TODO 1：读配置

新建 `devices.json`（上面那段），然后给 `probe.py` 加一个「读 JSON」的小函数。
放在 `render` 之后、`main` 之前即可：

```python
import json

def load_devices(path: str) -> list[dict]:
    """读 devices.json，返回设备清单。字段缺省用 .get 兜底。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("devices", [])
```

自检（先确认能读出来）：

```powershell
..\.venv\Scripts\python.exe -c "import probe; print(probe.load_devices('devices.json'))"
```

```
[{'host': '127.0.0.1', 'port': 1161, ...}, ...]
```

---

## 五、TODO 2：扫一批（核心）

在 `main` 里加 `--config` 参数。传了就扫整批；不传就维持 D12 的 `--port` 单台模式。

**改 `main`**（只动入口，不动 `collect_device`）：

```python
def main() -> int:
    parser = argparse.ArgumentParser(description="采一台或多台设备")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1161)
    parser.add_argument("--community", default="public")
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("--config", help="devices.json 路径，传了就扫整批")
    args = parser.parse_args()

    if args.config:
        return scan_config(args.config)
    return scan_one(args)
```

**新增两个函数**（放在 `main` 之前）：

```python
def scan_one(args) -> int:
    """D12 原来的单台逻辑，原样搬过来，行为不变。"""
    result = collect_device(
        args.host, community=args.community, port=args.port, timeout=args.timeout
    )
    raw_uptime = result.values.get("uptime")
    if isinstance(raw_uptime, str):
        try:
            result.values["uptime"] = fmt_ticks(int(raw_uptime))
        except ValueError:
            pass
    render(result)
    return result.exit_code


def scan_config(path: str) -> int:
    """读配置，扫一整批，退出码取所有设备里「最严重」的那个。"""
    results: list[CollectionResult] = []
    for dev in load_devices(path):
        result = collect_device(
            dev["host"],
            community=dev.get("community", "public"),
            port=dev.get("port", 161),
            timeout=dev.get("timeout", 0.5),
        )
        raw_uptime = result.values.get("uptime")
        if isinstance(raw_uptime, str):
            try:
                result.values["uptime"] = fmt_ticks(int(raw_uptime))
            except ValueError:
                pass
        render(result)
        results.append(result)

    # 聚合退出码 —— 见下方「退出码怎么聚合」的取舍
    PRIORITY = {"OK": 0, "DEGRADED": 1, "FAILED": 2}
    worst_verdict = max((r.verdict for r in results), key=lambda v: PRIORITY[v])
    worst = {"OK": 0, "DEGRADED": 2, "FAILED": 1}[worst_verdict]

    ok = sum(1 for r in results if r.verdict == "OK")
    degraded = sum(1 for r in results if r.verdict == "DEGRADED")
    failed = sum(1 for r in results if r.verdict == "FAILED")
    print(f"\n汇总：{len(results)} 台 → OK {ok} / DEGRADED {degraded} / FAILED {failed}，退出码 {worst}")
    return worst
```

### 退出码怎么聚合（今天唯一要动点脑子的地方）

D12 定的约定是 `OK=0 / DEGRADED=2 / FAILED=1`。注意一个反直觉点：
**数值上 `DEGRADED(2) > FAILED(1)`，但语义上「整机掉线(FAILED)」比「部分坏(DEGRADED)」更紧急。**

所以「取最严重」有两种写法，结果不同：

| 写法 | 一台 FAILED + 一台 DEGRADED → 整体退 | 语义 |
|---|---|---|
| **A. 按 exit_code 数值 `max`** | `max(1, 2) = 2`（告警） | 部分坏压过整机掉线 |
| **B. 按 verdict 严重度（FAILED > DEGRADED > OK）**（上面代码用的） | `1`（升级） | **整机掉线最紧急，优先升级** |

→ **推荐 B**：一台彻底掉线比一台部分坏更该立刻升级，所以聚合应按「语义严重度」而非「退出码数值」。
（如果你们运维约定「只要有任何部分失败就整体告警」，用 A 也行——把 `scan_config` 里的聚合换成 `worst = max(r.exit_code for r in results)` 即可。）

### 自检（先不起假设备，验证能读能循环）

```powershell
..\.venv\Scripts\python.exe probe.py --config devices.json; echo "exit=$LASTEXITCODE"
```

起来三台假设备（1161 正常 / 1162 部分失败 / 10.0.0.99 不存在）后，预期：

```
127.0.0.1        OK        4/4 项成功
    sysDescr  ...
    uptime     ...
    sysName    ...
    ifTable   4
127.0.0.1        DEGRADED  2/4 项成功
    ...
10.0.0.99        FAILED    0/4 项成功
    [!] sysDescr: unreachable（[Errno 10060] ...）
    ...

汇总：3 台 → OK 1 / DEGRADED 1 / FAILED 1，退出码 1
```

退出码 `1`（因为有一台 FAILED，按 B 方案优先级最高）。

---

## 六、跑验收

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d13
..\.venv\Scripts\python.exe check_d13.py
```

验收项（check_d13.py 会自己拉起假设备，跑完关掉）：

| 项 | 查什么 | 不通过时最常见原因 |
|---|---|---|
| B1 | devices.json 混合了 OK / DEGRADED / FAILED，扫完不抛异常 | JSON 字段名拼错 |
| B2 | 含一台不存在地址 → 该台 `FAILED`，整体退出码取最坏 | `worst` 被最后一台覆盖 / 没按 verdict 聚合 |
| B3 | 部分失败那台 → `DEGRADED`，另两项照常采到 | 循环里 `break` 了 |
| B4 | 退出码聚合正确（FAILED 优先于 DEGRADED） | 用了 `max(exit_code)` 而非 verdict 优先级 |
| B5 | 单台模式（`--port`）行为不变 | 误改了 `scan_one` |

---

## 七、卡住了怎么办

| 现象 | 原因 | 怎么改 |
|---|---|---|
| `FileNotFoundError: devices.json` | 路径传错 / 没 `cd` 到 d13 | check 会自己处理路径 |
| 退出码总是 0 | `worst` 没聚合，被最后一台覆盖 | 用 verdict 优先级或 `max` |
| 第三台没报 FAILED | `10.0.0.99:161` 其实能通 / 端口没对上 | 确认地址确实不可达 |
| 单台模式行为变了 | 误改了 `scan_one` | 保持 D12 原样，只加 `scan_config` |
| 基于 pyright 报「隐式相对导入 / 未知符号」 | 脚本式目录 + pysnmp 无类型，误报 | 与 D12 一样，已由 `.vscode/settings.json` 的 `diagnosticSeverityOverrides` 压制，不影响运行 |

---

## 八、今天必须记住的三条

**1. 批量扫描的退出码必须聚合，不能让最后一台覆盖前面。**
`for d in devices: main()` 的写法会把「批次最严重」悄悄变成「最后一台」，巡检系统据此报警会漏掉真实故障。

**2. 配置优于命令行。**
host / community / timeout 全部进 `devices.json`，代码零硬编码。明天（D14）做并发扫描时，这份配置不动，只换循环方式。

**3. D12 的 `CollectionResult` 今天兑现成 `list[CollectionResult]`。**
这正是 D12 README 说的「为明天准备」——三态 verdict 不是给单台看的，是给「一批设备里捞异常」看的。

---

## 九、明天（D14）预告

今天是「串行扫一批」，明天是「并发扫一批」：把 `scan_config` 里的 `for` 循环换成线程池 / 协程，
让 50 台设备不再一台一台等超时。`CollectionResult` 列表和退出码聚合**一行都不用改**——
并发只是把「依次 `collect_device`」变成「同时 `collect_device`」。
