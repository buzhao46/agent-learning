# Release v0.2 — 给 `snmpcheck` 接上 LLM 决策层（让项目名副其实成为 Agent）

- **Tag**：`v0.2`
- **日期**：2026-10-30（21 天训练收官之后追加）
- **范围**：`week1/snmpcheck/` 新增 LLM 决策 + 自然语言解读层
- **基线**：`v0.1`（规则驱动的告警闭环系统）

---

## 一、这个版本相对 v0.1 多了什么

v0.1 的 `snmpcheck` 只是「采集器 + 规则报告」（list / status），决策与文案都是
硬编码规则。v0.2 给它接上了**真正的 LLM 决策层**，让项目名副其实成为 Agent：

- **`snmpcheck agent` 子命令**：用 **ReAct 循环 + Function Calling** 让大模型
  **自己决定先查哪台设备、查哪些指标**，并在采集完成后**产出自然语言告警文案**
  （带处置优先级 hint）。
- **无 API Key 时 `RulePolicy` 规则兜底**：同一套骨架，把「大脑」换成确定性规则，
  保证没有 Key 也能端到端跑通、验收稳定全绿。
- **真采集复用 D8–D14 引擎**：决策层调用的工具底层是 `netprobe.py` 的**真 pysnmp
  采集**（GET/WALK + 七类错误分类），目标用内置 `fake_agent`（真实 UDP+BER 模拟
  设备），**无需真实交换机**即可端到端验证「真采集 → LLM 决策 → 自然语言解读」。

## 二、新增模块与职责

| 文件 | 职责 |
|---|---|
| `snmpcheck/netprobe.py` | 真 pysnmp 采集引擎：GET/WALK、七类 `ErrorKind` 错误分类、接口表三列按行号字典合并、uptime→TimeTicks |
| `snmpcheck/llm_client.py` | 双模式 LLM 客户端：有 `LLM_API_KEY` 走真实 OpenAI 兼容 SDK；无 Key 走 mock（`chat_tools` 无 Key 时抛错，强制走规则） |
| `snmpcheck/agent.py` | ReAct 决策循环（Function Calling）+ 结构化 issue + 自然语言解读层 + `RulePolicy` 规则兜底 |
| `snmpcheck/fake_agent.py` | 真 UDP+BER SNMP 模拟设备（sysDescr/uptime/sysName/ifTable，含中文接口名与故障开关），作为真采集目标 |
| `snmpcheck/check_agent.py` | 验收 A1–A5（退出码 / 模式行 / 采集动作 / 非空诊断 / 多台加练），5/5 通过 |

## 三、决策层怎么工作（面试可讲）

1. **ReAct 循环**：`run_agent` 主循环里，大脑每轮从工具集
   （`list_devices` / `get_device_health` / `get_interface_errors`）选一个动作 →
   执行 → 把 observation 回填 → 再决策，最多 `max_steps` 轮。
2. **结构化 issue 共用**：规则与 LLM 都先产出同一套结构化 issue（设备/级别/证据），
   再分别「规则渲染」或「LLM 自然语言化」，保证两条路径输出一致、可对比。
3. **解读层兜底**：`interpret()` 有 Key 时让 LLM 润色成自然语言（带处置 hint），
   无 Key 时退回规则渲染——不会因为没 Key 就崩。
4. **诊断始终打印**：`-q/--quiet` 只压汇总行，诊断结论不论何种模式都输出，避免
   「静默无输出」误以为成功。

## 四、工程门禁（本版本实测）

- ✅ **ruff**：`snmpcheck` 包 lint + format 全绿
- ✅ **mypy**：`snmpcheck` 包 9 文件零报错（pysnmp 无 stub 处精准 `type: ignore`，
  不 broad 关闭错误码）
- ✅ **pytest**：`check_agent` A1–A5 全绿（5/5）；`check_d7`(7/7) / `check_d17`(9/9)
  / `check_d18`(8/8) 回归全绿
- ✅ **覆盖率**：`snmpcheck` 包（含 `agent` + `netprobe`，排除 `fake_agent`/
  `__init__`/`check_*`）实测 **74.2%**
  - 较低的 `llm_client`(42.9%) 与 `agent` 的 LLM 分支是因为**真实 OpenAI 调用受
    API Key 门控**，无 Key 时只跑 mock / 规则兜底；这是诚实留白，CI 默认不覆盖这两支。

## 五、怎么跑

```bash
cd week1
.venv\Scripts\activate                       # Windows；Linux/Mac 用 source .venv/bin/activate
pip install -e . pysnmp pytest pytest-cov mypy ruff

# 1) LLM Agent 子命令（内置模拟设备，无需真交换机；无 Key 自动规则兜底）
snmpcheck agent                              # 默认 1 台，端口 1161
snmpcheck agent --ports 1161 1162            # 多台（A5 加练场景）
snmpcheck agent -q                           # 只压汇总行，诊断照常打印
snmpcheck agent --config devices.json        # 用真实设备清单（不起模拟）

# 2) 验收
python snmpcheck/check_agent.py              # A1–A5，5/5

# 3) 质量门禁
ruff check snmpcheck
mypy snmpcheck
pytest snmpcheck/check_agent.py snmpcheck/check_d7.py d17/check_d17.py d18/check_d18.py
```

> 想走**真实 LLM 决策**：`cp .env.example .env` 并填 `LLM_API_KEY` /
> `LLM_BASE_URL` / `LLM_MODEL`，再跑 `snmpcheck agent` 即可；不填则自动 `RulePolicy`。

## 六、已知限制（如实说）

1. 真实 LLM 分支（`llm_client` 的 OpenAI 路径、`agent` 的 LLM 决策分支）**未纳入
   CI 覆盖率**——必须真实 API Key 才能跑，属门控分支，非偷懒。
2. 仅实现 **SNMPv2c**；`fake_agent` 模拟的是单进程多端口的简化设备，不覆盖 v3/VLAN
   等真实复杂度。
3. `pysnmp` 每次 `getCmd` 约 0.7s 固定开销（新建 asyncio dispatcher），多台并发由
   `RulePolicy`/`agent` 串行定序，未做采集并发（那是 D14 的事，本层只管决策）。
4. `snmp-agent-demo/`（ReAct + Vue3 前端）是本仓库更早、更完整的 LLM Agent 演示，
   与本层思路一致但独立；`week1/day6_agent_llm.py` 是更轻的 ReAct 骨架。
