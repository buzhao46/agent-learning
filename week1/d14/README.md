# D14 · 并发扫描：让一批设备同时采，而不是排队等

**今天一句话**：D13 是「串行扫一批」（一台一台等超时），今天是「并发扫一批」——把 `scan_config` 的 `for` 循环换成线程池，让一批设备同时采集。`CollectionResult` 列表和退出码聚合**一行都不用改**——这正是 D12/D13 结构化设计的回报。

预计 30–40 分钟。改动只有一处：把 `scan_config` 里的循环换成 `ThreadPoolExecutor`。

---

## 一、今天的目录

```
week1/d14/
├── probe.py         ← D13 的成品，把 scan_config 改成并发
├── ifwalk.py        ← 原样搬来
├── snmpio.py        ← 原样搬来
├── fake_agent.py    ← 原样搬来
├── devices.json     ← 手写配置示例（多台，含不可达）
├── check_d14.py     ← 验收（不要改）
└── README.md        ← 这份
```

---

## 二、先看 D13 的串行版慢在哪

D13 的 `scan_config`：

```python
for dev in load_devices(path):
    result = collect_device(...)   # 这里会阻塞：等 SNMP 超时
    render(result)
```

10 台设备，每台 `timeout=0.5`、不可达约 0.55s 超时 → 串行 ≈ 5.5s。
**但第一台超时的时候，后面 9 个核全在空转等网络。** 这 5.5s 里真正干活的时间可能只有 0.5s（真正可达的那台），其余 5s 是纯等待。

---

## 三、TODO：换成线程池

改动只在 `scan_config`。**铁律：线程里只采，不打印。**

```python
from concurrent.futures import ThreadPoolExecutor

def scan_config(path: str, *, workers: int = 16) -> int:
    devices = load_devices(path)
    results: list[CollectionResult] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [
            ex.submit(collect_device, dev["host"],
                      community=dev.get("community", "public"),
                      port=dev.get("port", 161),
                      timeout=dev.get("timeout", 0.5))
            for dev in devices
        ]
        for fut in futs:                  # futs 保序 → 结果顺序 = 配置顺序
            results.append(fut.result())

    # 主线程统一收尾：转 uptime + 渲染 + 聚合
    for r in results:
        raw_uptime = r.values.get("uptime")
        if isinstance(raw_uptime, str):
            try:
                r.values["uptime"] = fmt_ticks(int(raw_uptime))
            except ValueError:
                pass
        render(r)

    # —— 下面这段聚合逻辑和 D13 一字不差 ——
    PRIORITY = {"OK": 0, "DEGRADED": 1, "FAILED": 2}
    worst_verdict = max((r.verdict for r in results), key=lambda v: PRIORITY[v])
    worst = {"OK": 0, "DEGRADED": 2, "FAILED": 1}[worst_verdict]
    ok = sum(1 for r in results if r.verdict == "OK")
    degraded = sum(1 for r in results if r.verdict == "DEGRADED")
    failed = sum(1 for r in results if r.verdict == "FAILED")
    print(f"\n汇总：{len(results)} 台 → OK {ok} / DEGRADED {degraded} / FAILED {failed}，退出码 {worst}")
    return worst
```

### 为什么用线程而不是进程 / 协程

`collect_device` 绝大部分时间在等 UDP 超时（网络 I/O）。CPython 的 **GIL 在 I/O 等待时会释放**，所以多线程在这里是真并行——所有设备的超时同时发生，整批耗时 ≈ 「最长那台」而不是「所有台之和」。

只有纯 CPU 密集才需要多进程。SNMP 采集是 I/O 等待，`ThreadPoolExecutor` 正好，还不用处理进程间传 `CollectionResult`（那得 pickle，麻烦）。

### 为什么「线程里只采不打印」

多个线程同时 `print` 会让报告交错成一团乱麻（A 台半行 + B 台半行 + …）。而且 `print` 有共享锁，并发 print 反而可能因为抢锁变慢。**采集和展示必须分层**：线程只负责 `collect_device` 返回结构化 `CollectionResult`，主线程按配置顺序统一 `render` + 聚合。这正好是 D12 把 `collect` 和 `render` 分离的好处——今天直接受益。

---

## 四、自检

先起两台假设备（正常 / 部分失败）：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\d14
..\.venv\Scripts\python.exe fake_agent.py --port 1161
..\.venv\Scripts\python.exe fake_agent.py --port 1162 --gen-err --no-such-name
```

跑批量（`devices.json` 含 2 台可达 + 3 台不可达）：

```powershell
Measure-Command { ..\.venv\Scripts\python.exe probe.py --config devices.json }
```

预期：5 台全出报告，退出码 1（有不可达），耗时 ≈ 0.5s 量级（不是 3 台不可达 × 0.5s = 1.5s 的串行感，而是 ≈ 一台超时的时间）。

---

## 五、跑验收

```powershell
..\.venv\Scripts\python.exe check_d14.py
```

| 项 | 查什么 |
|---|---|
| B1 | 并发扫混合批次不抛异常，OK/DEGRADED/FAILED 各出现，退出码 1 |
| B2 | 报告覆盖全部设备（verdict 行数 == 设备数） |
| B3 | 并发速度：总耗时明显小于 N 台串行理论耗时（证明没退化成排队） |
| B5 | 单台模式（--port）行为不变 |

---

## 六、卡点表

| 现象 | 原因 | 改法 |
|---|---|---|
| 报告顺序乱了 | 用了 `as_completed` 渲染 | 用保序的 `futs` 列表 |
| 报告交错成乱麻 | 线程里直接 `render` | render 移到主线程 |
| `RuntimeError: no current event loop in thread` | pysnmp 的同步 `getCmd` 是 asyncio 封装，内部调 `asyncio.get_event_loop()`；主线程有，但线程池工作线程没有 | worker 入口先给当前线程装一个 `asyncio.new_event_loop()`（`_collect_one` 就是干这个） |
| 退出还刷 `Task was destroyed but it is pending` | pysnmp 的 asyncio 后端在 loop 关闭时残留 timeout watchdog task，GC 时报警 | 顶层静音 `logging.getLogger("asyncio")` + `filterwarnings(ResourceWarning)`，与采集结果无关 |
| 验收 B3 误判「退化成串行」 | 不可达设备单台本身就慢（`snmpio.walk` 会重试多次），拍脑袋按 `N×0.5s` 估算串行耗时会错得离谱 | 串行基准要在**同进程实测**（顺序调 `_collect_one`），再和并发对比，不要估 |
| 退出码不对 | 聚合逻辑被误改 | 直接复用 D13 那段，别动 |

---

## 七、今天记住的三条

1. **I/O 密集用线程池**：SNMP 采集在等网络，GIL 释放，多线程真并行。
2. **采集与展示分层**：线程只采（返回结构化结果），主线程统一渲染——这正是 D12 设计的回报。
3. **结构化数据的复利**：D12 的 `CollectionResult` + 三态 verdict，到 D14 连并发都不用改聚合逻辑，只换循环方式。

---

## 八、明天（D15）预告

今天是「并发扫一批，结果打在屏幕上」。明天是「结果落盘」：把 `list[CollectionResult]` 序列化成 JSON 报告 + 持久化到文件，支持增量巡检（只重扫上次失败的）。结构化的好处继续复利。
