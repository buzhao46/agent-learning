# D17 · 告警状态机与通知：让告警只发一次

**今天一句话**：D16 的 `decide` 能回答「这台设备现在坏不坏」，但它无状态——只要连续失败还 ≥ 阈值，每次跑都返回 ALERT。今天引入**持久化状态**，把「该不该再喊一次」从「坏不坏」里拆出来，再补上通知、outbox 和静默窗口。

预计 40–60 分钟。新增 `state.py`、`alerts.py`、`notify.py`、`alert.py`，`history.py` 做一个纯重构（`Trend.last_seen`）。

---

## 一、今天的目录

从 D15 复制（D16 的代码也在 D15 里）：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
New-Item -ItemType Directory d17
Copy-Item d15\*.py,d15\*.json d17\
```

```
week1/d17/
├── probe.py         ← 原样搬：采集 + 落盘 + 增量
├── report.py        ← 原样搬：汇总 / 原子写 / 读回
├── history.py       ← 改动：group_by_device 带时间，Trend 加 last_seen
├── trend.py         ← 原样搬：D16 的终端趋势表
├── state.py         ← 新增：alert_state.json + outbox
├── alerts.py        ← 新增：状态机（今天的核心）
├── notify.py        ← 新增：console / file / webhook
├── alert.py         ← 新增：入口，五步流程
├── ifwalk.py        ← 原样搬
├── snmpio.py        ← 原样搬
├── fake_agent.py    ← 原样搬（D16 加了 --uptime）
├── devices.json     ← 配置
├── check_d12..d17.py
└── README.md        ← 这份
```

两个刻意的选择：

- **不复制 `d15/README.md`**。D15 的笔记里有些代码片段已被 D16 改过（`save_report` 的指纹、增量合并的 key），复制过去就是错文档。
- **不复制 `out/`**。它在 `.gitignore` 里，且混着验收的临时目录；历史报告不该跨天继承。

复制完先验基线，**两个全绿再往下写**：

```powershell
cd d17
..\.venv\Scripts\python.exe check_d15.py     # 4/4
..\.venv\Scripts\python.exe check_d16.py     # 6/6
```

---

## 二、先看 D16 缺什么

三处开口，都能在代码里指到具体位置：

1. **`decide` 是无状态纯函数**。连续 3 次失败之后，只要那 3 份报告还在，每次运行都返回 ALERT。接上通知就是每 5 分钟一条，运维第三天就把通知静音了——D16 的降噪白做。
2. **`consecutive_ok` 算了但没人用**。恢复也需要连续 N 次才消警，否则设备一抖就在 ALERT / OK 之间来回，这叫 flapping alarm，比不告警更烦人。计数要跨运行累积，就得存下来。
3. **`Trend` 没有 `last_seen`**。增量巡检会跳过上次 OK 的设备，某台设备可能几小时没被扫到。现在 `decide` 只能看到「历史里有 3 条样本」，看不到「最后一条是 3 小时前的」。

一句话概括今天的主题：**降噪的最后一步是有状态**。

---

## 三、TODO 1：给 `Trend` 补 `last_seen`（纯重构）

`history.py` 里 `group_by_device` 原来只存设备片段，时间在报告顶层，丢了。改成把时间一起带下来：

```python
def group_by_device(reports: list[dict]) -> dict[tuple[str, int], list[tuple[str, dict]]]:
    groups: dict[tuple[str, int], list[tuple[str, dict]]] = {}
    for report in reports:
        stamp = str(report.get("generated_at", ""))
        for dev in report.get("devices", []):
            groups.setdefault(device_key(dev), []).append((stamp, dev))
    return groups
```

`analyze` 的入参随之变成 `list[tuple[str, dict]]`，函数体第一行解包即可，其余逻辑一行不动：

```python
samples = [dev for _, dev in items]
trend.last_seen = items[-1][0] or None
```

`trend_to_dict` 里补一个 `"last_seen": trend.last_seen`。

**这是一次纯重构，行为不能变**：改完立刻重跑 `check_d16.py`，必须还是 6/6。如果先在 `trend.py` 里加显示、再改签名，你会分不清是重构坏了还是新功能坏了。

---

## 四、TODO 2：`state.py` —— 状态持久化 + outbox

状态文件默认 `out/alert_state.json`：

```json
{
  "schema": 1,
  "updated_at": "2026-11-03T11:00:00",
  "devices": {
    "127.0.0.1:57327": {
      "status": "ALERT", "since": "2026-11-03T10:20:00", "stale": false,
      "consecutive_bad": 3, "consecutive_ok": 0,
      "last_seen": "2026-11-03T10:58:00",
      "last_notified_at": "2026-11-03T10:20:00"
    }
  },
  "pending": []
}
```

三个设计点：

- **key 用 `"host:port"` 字符串**。JSON 的 key 只能是字符串，tuple 存不进去。解析用 `rpartition(":")`，别用 `split(":")`——IPv6 地址里全是冒号。
- **`pending` 是 outbox**：没送出去的事件先存着，下次运行开头补发。上限 100 条，超出丢最旧并打印警告。不设上限的话，通知渠道坏一周能把状态文件撑到几十 MB，恢复时一次糊几百条过期告警到人脸上。
- **坏文件先备份再初始化**：

```python
try:
    data = json.loads(file.read_text(encoding="utf-8-sig"))
except (OSError, json.JSONDecodeError) as exc:
    backup = _backup(file)          # alert_state.bad-<时间戳>.json
    return empty_state(now), f"状态文件损坏，已备份为 {backup.name}：{exc}"
```

不要静默覆盖。那是唯一能复盘「为什么昨天那条告警没发出来」的现场。

写盘复用 `report.write_json_atomic`：`tmp + os.replace`，不会留半截 JSON。

---

## 五、TODO 3：`alerts.py` —— 状态机（今天真正的产出）

### 输入输出

```python
@dataclass
class Observation:          # 由 history.Trend 映射而来
    label: str
    verdict: str            # "ALERT" / "OK" / "UNKNOWN"
    consecutive_bad: int
    consecutive_ok: int
    last_seen: str | None

def step(prev, obs, *, recover=2, stale_after=30, now=None) -> tuple[dict, list[Event]]
```

`step` 是纯函数：不读文件、不打印、不发通知。这样验收可以直接调它，不用起进程。

### 核心分支（这段决定一切）

```python
if obs.verdict == "ALERT":
    if old != ST_ALERT:
        state["status"] = ST_ALERT
        state["since"] = stamp
        events.append(Event(EVENT_OPEN, obs.label, old, ST_ALERT, _alert_reason(obs), stamp))
    # 已经是 ALERT：只更新计数，不产生事件 —— 今天所有降噪都靠这一条
```

### 恢复要防抖

```python
elif obs.verdict == "OK":
    if old == ST_ALERT:
        if obs.consecutive_ok >= recover:
            ... 产生 EVENT_CLOSE
        # 否则维持 ALERT：但 consecutive_ok 已经写进 state，下次接着往上数
```

`else` 分支那行注释不是废话：**忘了把新的 `consecutive_ok` 写回 state，恢复计数每次从 0 开始，永远恢复不了**。这个 bug 跑一次看不出来，跑两次才暴露。

### `UNKNOWN` 不覆盖已知状态

设备失联时最新报告里根本没有它，`decide` 给不出结论。如果这时把 `UNKNOWN` 写回去，下一次数据回来时旧状态已经丢了，会凭空产生一条 `alert_close`——「设备已经恢复了」——而实际上它从来没被修好过。

### 与「四状态互斥」的差异（有意为之）

直觉画法是把 `OK / ALERT / STALE / UNKNOWN` 当四个互斥状态。实现里把 **`stale` 拆成独立布尔字段**，`status` 只留 `OK / ALERT / UNKNOWN`：

- 「设备坏了」和「监控瞎了」是两个正交事实，互斥表达会出现「进了 STALE 就丢失 ALERT 记录」，数据一恢复不知道该不该发恢复通知；
- 拆开之后两个事实并存，转换逻辑也不用再记 `prior_alert` 这种补丁字段。

### 静默判定（同一文件）

```python
def is_silenced(label, now, silences) -> tuple[bool, str]:
    """silences: [{"device": "10.0.0.9:161" 或 "*", "until": "...", "reason": "换板卡"}]"""
```

`until` 解析不了就忽略这条——宁可漏静默，也不要变成永久静默。没有结束时间的静默等于永久关监控。

---

## 六、TODO 4：`notify.py` —— 渠道抽象

```python
def send(event, *, channel="console", webhook=None, log_path="out/alerts.log",
         timeout=3.0) -> tuple[bool, str]:
    """发一条事件。失败只返回，不抛 —— 状态机不能因为网络抖动崩。"""
```

| 渠道 | 用途 |
|---|---|
| `console` | 人工跑的时候看 |
| `file` | 追加 `out/alerts.log`，一行一条 JSON；验收靠它断言 |
| `webhook` | `urllib` POST JSON，3 秒超时，失败重试 1 次 |

- **不引入 `requests`**：为一个 POST 加依赖，换环境就得再装一次。
- **只重试 1 次**：渠道挂半小时靠 outbox 下次补发，比在一次运行里死等强；重试太多次会把调度周期拖长，让下一次巡检迟到。

---

## 七、TODO 5：`alert.py` 入口

```powershell
python alert.py --report-dir out --state out/alert_state.json --channel file
python alert.py --report-dir out --dry-run
```

五步，顺序不能换：

1. 读状态（坏文件先备份，按首次运行处理）
2. 算趋势（**直接 import `history.build_trends`，不读 `trend.py --json`**：少一跳、少一个失败点，也不用把展示层拉进来）
3. 补发 outbox 里上次没送出去的
4. 每台设备过状态机 → 新事件 → 静默过滤 → 发送 / 入队
5. 原子写回状态

### 状态里有、本次报告里没有的设备，也要过一遍

```python
for label, prev in state.get("devices", {}).items():
    if label in known:
        continue
    observations.append(Observation(label=label, verdict="UNKNOWN",
                                    last_seen=prev.get("last_seen"), ...))
```

增量巡检会跳过上次 OK 的设备。只按 `trends` 迭代的话，这些设备会从状态机里消失——既不会 stale，也不会告警。**监控自己瞎了，却在报平安。**

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 本次没有新事件 |
| 1 | 产生了事件（含 dry-run） |
| 2 | 状态读写失败，或有待发事件发送失败 |

⚠️ `0` 不是「设备都好」，只是「没有新事件」。设备昨天就坏了、今天一直没好，退出码也是 0。要判健康请看 `trend.py` 的退出码。这个差异已写进 `alert.py` 的 docstring——接调度器时最容易误读。

---

## 八、TODO 6：静默窗口

```powershell
python alert.py --report-dir out --silences out/silences.json
python alert.py --report-dir out --silence-until "2026-11-04T02:00:00"
```

`silences.json`：

```json
[{"device": "10.0.0.9:161", "until": "2026-11-04T02:00:00", "reason": "更换板卡"}]
```

两条语义必须写死：

- **静默抑制的是通知，不是状态**。状态照常推进，事件照常产生，只是不发、**也不入 outbox**（静默的语义是「不发」，不是「延后发」）。
- **静默结束不补发历史告警**，只发当前真实状态。否则维护窗口一结束，你收到 40 条过期告警。

---

## 九、自检

起一台假设备，先采一次正常的：

```powershell
..\.venv\Scripts\python.exe fake_agent.py --port 1161
..\.venv\Scripts\python.exe probe.py --config devices.json --report-dir out
```

停掉假设备，再采三次（`devices.json` 只留 `127.0.0.1:1161` 即可），然后跑告警：

```powershell
..\.venv\Scripts\python.exe alert.py --report-dir out --state out\alert_state.json --channel file
```

预期第一次：

```
状态：首次运行（状态文件不存在）
  127.0.0.1:1161         -        → ALERT            alert_open
[发送] alert_open  127.0.0.1:1161       → out\alerts.log
状态已写入 out\alert_state.json（待发 0 条）
exit=1
```

**同一条命令再跑一遍**，这是今天最关键的验证：

```
  127.0.0.1:1161         ALERT    → ALERT            -
exit=0
```

日志不增长、`since` 不变。如果第二次又是一条 `alert_open`，说明状态没落盘或者 key 对不上。

`out/alerts.log` 是 UTF-8；Windows 控制台默认 GBK，用 `Get-Content -Encoding utf8` 看。

---

## 十、跑验收

`check_d17.py` 不启动 SNMP，直接构造 `scan_*.json`（时间戳用「现在 - N 分钟」生成，stale 判定才是确定性的，不靠 sleep）：

| 项 | 查什么 |
|---|---|
| B1 | 无状态文件 + 连续 3 次失败 → 立即 `alert_open`、状态落盘、退出码 1 |
| B2 | 同一批报告再跑 → 零事件、退出码 0、日志不增长、`since` 不变 |
| B3 | `recover=2` 时 1 次 OK 不消警，第 2 次 OK 才 `alert_close` |
| B4 | 设备从本次报告中消失 → 状态仍 ALERT，不被写成 UNKNOWN |
| B5 | `last_seen` 超过 `stale_after` → 发一次 `stale`，再跑不重复 |
| B6 | webhook 不可达 → 退出码 2、`pending` 保留、状态仍 ALERT；换 file 渠道重跑 → 补发成功 |
| B7 | `--dry-run` → 状态文件字节不变、日志不增长，但报告「将发什么」 |
| B8 | 状态文件坏 JSON → 不崩、备份成 `alert_state.bad-*.json`、按首次运行处理 |
| B9 | 静默窗口 → 不发、不入队，但状态仍推进到 ALERT |

```powershell
..\.venv\Scripts\python.exe check_d17.py
# 通过 9/9
```

---

## 十一、卡点表

| 现象 | 原因 | 改法 |
|---|---|---|
| 每 5 分钟收到同一条告警 | 拿 `decide` 的返回值直接发通知，没走状态机 | 事件只在跃迁时产生 |
| 恢复后不消警 | 新的 `consecutive_ok` 没写回 state | `step` 里无论走哪个分支都先写计数 |
| 抖一下就消警、再抖又报警 | `recover` 设成 1 | `recover` 至少 2，与 `threshold` 独立 |
| 设备失联时收到「恢复」 | `UNKNOWN` 覆盖了 `ALERT` | `UNKNOWN` 一律不改状态 |
| 同一条告警重复发 | 通知失败后回滚状态重发 | 状态只前进，失败进 outbox |
| 状态文件偶发半截 JSON | 直接 `open("w")` 写 | `write_json_atomic` |
| 状态文件越滚越大 | `pending` 无上限 | 上限 100，丢最旧并告警 |
| key 解析错乱 | `split(":")` 拆 `host:port` | `rpartition(":")` |
| `alerts.log` 里中文乱码 | 控制台按 GBK 读 UTF-8 文件 | 文件本身没问题，用 `-Encoding utf8` 看 |
| 增量跳过的设备永远不告警 | 只按 `trends` 迭代，没把状态里的设备补进来 | `collect_observations` 追加 UNKNOWN |

---

## 十二、今天记住的三条

1. **降噪的最后一步是「有状态」**。无状态判定只能回答「现在坏不坏」，回答不了「该不该再喊一次」。
2. **状态推进与通知送达必须解耦**。状态是事实，通知是投递；把投递失败当成事实回滚，会造出重复告警的死循环。
3. **`STALE` 和 `ALERT` 是两种病**：一个是设备坏了，一个是监控瞎了。混成一个状态，故障复盘时永远分不清「设备挂了 3 小时」和「我们 3 小时没采到」。

---

## 十三、明天（D18）预告

告警能只发一次了，下一步是**告警的收敛与升级**：同一机房的 20 台设备一起掉线时，是把 20 条通知都发出去，还是收敛成一条「上游链路故障」？以及「告警发了 30 分钟没人处理就升级给第二个人」——这需要 `pending` 之外再加一层**未确认状态**（ack），而不仅仅是投递状态。今天的 outbox 和 `since` 字段就是为它准备的。
