# D21 · 值班排班：ack 链接 by= 默认填当前值班人

**今天一句话**：D19 让「点链接就能确认」了，D20 让「同轮批量故障聚成一条」了——但点链接时 `by=` 还得每个人手填。今天加 **值班排班**：配一张排班表，确认链接的 `by=` 默认填上「当前当班人」，升级摘要直接 `@ 值班人`，谁该负责一眼可见。

预计 40–60 分钟。在 D19/D20 的 `alert.py` / `acktoken.py` 上扩展，新增 `roster.py` 与 `check_d21.py`，不改既有语义。

---

## 一、今天的目录

```
week1/d18/
├── roster.py          ← 新增：排班表加载 + on_duty 查询（日期+班次 → 人）
├── acktoken.py        ← 扩展：device/group 链接可带 by= 预填（不签名）
├── alert.py           ← 扩展：--roster / --shift，ack_url_for 默认填值班人；升级摘要 @值班人
├── check_d21.py       ← 新增：C1–C8 共 8 个用例
├── README21.md        ← 本文件（按 README+天数 约定）
└── （D19 的 ack_server / board / D20 的 digest 等不变）
```

三个刻意选择：

- **`by=` 是「谁点的」的预填，不是安全字段，不参与签名**。签名只锁 `device` / `group`（防别人消音你的告警）。值班人是展示/责任归属用的，点链接的人照样能改成自己——这本来就该允许（替班、代确认）。
- **排班表缺失时静默退化，不兜底填人**。查不到今班 → 链接不带 `by=`，回到 D19 行为；绝不默默填一个错的人，误导谁该负责。
- **只改「生成链接」这一处**。状态机 `step`、升级 `maybe_escalate`、闭环 `ack_server` 完全不动——`by` 本来就是服务端早已读取的查询字段（默认「未知」），今天只是让 `alert.py` 在造链接时把它预填好。

---

## 二、先看 D20 缺什么

一个真实场景暴露的缺口：

- **确认人每次都要手填**。D19 的链接是 `?device=X&sig=S`，点开确认页 `by` 默认「未知」，得自己打名字。半夜 20 台同时告警，谁当班、谁该挨个确认，链接里看不出来。
- **升级通知不知道该 @ 谁**。D20 的升级摘要只说「本轮升级 N 台」，没说该找当班人。

一句话：**D19 解决了「能不能一键确认」，D21 解决「确认人/责任人默认是谁」**。

---

## 三、TODO 1：排班表（roster.py）

纯 JSON，零依赖：

```json
{"roster": [
    {"date": "2026-11-09", "shift": "day",   "person": "张三"},
    {"date": "2026-11-09", "shift": "night", "person": "李四"}
]}
```

查询函数：

```python
def default_shift(now):  # 8–19 点算 day，其余 night
    return "day" if 8 <= now.hour < 20 else "night"


def on_duty(path, now, shift=None):
    rows = load_roster(path)  # 文件缺失/损坏 → 空列表，不崩
    if not rows:
        return None
    shift = shift or default_shift(now)
    date = now.strftime("%Y-%m-%d")
    for row in rows:
        if row.get("date") == date and row.get("shift") == shift and row.get("person"):
            return str(row["person"])
    return None  # 今天没排班 → None（链接不带 by）
```

`load_roster` 对「文件不存在 / 不是合法 JSON / 不是数组」一律返回空列表并打警告——排班表是运营配置，坏了不能让告警流程崩。

---

## 四、TODO 2：链接带上 by=（acktoken.py）

`device_ack_url` / `group_ack_url` 加一个可选的 `by` 参数，**追加在 sig 之后、不参与签名**：

```python
def device_ack_url(base_url, label, secret, by=None):
    url = f"{base_url.rstrip('/')}/ack?device={quote(label)}&sig={sign_device(secret, label)}"
    if by:
        url += f"&by={quote(str(by))}"
    return url
```

为什么放 sig 后面且不签名：`by` 是「谁点了这个链接」的自述，改它不会破坏「只有持密钥者才能生成合法 sig」这条安全边界。服务端 `ack_server` 早就读 `by` 查询字段（默认「未知」），今天只是把默认从「未知」变成「当班人」。

---

## 五、TODO 3：默认填值班人（alert.py）

`ack_url_for` 多拿一个 `now`，查到当班人就透传给 `acktoken`：

```python
def ack_url_for(kind, label, args, now):
    if not args.ack_base_url:
        return None
    by = roster.on_duty(args.roster, now, args.shift) if args.roster else None
    if kind == EVENT_GROUP:
        return acktoken.group_ack_url(args.ack_base_url, label, args.ack_secret, by=by)
    return acktoken.device_ack_url(args.ack_base_url, label, args.ack_secret, by=by)
```

两处调用（补发 pending、新事件循环）都补上 `now`。升级摘要 `build_digest` 里也复用同一份 `on_duty_person`，在 `reason` 末尾补一句 `；@值班人 张三`——升级通知直接指明该找谁。

新增参数：`--roster <path>`、`--shift day|night`（不传则按当前小时推断）。

---

## 六、TODO 4：验收（check_d21.py）

直接构造 `scan_*.json` + `roster.json`，8 个用例：

| 项 | 查什么 |
|---|---|
| C1 | 无 roster → ack 链接不带 by（向后兼容 D19） |
| C2 | 配 roster 且日期/班次命中 → 链接带 by=张三 |
| C3 | 组确认链接也带 by（group_ack_url） |
| C4 | roster 日期不匹配（今天没排班）→ 链接不带 by |
| C5 | 升级摘要 reason 含 @值班人 |
| C6 | 端到端：带 by=张三 的链接点一下 → acknowledged=True 且 acked_by=张三（真起 HTTP 服务） |
| C7 | by 是自由字段：把链接里 by=张三 改成 by=李四 再点 → acked_by=李四 |
| C8 | 回归：D20（8/8）+ D19（8/8）+ D18（8/8）+ D17（9/9） |

```powershell
..\.venv\Scripts\python.exe check_d21.py     # 通过 8/8
..\.venv\Scripts\python.exe check_d20.py     # 仍 8/8（回归）
..\.venv\Scripts\python.exe check_d19.py     # 仍 8/8（回归）
..\.venv\Scripts\python.exe check_d18.py     # 仍 8/8（回归）
..\.venv\Scripts\python.exe check_d17.py     # 仍 9/9（回归）
```

C6 / C7 是真起 `ack_server`、用同一密钥签名、发 HTTP 请求验证整条链路：C6 验证默认 `by` 落到状态，C7 验证 `by` 可改（仍 200、状态记成改后的人）。

---

## 七、自检（端到端）

```powershell
# 1. 准备一张今天的排班表
#    {"roster":[{"date":"2026-11-09","shift":"day","person":"张三"}]}
# 2. 停掉假设备，连采 3 次，跑告警
..\.venv\Scripts\python.exe alert.py --report-dir out --state out/alert_state.json \
    --channel file --ack-base-url http://127.0.0.1:8731 --ack-secret x \
    --roster roster.json --shift day
#    看 out/alerts.log 里 alert_open 的 ack_url 应带 &by=张三
```

---

## 八、卡点表（预计会踩的）

| 现象 | 原因 | 改法 |
|---|---|---|
| 测试断言「链接不带 by」误报 | 链接里 by=张三 被 URL 编码成 %E5%BC%A0%E4%B8%89，按字面字符串比对不到 | 比对前 `urllib.parse.unquote(url)` |
| 端到端 403 | alert 用的 `--ack-secret` 和服务端 `--secret` 不一致 | 测试里两边用同一个 SECRET |
| 今天没排班却填了人 | 排班表日期写死成过去/未来 | 测试用 `datetime.now().strftime("%Y-%m-%d")` 动态生成 |
| 班次对不上 | 没传 --shift 且当前小时落在 night | 测试显式传 --shift day，绕过依赖真实时间 |
| 改 by 后确认失效 | 误把 by 也塞进签名 | by 不签名，改了只是改「谁点」，sig 仍有效 |

---

## 九、今天记住的三条

1. **确认人 = 责任归属，不是安全边界**。签名锁死「谁能消音」，值班人只是默认填的便利项，允许被改。
2. **运营配置损坏要优雅退化**。排班表读不出来 → 不带 by、回到 D19，而不是崩或填错人。
3. **最小改动原则**：状态机 / 升级 / 闭环一个都不碰，只在「造链接」这一处接入排班——既降低了回归面，也让「责任归属」和「确认动作」解耦。

---

## 十、D22 预告

告警能收敛、能升级、能点链接确认、能看总览、能聚合降噪、能默认填值班人了。下一步往「**可观测性 / 复盘**」走，两个候选：

- **历史时间线**：把 `state["devices"]` 的 `since / last_escalated_at / acked_at` 攒成「单设备告警生命周期」记录，复盘「这台机器这个月炸了几次、每次卡多久才有人管」（需要在状态里追加一个历史 episode 列表，并在 board 上加一个时间线视图）。
- **通知跨轮去重冷却**：澄清一下——当前 `alert_open` / `alert_escalate` 只在「跃迁 / 跨级」时产生，持续 ALERT 不会每轮重发，所以严格说没有「跨轮刷屏」的问题。如果要把它做得更稳，可以加「摘要重发冷却」：只有级别变化 / 新设备加入 / 超时未 ack 才重发摘要，纯重复的轮次静默（防御性，非必需）。
