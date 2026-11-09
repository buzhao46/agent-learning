# D18 · 告警收敛与升级：别让 20 台同时掉线刷爆手机

**今天一句话**：D17 让「同一条告警只发一次」了，但没解决两件事——20 台一起掉线还是 20 条通知；告警发出来半小时没人理，也不会自动找第二个人。今天加 **ack（确认）**、**escalation（升级）** 和 **correlation（同组批量故障收敛成一条）**。

预计 50–70 分钟。在 D17 的 `alerts.py` / `state.py` / `alert.py` 上扩展，新增 `ack.py` 和 `groups.json`，不改既有接口语义。

---

## 一、今天的目录

从 D17 复制（D17 的代码已经在 `week1/d17/`，状态机、outbox、静默都落地了）：

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
New-Item -ItemType Directory d18
Copy-Item d17\*.py,d17\*.json d18\
```

```
week1/d18/
├── probe.py / report.py / history.py / trend.py   ← 原样搬
├── state.py        ← 扩展：state["devices"][label] 加 ack/escalation 字段，新增 state["groups"]
├── alerts.py       ← 扩展：新增 maybe_escalate()、AlertState 加字段、GroupState
├── notify.py       ← 基本不动，alert.py 按 kind 选渠道即可
├── alert.py        ← 扩展：主流程加「扫描超时升级」+「组收敛」两步
├── ack.py          ← 新增：人工确认告警的 CLI
├── groups.json     ← 新增：拓扑分组（机房/网段 → 设备清单）
├── fake_agent.py / devices.json / check_d12..d17.py
└── README.md       ← 这份
```

两个刻意选择（和 D15→D17 一致）：

- **不复制 `d17/README.md`**。D18 会改 `state.py` 的字段结构，复制过去的文档立刻过时。
- **不复制 `out/`**。含验收临时目录；历史状态不该跨天继承。

复制完先验基线——**D17 的代码原样搬过来，逻辑没动，`check_d17.py` 应直接 9/9**：

```powershell
cd d18
..\.venv\Scripts\python.exe check_d17.py     # 9/9
```

如果这里不是 9/9，说明复制漏了文件或 `.venv` 不在 d18 的父目录，先修好再往下。

---

## 二、先看 D17 缺什么

四个开口，都能在 D17 代码里指到：

1. **幂等只解决了「重复 open」，没解决「总量炸」**。D17 的 `step` 保证同一台设备 ALERT 只发一条 `alert_open`。但 20 台同时掉线 = 20 条 `alert_open`，每条只发一次，可 20 条还是 20 条。运维的手机照样炸。
2. **没有 ack**。运维在别的系统看到、确认了「我知道了」，但 D17 的状态里没处记。`state["devices"][label]` 只有 `status/since/stale/consecutive_*`，没有「是否已被确认」。下次巡检照常跑，恢复前一切如常——只是没人能标记「在处理了」。
3. **没有 escalation（升级）**。`alert.py` 五步流程里没有「过了 N 分钟还没 ack 就找第二个人」这一步。告警发了等于发了，石沉大海没人知道。
4. **没有拓扑**。`step` 只认 `host:port`，不知道「这 20 台属于同一个机房、共享一条上游链路」。要做收敛，得先有「谁和谁是一伙的」。

一句话：**D17 管住了「一条告警发几次」，D18 管「告警该找谁、该不该合并」。**

---

## 三、TODO 1：拓扑分组（groups）

告警策略不该和采集配置（`devices.json`）绑死——采集加一台机器，不代表告警就要为它单独建组。所以**独立一个 `groups.json`**，解耦。

```json
{
  "groups": [
    { "name": "机房A",  "members": ["10.0.0.9:161", "10.0.0.10:161", "10.0.0.11:161"] },
    { "name": "机房B",  "members": ["10.0.0.20:161", "10.0.0.21:161"] }
  ]
}
```

`alert.py` 启动时读它，建一张 `label -> group_name` 的逆索引。`members` 里没列出的设备算「无组」，不参与收敛，但**仍然各自正常告警**——收敛是「有组才合并」，不是「有组才通知」。

**坑**：`label` 是 `"10.0.0.9:161"`，组文件里写 `"10.0.0.9:161"` 还是 `"10.0.0.9"` 必须统一。建议组文件里就写完整 `host:port`，和状态 key 完全一致，省一层映射。

---

## 四、TODO 2：状态机加 ack / escalation 字段

在 `state.py` 的 `empty_state` 基础上，单设备状态扩成：

```python
# state["devices"][label] = {
#   "status": "ALERT", "since": "...", "stale": false,
#   "consecutive_bad": 3, "consecutive_ok": 0, "last_seen": "...", "last_notified_at": "...",
#   "acknowledged": False, "acked_by": None, "acked_at": None,
#   "escalation_level": 0, "last_escalated_at": None,
# }
```

`alerts.py` 里 `step()` **只尊重这些字段，不修改它们**（原因见第七节偏离声明）。具体：

- 进入 ALERT（`alert_open`）时：`acknowledged=False`、`escalation_level=0`、`acked_by/acked_at=None`。
- 恢复 OK（`alert_close`）时：把 `acknowledged / acked_by / acked_at / escalation_level / last_escalated_at` 全部清零——设备修好了，确认记录没有保留意义。
- 已是 ALERT 且 `acknowledged=True`：保持状态，但**升级扫描会跳过它**（见 TODO 4）。

新增一个**独立的纯函数** `maybe_escalate`，不塞进 `step`：

```python
def maybe_escalate(
    prev: dict, *, escalate_after: int, max_level: int, now: datetime
) -> tuple[dict, Event | None]:
    """返回 (可能更新的状态, escalate 事件或 None)。纯函数，不读盘不发通知。"""
    if prev.get("status") != "ALERT" or prev.get("acknowledged"):
        return prev, None
    level = int(prev.get("escalation_level", 0))
    waited = (now - datetime.fromisoformat(prev["since"])).total_seconds() / 60
    threshold = escalate_after * (
        2**level
    )  # 窗口翻倍：L0→等 escalate_after，L1→再等 2×，L2→再等 4×
    if level < max_level and waited >= threshold:
        new_state = {
            **prev,
            "escalation_level": level + 1,
            "last_escalated_at": now.isoformat(timespec="seconds"),
        }
        event = Event(
            "alert_escalate",
            prev_label(prev),
            "ALERT",
            "ALERT",
            f"未确认告警升级至 L{level + 1}（已等待 {waited:.0f} 分钟）",
            now.isoformat(timespec="seconds"),
        )
        return new_state, event
    return prev, None
```

`Event.kind` 取值新增：`"alert_ack"`、`"alert_escalate"`、`"group_alert"`（`Event.to_dict` 不用改）。

**为什么升级窗口翻倍**：一条告警等了 30 分钟没人理，说明第一级的人没看到；再等 30 分钟还升级一次就够烦了，再等 30 分钟又升级一次就是骚扰。翻倍（30 / 60 / 120）既给机会处理，又不刷屏。

---

## 五、TODO 3：ack 接口（ack.py）

人工确认是**外部动作**，不该在 `step` 里自动发生。单独一个 CLI：

```powershell
python ack.py --state out/alert_state.json --device 10.0.0.9:161 --by alice --note "在排了"
python ack.py --state out/alert_state.json --group 机房A --by bob        # 批量确认整组
```

`ack.py` 逻辑（直接复用 `state.py` 的 `load_state/save_state`）：

```python
state, _ = load_state(args.state, now=now)
targets = resolve_targets(state, args)  # --device 单台，或 --group 展开成组内所有 label
for label in targets:
    dev = state["devices"].get(label)
    if dev and dev.get("status") == "ALERT":
        dev["acknowledged"] = True
        dev["acked_by"] = args.by
        dev["acked_at"] = now.isoformat(timespec="seconds")
save_state(state, args.state, now=now)
# 可选：把 alert_ack 事件进 pending，下次 notify 发一条「已确认」
```

退出码：`0` 成功，`2` 状态文件损坏（按 `load_state` 的降级行为，损坏时其实会初始化成空状态——**这点要小心**：ack 一个不存在的状态文件，不该创建空文件然后说成功。所以 `ack.py` 里先判断文件是否存在，不存在直接报 `2`）。

**坑**：ack 一个已经 `stale` 的设备没意义——它根本没数据了，确认什么？`ack.py` 对 `stale` 设备给个警告但不阻止（语义上允许「我知道它失联了」），但不发 `alert_ack` 通知，避免误导。

---

## 六、TODO 4：升级（加进 alert.py 主流程）

D17 `alert.py` 是五步：读状态 → 算趋势 → 补发 pending → 过状态机 → 写回。D18 在**第三步之后、第四步之前**插一步「扫描超时升级」：

```python
# 3.5 升级扫描：对每台 ALERT 且未 ack 的设备，检查是否该升级
for label, dev in state["devices"].items():
    new_dev, event = maybe_escalate(
        dev, escalate_after=args.escalate_after, max_level=args.max_level, now=now
    )
    state["devices"][label] = new_dev
    if event is not None:
        escalation_events.append(event)
```

升级事件要和「新状态跃迁事件」一起进第四步的发送通道，但**走升级渠道**：

```python
def channel_for(kind: str, args) -> tuple[str, str | None]:
    if kind in ("alert_escalate", "group_alert"):
        return args.escalate_channel, args.escalate_webhook
    return args.channel, args.webhook
```

`alert.py` 加参数：`--escalate-after`（默认 30 分钟）、`--max-level`（默认 2）、`--escalate-channel`（默认 webhook）、`--escalate-webhook`。

**幂等天然成立**：`maybe_escalate` 一旦把 `escalation_level` 加了 1，下次再扫描 `threshold` 翻倍，不会重复发同级。即使 `alert.py` 中途崩溃、状态已写回，重跑也只会产生下一级（如果又超时了），不会重复 L1。

---

## 七、TODO 5：收敛（correlation）

20 台一起掉线，理想是 1 条「机房 A 上游故障」，不是 20 条。在**第四步（发送）之前**做组聚合：

```python
def correlate(events, state, groups, threshold):
    # 1. 统计每个组当前 ALERT 数 / 组内总数
    group_stats = {}
    for g in groups["groups"]:
        members = set(g["members"])
        alerted = [m for m in members if state["devices"].get(m, {}).get("status") == "ALERT"]
        group_stats[g["name"]] = (len(alerted), len(members))

    final_events = []
    for ev in events:
        gname = label_to_group.get(ev.label)
        if gname and group_stats[gname][0] / group_stats[gname][1] >= threshold:
            if ev.kind == "alert_open":  # 单设备 open 被收敛，不单独发
                ev.suppressed = True
                ev.reason = f"已收敛到组告警（{gname}）"
                continue
        final_events.append(ev)

    # 2. 对每个越过阈值的组，生成/复用一条 group_alert
    for g in groups["groups"]:
        n_alert, n_total = group_stats[g["name"]]
        gstate = state["groups"].setdefault(g["name"], {"status": "OK", "since": None})
        if n_total and n_alert / n_total >= threshold:
            if gstate["status"] != "ALERT":
                gstate["status"] = "ALERT"
                gstate["since"] = now.isoformat(timespec="seconds")
                final_events.append(
                    Event(
                        "group_alert",
                        g["name"],
                        "OK",
                        "ALERT",
                        f"{g['name']}：{n_alert}/{n_total} 设备不可达，疑似上游故障",
                        stamp,
                    )
                )
        else:
            if gstate["status"] == "ALERT":
                final_events.append(
                    Event(
                        "group_alert",
                        g["name"],
                        "ALERT",
                        "OK",
                        f"{g['name']} 故障恢复，剩 {n_alert}/{n_total} 台异常",
                        stamp,
                    )
                )
            gstate["status"] = "OK"
    return final_events, state
```

`state["groups"]` 是 D18 新增的顶层键，结构 `{组名: {status, since}}`，和 `state["devices"]` 平级。

**三个偏离声明（和 D17 把 stale 拆布尔字段同一思路，都是「别把正交的事塞进一个状态」）：**

1. **group 状态不写进单设备 state**。设备各自仍是 ALERT（事实不丢），只是通知被收敛。`group_alert` 和单设备 `alert_open` 是两套事实，存两个地方才不会互相干扰。
2. **ack 只能由 `ack.py` 和 `close` 修改，`step` 不碰它**。`step` 的职责是「状态跃迁」，确认是运维的人为动作；混进去会让「自动重新 open」把别人的 ack 悄悄清掉。
3. **收敛抑制的是「通知」，不是「状态」**。被 `suppressed` 的单设备事件仍然进了 `pending`（或不进，但状态照常 ALERT），将来退出收敛（组恢复）时，如果个别设备还坏着，会重新按单设备发——而不是「组好了就全当好了」。

---

## 八、TODO 6：验收（check_d18.py）

直接构造 `scan_*.json`，不启 SNMP（和 D17 验收同一手法，时间戳用「现在 - N 分钟」，升级判定才确定）。覆盖：

| 项 | 查什么 |
|---|---|
| C1 | ack 后 `maybe_escalate` 返回 None：确认的设备不再产生 escalate |
| C2 | 未 ack、等待 ≥ `escalate_after` → 产生 1 条 `alert_escalate`，且 level 只 +1（重复跑不刷屏） |
| C3 | 同组 15/20 同时掉线 → 只有 1 条 `group_alert`，单设备 `alert_open` 被 `suppressed` |
| C4 | 组恢复到 5/20（<阈值）→ 发 `group_alert` 恢复明细，组状态清回 OK |
| C5 | 升级到 `max_level` 后不再升级（上限生效） |
| C6 | `ack.py --device` 标记后，状态文件 `acknowledged=True` 且持久；`--group` 批量展开正确 |
| C7 | 组状态与单设备状态解耦：组恢复不把组内仍 ALERT 的设备误标 OK |
| C8 | 回归：D17 的 B1–B9 仍全过（升级/收敛不改既有语义） |

```powershell
..\.venv\Scripts\python.exe check_d18.py     # 通过 8/8
..\.venv\Scripts\python.exe check_d17.py     # 仍 9/9（回归）
```

---

## 九、自检（端到端，真机）

```powershell
# 起 1 台假设备，采一次正常的
..\.venv\Scripts\python.exe fake_agent.py --port 1161
..\.venv\Scripts\python.exe probe.py --config devices.json --report-dir out

# 停掉假设备，连采 3 次（设备进 ALERT）
# 再往 groups.json 里把这台和另外 19 台放进「机房A」
..\.venv\Scripts\python.exe alert.py --report-dir out --state out/alert_state.json --channel file

# 预期：20 台里若 ≥12 台 ALERT → 只 1 条 group_alert 进 alerts.log，单设备 open 被抑制
# 等 escalate_after 分钟（或把报告时间戳改旧）再跑一次 → 1 条 alert_escalate（升级渠道）
# 人工确认：
..\.venv\Scripts\python.exe ack.py --state out/alert_state.json --group 机房A --by alice
# 再跑 alert.py → 不再产生 escalate（C1 验证）
```

注意 `out/alerts.log` 是 UTF-8，Windows 控制台默认 GBK 会乱码，用 `Get-Content -Encoding utf8` 看。

---

## 十、卡点表（预计会踩的）

| 现象 | 原因 | 改法 |
|---|---|---|
| 升级刷屏 | `escalation_level` 没写回 state | `maybe_escalate` 返回新状态，alert.py 必须接住并写回 |
| ack 后又被升级 | `step` 重 open 时清了 ack | `step` 不碰 ack 字段，只有 `ack.py` 和 `close` 改 |
| 组恢复把坏设备标 OK | 收敛逻辑误改单设备状态 | 收敛只动 `state["groups"]`，不碰 `state["devices"]` |
| 20 条变 1 条但恢复后失联设备消失 | suppressed 事件没保留状态 | 抑制的是通知，设备状态仍各自 ALERT |
| ack 空状态文件却成功 | `load_state` 降级成空状态 | `ack.py` 先判文件存在，不存在直接 `2` |
| 组名和 label 对不上 | 组文件写 `10.0.0.9` 而非 `10.0.0.9:161` | 组文件统一用完整 `host:port` |
| 升级窗口太密 | 固定间隔 | 用翻倍窗口（30/60/120），给处理机会又不骚扰 |
| C8 回归挂 | 改了 `step` 签名 | `step` 只加字段不改签名语义，验收先跑 D17 |

---

## 十一、今天记住的三条

1. **ack 是人为动作，`step` 不碰它**。状态跃迁和人工确认是两个正交事实，混在一起会出现「自动重开悄悄清掉别人的确认」。
2. **收敛抑制通知、不抑制状态**。设备各自还是 ALERT，组告警只是「少发一条」，不是「当成没坏」。
3. **升级要翻倍窗口 + 上限**。一条告警等 30 分钟没人理说明第一级没看到，再等 30 分钟还升一级就够了，死等固定间隔只会刷屏。

---

## 十二、D19 预告

告警能收敛、能升级、能确认了。下一步是**闭环**：通知里带一个 ack 链接（webhook 收到后回写 `ack.py`），让运维在手机上点一下就确认，不用再登机器跑 CLI；以及**告警生命周期看板**——把 `state["devices"]` 里 `since / acknowledged / escalation_level` 渲染成一张当前告警总览表（谁在处理、升到第几级、卡了多久），这正是 D17 那张「状态机状态表」该长成的样子。
