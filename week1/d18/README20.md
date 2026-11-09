# D20 · 通知聚合（digest）：别让一堆告警刷爆手机

**今天一句话**：D18 让「同组批量故障收敛成一条」了，D19 让「点链接就能确认」了——但还有个洞：**同一轮巡检里 20 台没分组的设备同时挂，照样发 20 条 `alert_open`**。今天加 **通知聚合（digest）**：把同一轮巡检里的新告警 / 升级分别聚成一条摘要（每台带自己的确认链接），升级摘要自带「已等待 X 分钟 + 当前级别」的上下文。

预计 40–60 分钟。在 D18/D19 的 `alert.py` / `notify.py` / `alerts.py` 上扩展，新增 `check_d20.py`，不改既有语义。

---

## 一、今天的目录

D20 直接在 D18/D19 目录里加文件（每天都是已落地快照，不需要复制）：

```
week1/d18/
├── alert.py          ← 扩展：同轮新告警/升级聚合成摘要（--digest-threshold / --no-digest）
├── alerts.py         ← 扩展：新增 EVENT_DIGEST / EVENT_ESC_DIGEST 两个事件类型
├── notify.py         ← 扩展：console 渲染摘要（列出每条 items 及其确认链接）
├── check_d20.py      ← 新增：C1–C8 共 8 个用例
├── README20.md       ← 本文件（按 README+天数 约定）
└── （D19 的 acktoken / ack_server / board 等不变）
```

两个刻意选择：

- **聚合是「发送层」的事，不是状态机的事**。和 D18 收敛一样，聚合只决定「怎么发」，不碰 `step` 的跃迁语义。20 台同时挂，状态机照样逐台记 ALERT，只是发送时把它们并成一条。
- **聚合和收敛是正交的两层，互补不冲突**。收敛基于**拓扑**（已知组，组内 ≥60% 才合并）；聚合基于**时间轮次**（同一轮任意一批 ≥N 条就并）。一台既在组里、又和别的组外设备同轮挂，两条规则各管各的——组里那几台走 group_alert，组外那几台走 digest。

---

## 二、先看 D19 缺什么

一个真实场景暴露的缺口：

- **散装批量故障仍刷屏**。D18 的收敛只认「配置文件里写好的组」。可现实里，一大批设备同时挂，往往是因为上游交换机 / 电源 / 骨干链路，它们不一定被预先归到一个组里。这 20 条 `alert_open` 一条不少，手机照炸。
- **升级通知没有上下文**。D18 的 `alert_escalate` 原因里写了「已等待 X 分钟」，但**多条设备同时升级**时，还是各发各的，没法一眼看清「这一轮有 3 台同时升到 L1」。

一句话：**D18 收敛管「已知组的批量故障」，D20 聚合管「任意一批同时发生的告警」+ 升级汇总**。

---

## 三、TODO 1：新事件类型（alerts.py）

`step` 只产 `alert_open / close / stale`，`maybe_escalate` 只产 `alert_escalate`，`correlate` 只产 `group_alert`。聚合产物是「发送层的合成事件」，不在状态机生产列表里，所以在 `alerts.py` 只**登记两个常量**，供 `alert.py` 构造摘要事件时用：

```python
EVENT_DIGEST = "alert_digest"        # 本轮新增告警的摘要
EVENT_ESC_DIGEST = "escalate_digest" # 本轮升级事件的摘要
```

不往 `step` 里塞，是因为摘要是「同一轮多个事件的组合视图」，没有单一的状态跃迁对应它——硬塞进 `Event` 生产链只会让状态机脏。

---

## 四、TODO 2：聚合逻辑（alert.py）

核心在 `main` 的「新事件发送」前，先把 `all_events` 按类型拆开：

```python
opens = [ev for ev in all_events if ev.kind == EVENT_OPEN]
escalations = [ev for ev in all_events if ev.kind == EVENT_ESCALATE]
others = [ev for ev in all_events if ev.kind not in (EVENT_OPEN, EVENT_ESCALATE)]

# 静默的设备仍单独走 dispatch（会打印 [静默] 且不发、不入队）；
# 只有「未静默」的同类型事件才进摘要。
active_opens = [ev for ev in opens if not is_silenced(ev.label, now, silences)[0]]
active_escalations = [ev for ev in escalations if not is_silenced(ev.label, now, silences)[0]]

to_send = list(others)
if not args.no_digest and len(active_opens) >= args.digest_threshold:
    to_send.append(build_digest(active_opens, state, args, now, EVENT_DIGEST))
else:
    to_send.extend(active_opens)
# escalations 同理 → escalate_digest
```

`build_digest` 把一批事件并成一条摘要 dict，每条 `items` 带各自的确认链接和上下文：

```python
def build_digest(events, state, args, now, kind):
    is_escalate = kind == EVENT_ESC_DIGEST
    items = []
    for ev in events:
        dev = state["devices"].get(ev.label, {})
        item = {"device": ev.label, "ack_url": ack_url_for(EVENT_OPEN, ev.label, args), "reason": ev.reason}
        if is_escalate:
            item["level"] = dev.get("escalation_level")
            item["waited_minutes"] = 距 since 的分钟数   # 升级上下文
        items.append(item)
    return {"id": f"{kind}:digest:{stamp}", "kind": kind, "device": "digest",
            "reason": f"本轮{'升级' if is_escalate else '新增'} {len(items)} 台", "items": items, ...}
```

**阈值与开关**：`--digest-threshold N`（默认 2，≥N 条才聚合；设为 1 则永远聚合，设为很大的数等于关闭）；`--no-digest` 完全关掉聚合，回到「每条单独发」。

**渠道**：`alert_digest` 走普通渠道（`--channel`），`escalate_digest` 走升级渠道（`--escalate-channel`）——和单条事件一致。

**静默不丢提示**：被静默的设备不进摘要，但仍单独喂给 `dispatch`，由它在原地打印 `[静默]` 且不发、不入队。这样「静默只抑制通知、不阻止状态推进」的 D18/D17 契约不受聚合影响（D20 的回归里专门守住这条）。

---

## 五、TODO 3：摘要渲染（notify.py）

`send` 的 `console` 分支对两种摘要做特殊渲染：

```
[通知] 告警摘要 本轮新增 3 台（已聚合，避免刷屏）
    - 10.0.0.9:161          确认：http://.../ack?device=...&sig=...
    - 10.0.0.9:162          确认：...
    - 10.0.0.9:163          确认：...

[通知] 升级摘要 本轮升级 3 台
    - 10.0.0.9:161  级别L1 等待200分钟  确认：...
    - 10.0.0.9:162  级别L1 等待200分钟  确认：...
    - 10.0.0.9:163  级别L1 等待200分钟  确认：...
```

`file` / `webhook` 渠道不用改：`json.dumps(event)` 天然把 `items` 数组（含每条 `ack_url`）写进日志 / POST 出去，下游照常消费。

---

## 六、TODO 4：验收（check_d20.py）

直接构造 `scan_*.json`（时间戳用「现在 - N 分钟」；升级场景用 `rewind_since` 把 since 拨回过去），8 个用例：

| 项 | 查什么 |
|---|---|
| C1 | 单台告警仍单独发 `alert_open`，不聚合 |
| C2 | 3 台未分组同时挂 → 只发 1 条 `alert_digest`（不再 3 条 `alert_open`） |
| C3 | digest 每条 `items` 都带各自的确认链接（device 链接） |
| C4 | 3 台同时升级 → 只发 1 条 `escalate_digest`（含级别 + 等待时长上下文），无单独 `alert_escalate` |
| C5 | 组收敛与 digest 共存：组内 3 台 → 1 条 `group_alert`；组外 2 台 → 1 条 `alert_digest(2)`，无单独 open |
| C6 | 阈值控制：`--digest-threshold 3` 时 2 台仍单独发 |
| C7 | `--no-digest` 关闭聚合：3 台挂发 3 条 `alert_open` |
| C8 | 回归：D19（8/8）+ D18（8/8）+ D17（9/9） |

```powershell
..\.venv\Scripts\python.exe check_d20.py     # 通过 8/8
..\.venv\Scripts\python.exe check_d19.py     # 仍 8/8（回归）
..\.venv\Scripts\python.exe check_d18.py     # 仍 8/8（回归）
..\.venv\Scripts\python.exe check_d17.py     # 仍 9/9（回归）
```

---

## 七、自检（端到端）

```powershell
# 1. 停掉假设备，连采 3 次（5 台没分组的设备同时挂）
# 2. 跑告警，看 out/alerts.log
..\.venv\Scripts\python.exe alert.py --report-dir out --state out/alert_state.json \
    --channel file --ack-base-url http://127.0.0.1:8731 --ack-secret x
#    应只看到 1 条 alert_digest（items=5），而不是 5 条 alert_open

# 3. 故意把 --digest-threshold 调大或 --no-digest，验证可关
```

---

## 八、卡点表（预计会踩的）

| 现象 | 原因 | 改法 |
|---|---|---|
| 回归里「没有提示静默」 | 聚合时把静默设备从批次里滤掉，导致没走到 `dispatch` 的 `[静默]` 打印 | 静默设备仍单独喂 `dispatch`（只排除出摘要），`[静默]` 提示与「不发/不入队」都不丢 |
| digest 里每台没有确认链接 | 建摘要时没给每条 items 算 `ack_url` | `build_digest` 内对每条调 `ack_url_for(EVENT_OPEN, label)` |
| 阈值 2 时单台也被聚合 | 阈值判断写成了 `>` 而非 `>=` | `>= args.digest_threshold`（默认 2，单台不触发） |
| escalate_digest 走错渠道 | `channel_for` 没把 `escalate_digest` 归到升级渠道 | `channel_for` 里 `EVENT_ESC_DIGEST` 与 `EVENT_ESCALATE` / `EVENT_GROUP` 同组 |
| 组收敛的组里设备又进了 digest | 混淆了收敛与聚合的归属 | 收敛已在 `correlate` 把组内 open 丢弃；聚合拿到的 `opens` 本来就不含组内设备 |

---

## 九、今天记住的三条

1. **聚合是发送层降噪，收敛是拓扑降噪，两者正交互补**。收敛管「已知组」，聚合管「同一轮任意一批」——别想用一个取代另一个。
2. **摘要里的每台设备都要带自己的确认链接**。聚合的是「消息数量」，不是「确认责任」，谁挂了点谁的链接。
3. **静默的提示语义不能因为聚合丢**。被静默的设备不进摘要，但必须仍走 `dispatch` 打印 `[静默]` 且不发、不入队——这是 D17/D18 立过的契约。

---

## 十、D21 预告

告警能收敛、能升级、能点链接确认、能看总览、能把同轮批量故障聚成一条了。下一步往「**闭环深化 + 可观测性**」走，三个候选（挑一至二）：

- **值班排班**：ack 链接的 `by=` 默认填当前值班人（接一张排班表：日期/班次 → 人名），不用每个人手动写名字；升级摘要直接 @ 当前值班人。
- **历史时间线**：把 `state["devices"]` 的 `since / last_escalated_at / acked_at` 攒成「单设备告警生命周期」记录，复盘「这台机器这个月炸了几次、每次卡多久才有人管」（需要在状态里追加一个历史 episode 列表）。
- **通知去重跨轮**：当前聚合只管「同一轮」。如果一台设备连续 N 轮都 ALERT 且没 ack，是否每轮都发摘要？可以加一个「摘要重发冷却」——只有级别变化 / 新设备加入 / 超时未 ack 才重发，纯重复的轮次静默。
