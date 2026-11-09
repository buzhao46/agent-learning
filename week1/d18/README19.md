# D19 · ack 闭环与告警生命周期看板

**今天一句话**：D18 让告警能收敛、能升级、能确认了——但确认还得登机器跑 `ack.py`。今天补上「最后一公里」：**通知里带一条签过名的确认链接，运维在手机上点一下就确认**；再加一张**告警生命周期看板**，把 `state["devices"]` 里的 `since / acknowledged / escalation_level` 渲染成「谁在处理、升到第几级、卡了多久」的总览。

预计 40–60 分钟。在 D18 的 `alert.py / ack.py / notify.py / state.py` 上扩展，新增 `acktoken.py`、`ack_server.py`、`board.py`、`check_d19.py`。不改 D18 的语义，只加「链接」和「看板」两条旁路。

---

## 一、今天的目录

D19 直接在 D18 目录里加文件（D18 是已落地的快照，不需要再复制）：

```
week1/d18/
├── alert.py          ← 扩展：--ack-base-url / --ack-secret，发送时给事件注入确认链接
├── ack.py            ← 扩展：把「算目标 / 写确认」抽成 resolve_targets / apply_ack
├── notify.py         ← 扩展：console 打印确认链接，file/webhook 把 ack_url 写进 JSON
├── state.py          ← 不动（D18 已带 groups / ack / escalation 字段）
├── alerts.py         ← 不动
├── acktoken.py       ← 新增：ack 链接的 HMAC 签名 / 校验（无第三方依赖）
├── ack_server.py     ← 新增：HTTP 闭环服务（/ack 确认 + / 看板）
├── board.py          ← 新增：告警生命周期看板（CLI + 供 server 渲染网页）
├── groups.json       ← 沿用 D18
└── check_d19.py      ← 新增：C1–C8 共 8 个用例
```

两个刻意选择（和 D15→D19 一脉相承）：

- **不把 ack 逻辑塞进状态机**。`ack_server` 回写状态时，只调 D18 已有的 `apply_ack`（改 `acknowledged`），`step` 依旧不碰 ack 字段。确认是「人为动作」，状态跃迁是「自动逻辑」，两条线不能混。
- **看板只读不写**。看板只是把现状讲清楚，不碰状态文件；写权限仍在 `alert.py` / `ack.py` / `ack_server`。

---

## 二、先看 D18 缺什么

三个开口，都能在 D18 代码里指到：

1. **ack 还停在生产力的上个世纪**。`ack.py` 是 CLI，运维得 SSH 上机器、找 `state` 路径、敲 `--device`。告警发到手机了，确认还得回工位——这段链路断了，D18 的 ack 等于没真正闭环。
2. **没有「确认入口」这种东西**。`alerts.Event.to_dict()` 只有 `kind/device/reason/...`，没有「怎么确认」的线索。运维收到通知，除了回机器什么也做不了。
3. **没有总览**。D17 画过一张「状态机状态表」，但那是给开发者看的跃迁图；运维要的是**此刻**「哪几台还红着、谁认领了、升到第几级、卡了多久」——`state["devices"]` 里有数据（`since/acknowledged/escalation_level`），但没有人把它变成一屏能看完的表。

一句话：**D18 让告警「能被确认」，D19 让告警「点一下就确认」且「一眼看清现状」**。

---

## 三、TODO 1：ack 链接签名（acktoken.py）

链接会被发到聊天 / 邮件里，谁都能点。不签名的话，任何人都能随便确认任意告警，甚至写脚本批量确认、把真实故障全「消音」。所以链接必须带签名：

```python
# acktoken.py
def sign_device(secret, label) -> str:          # HMAC-SHA256(secret, label) 取前 32 位 hex
def sign_group(secret, group) -> str:           # 独立命名空间：HMAC(secret, "group:" + group)
def verify_device(secret, label, sig) -> bool:  # compare_digest 防时序攻击
def verify_group(secret, group, sig) -> bool:
def device_ack_url(base_url, label, secret) -> str:  # base/ack?device=<label>&sig=<sig>
def group_ack_url(base_url, group, secret) -> str:   # base/ack?group=<group>&sig=<sig>
```

**为什么 device / group 用两个独立命名空间**：一条 `group` 链接确认整组，权限比 `device` 大。如果共用一个签名函数，拿到某台设备的 sig 就能伪造组链接。命名空间隔离后，`sign_device` 的 sig 验不过 `verify_group`。

**服务端无状态**：链接自己携带 `device + sig`，服务端不存表，靠同一密钥复算比对。换密钥 = 让所有旧链接失效，运维友好。

`secret` 默认是 `change-me-in-prod` 并会打印警告；生产必须 `--ack-secret`。

---

## 四、TODO 2：通知内嵌确认链接（notify.py + alert.py）

`alert.py` 发送每条事件前，按 kind 算链接并注入 `ack_url`：

```python
def ack_url_for(kind, label, args):
    if not args.ack_base_url:
        return None
    if kind == EVENT_GROUP:
        return acktoken.group_ack_url(args.ack_base_url, label, args.ack_secret)
    return acktoken.device_ack_url(args.ack_base_url, label, args.ack_secret)

# 新事件循环里：
payload = event.to_dict()
url = ack_url_for(event.kind, event.label, args)
if url:
    payload["ack_url"] = url
```

`notify.py` 的改动极小：

- `console` 渠道：除了打印通知，多打一行 `确认：<url>`；
- `file` / `webhook` 渠道：`payload` 已经是完整 dict，`json.dumps` 天然带上 `ack_url`。

`alert.py` 加参数：`--ack-base-url`（如 `http://host:8731`）、`--ack-secret`。**补发 pending 时也重新算链接**，保证重发出来的通知照样可点。

---

## 五、TODO 3：ack 逻辑抽出来（ack.py）

D18 的 `ack.py` 把「解析目标 + 写确认 + 存盘」全写在 `main` 里。D19 把它拆成两个纯函数，CLI 和 `ack_server` 共用：

```python
def resolve_targets(state, groups_path, device, group) -> set[str]:
    """--device 单台，或 --group 展开成组内所有 label。"""

def apply_ack(state, targets, by, note, now) -> int:
    """对 targets 里仍是 ALERT 的设备写 acknowledged，返回确认了几台。直接改 state。"""
```

`ack_server` 的 `/ack` 处理直接调这两个：解析 → 校验签名 → `apply_ack` → `save_state`。`ack.py` 的 CLI 不变，还是 `python ack.py --device ... --by alice`。

---

## 六、TODO 4：闭环 HTTP 服务（ack_server.py）

一个 `ThreadingHTTPServer`，无框架、无额外依赖：

```powershell
python ack_server.py --state out/alert_state.json --groups groups.json \
    --host 0.0.0.0 --port 8731 --secret my-secret
```

路由：

| 路径 | 作用 |
|---|---|
| `GET /` | 看板网页（复用 `board.render_html`） |
| `GET /api/board` | 看板 JSON |
| `GET /ack?device=X&sig=S&by=Y` | 校验签名 → `apply_ack` 写回 → 显示「已确认」 |
| `GET /ack?group=G&sig=S&by=Y` | 同上，但确认整组 |

安全边界（写进代码注释）：**本服务只开放「确认」，不开放「取消确认 / 改状态」**。确认是不可逆的人为动作，不允许通过链接反向操作——否则有人点一下就把别人的 ack 撤了，更乱。

点链接的完整链路：

```
alert.py 发通知 ──带 ack_url──> 聊天/邮件 ──运维点击──> ack_server /ack
        └── 校验签名 ──> apply_ack 写 acknowledged=True ──> 下次 alert.py 跑，该设备不再升级
```

---

## 七、TODO 5：告警生命周期看板（board.py）

纯读 `state`（`collect`），不写。一张表讲清「现在谁在处理」：

| 维度 | 字段 | 说明 |
|---|---|---|
| 设备行 | `since → 持续时长` | `状态开了多久`，>1h 标 `X.Xh`，否则 `Xm` |
| 设备行 | `acknowledged / acked_by` | 谁认领了，没认领标「未确认」 |
| 设备行 | `escalation_level` | 升到第几级（L1/L2），没升标 `-` |
| 设备行 | `group` | 所属组（来自 groups.json 逆索引） |
| 组行 | `alerted / total` | 组内几台红着，占比 |
| 汇总 | `alerts / acked / unacked / escalated / groups_alerting` | 一屏看完的 KPI |

两种输出：`python board.py --state ... --groups ...` 打印文本表；`--html` 打印网页；`--json` 给程序消费（server 的 `/api/board` 用的就是它）。

**只显示 ALERT**：看板是「当前故障总览」，OK / UNKNOWN 不进表。组也只看 `status == ALERT` 的。组恢复后自动退出组行，但组内仍坏的设备**保留设备行**——和 D18「收敛抑制通知、不抑制状态」一脉相承。

---

## 八、TODO 6：验收（check_d19.py）

直接构造 `scan_*.json`（和 D17/D18 同一手法，时间戳用「现在 - N 分钟」），外加**真起一个 HTTP 服务**发请求，不走 mock：

| 项 | 查什么 |
|---|---|
| C1 | ack 链接签名：合法可验、伪造 / 换密钥 / 空签名均拒；device/group 命名空间隔离 |
| C2 | 通知内嵌确认链接：`alert_open` 的 `ack_url` 含 `/ack?device=` |
| C3 | 看板渲染 ALERT 设备：持续时长 / 确认人 / 升级级别 / 组 齐全 |
| C4 | 闭环端到端：`alert` 发链接 → 点链接（真实 HTTP）→ 状态 `acknowledged=True` |
| C5 | 伪造签名点链接：服务端 403，状态不被改动 |
| C6 | 组确认链接：一次点完组内 3 台 |
| C7 | 看板反映组状态：组 ALERT 进组行；组恢复后退出组行但保留仍坏设备行 |
| C8 | 回归：D18（8/8）+ D17（9/9） |

```powershell
..\.venv\Scripts\python.exe check_d19.py     # 通过 8/8
..\.venv\Scripts\python.exe check_d18.py     # 仍 8/8（回归）
..\.venv\Scripts\python.exe check_d17.py     # 仍 9/9（回归）
```

---

## 九、自检（端到端，真机 + 真服务）

```powershell
# 1. 起假设备，采一次正常的
..\.venv\Scripts\python.exe fake_agent.py --port 1161
..\.venv\Scripts\python.exe probe.py --config devices.json --report-dir out

# 2. 停掉假设备，连采 3 次（设备进 ALERT）
# 3. 起闭环服务
..\.venv\Scripts\python.exe ack_server.py --state out/alert_state.json --groups groups.json --port 8731 --secret x

# 4. 跑告警（带确认链接）
..\.venv\Scripts\python.exe alert.py --report-dir out --state out/alert_state.json --channel file \
    --ack-base-url http://127.0.0.1:8731 --ack-secret x

# 5. 浏览器打开 http://127.0.0.1:8731/ 看看板；点通知里的确认链接 → 状态被确认
# 6. 再跑一次 alert.py → 已确认的设备不再升级
```

注意 `out/alerts.log` 是 UTF-8，Windows 控制台默认 GBK 会乱码，用 `Get-Content -Encoding utf8` 看；确认链接里的中文组名已经 `urllib.parse.quote` 过，不会有编码问题。

---

## 十、卡点表（预计会踩的）

| 现象 | 原因 | 改法 |
|---|---|---|
| 点链接 403 | 服务端 `--secret` 和发链接时的 `--ack-secret` 不一致 | 两端用同一个密钥；测试里抽成常量 `SECRET` |
| 组告警 / 升级事件没进日志 | 它们走 `--escalate-channel`（默认 webhook），没给 URL 就进 pending | 验收时显式 `--escalate-channel file`，真实环境配 webhook |
| 链接中文组名乱码 | 没 quote | `acktoken` 用 `urllib.parse.quote` 编码 label/group |
| 伪造链接能消音 | 没签名 | 链接必须带 HMAC sig，服务端 `compare_digest` 校验 |
| 取消确认也能点 | 服务开放了写反向字段 | 服务只开放「确认」一条路径 |
| 看板把恢复的设备也算进去 | 组恢复后没清组状态 | 收敛逻辑（D18）已经把组状态清回 OK，看板据此退出组行 |

---

## 十一、今天记住的三条

1. **确认是不可逆的人为动作，链接只负责「确认」**。状态跃迁（自动）和人工确认（人为）是两条正交线，闭环服务不碰 `step`、不开放反向写。
2. **签名把「谁能用链接消音」锁死**。没密钥造不出合法 sig，换密钥让所有旧链接失效——这是把 ack 暴露到公网前的必做项。
3. **看板只读不写，和状态机解耦**。看板只是把 `since / acknowledged / escalation_level` 翻成能一眼看完的表，写权限仍在 `alert.py` / `ack.py` / `ack_server`。

---

## 十二、D20 预告

告警能收敛、能升级、能点链接确认、能看总览了。下一步往「降噪 + 闭环深化」走：

- **通知聚合**：同一条告警在 L1→L2 之间别反复发相同内容，升级事件带「已等待 X 分钟 + 之前通知过」的上下文；
- **值班排班**：ack 链接的 `by=` 默认填当前值班人（接排班表），不用每人手动写名字；
- **历史时间线**：把 `state["devices"]` 的 `since / last_escalated_at / acked_at` 画成单设备的告警生命周期折线，复盘「这台机器这个月炸了几次、每次卡多久才有人管」。
