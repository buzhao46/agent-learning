# snmpcheck 是怎么接上 LLM 的（v0.2 实现步骤）

> **一句话**：v0.1 的 `snmpcheck` 只是「采集器 + 规则报告」，决策和文案都是硬编码。
> 本文件记录 **v0.2 把 LLM 决策层真正接进去的每一步**——从一个空壳到
> `snmpcheck agent` 能端到端跑「真 pysnmp 采集 → LLM 决策 → 自然语言解读」。

> **为什么加**：项目曾被问「这个哪里用到了大模型，怎么能叫 Agent」。加完后它才名副其实：
> LLM 真的在**决定采什么、按什么顺序**（决策），并把结论**说成人话**（解读），
> 而不是把规则系统硬叫成 Agent。

---

## 零、最终架构（先看全景，再看每步）

```
snmpcheck agent  (snmpcheck.py: cmd_agent)          ← 入口：拉起模拟设备 / 读设备清单
        │
        ▼
   run_agent()  (agent.py)                          ← ReAct 主循环（决策）
        │  llm.chat_tools(...)  ──► 模型选工具、定顺序   （有 Key）
        │  RulePolicy.next_call() ────────────────────  （无 Key 兜底）
        ▼
   execute_tool()  (agent.py)                       ← 执行工具（异常转 {"error":...}）
        │
        ▼
   netprobe.py  ─── 真 pysnmp GET/WALK ──────────►  fake_agent.py（真 UDP+BER 模拟设备）
        │
        ▼
   _build_issues()（规则判断） → interpret()/_llm_render()（LLM 说人话）  ← 解读层
```

两条路径的分叉点只有一处：`agent.py:308` 的 `use_llm = llm.config.ready`。
**有 Key → 模型驱动；无 Key → 规则驱动。同一套骨架，可替换的大脑。**

---

## 一、步骤 0：准备（依赖与目录）

```bash
cd week1
.venv\Scripts\activate
pip install pysnmp openai        # pysnmp 真采集；openai 仅真实模式需要（mock 不用）
```

包内最终新增 5 个文件（都在 `week1/snmpcheck/`）：

| 文件 | 角色 | 依赖谁 |
|---|---|---|
| `llm_client.py` | 双模式 LLM 客户端 | openai（可选） |
| `netprobe.py` | 真 pysnmp 采集后端（工具的真实现） | pysnmp |
| `fake_agent.py` | 真 UDP+BER 模拟设备（采集目标） | pysnmp |
| `agent.py` | 决策 + 解读层 | `llm_client` / `netprobe` |
| `check_agent.py` | 验收 A1–A5 | 子进程跑 CLI |

> `netprobe.py` 与同包旧的 `probe.py`（D7 的模拟采集）**分开**：`probe` 是对着
> 确定性公式假装采，`netprobe` 是真的发 UDP、真的会超时、真的拿到接口表。

---

## 二、步骤 1：`llm_client.py`（先有「能调模型」的薄封装）

**目标**：一个客户端，**有 Key 走真实 OpenAI 兼容接口，没 Key 自动降级 mock**，保证
任何机器都能跑，不因为没申请到 Key 就卡死学习。

三个关键点（照抄结构即可）：

```python
# 1) 配置：从 .env / 环境变量读，.ready 判断有没有 Key
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    @classmethod
    def load(cls) -> "LLMConfig": ...          # 读 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
    @property
    def ready(self) -> bool: return bool(self.api_key.strip())

# 2) 普通对话：无 Key 返回 [mock] 文本；有 Key 调 SDK
class LLMClient:
    def chat(self, messages, *, temperature=0.7, max_tokens=512, stream=False) -> str: ...

    # 3) Function Calling：无 Key 直接抛，强制调用方走规则
    def chat_tools(self, messages, tools, *, temperature=0.2) -> dict:
        if not self.config.ready:
            raise RuntimeError("chat_tools 需要真实 Key，mock 模式下请用规则决策器")
        ...
        return {"content": msg.content,
                "tool_calls": [{"id","name","arguments","raw_arguments"}, ...]}
```

**设计决策（面试点）**：
- `chat` 无 Key 走 mock（能跑通流程），但 `chat_tools` 无 Key **直接抛**——因为
  Function Calling 无法模拟，硬编一个假决策会污染「到底是模型在决策吗」这个问题。
- 模型偶尔吐非法 JSON，用 `_safe_json()` 兜住（`json.loads(raw)` 失败给 `{}`），
  别让整个循环崩。

自检：`python -m snmpcheck.llm_client` → 打印 `配置：mock 模式…` + 一句回复。

---

## 三、步骤 2：`netprobe.py`（工具的真正后端：真 pysnmp 采集）

**目标**：把「采一台设备/一张接口表」做成**永不抛异常、失败也结构化**的函数，供
`agent.py` 的工具层调用。

核心数据结构（三件套）：

```python
class ErrorKind(str, Enum): ...  # 七类：timeout/unreachable/bad_oid/too_big/device_error/no_data/protocol
    @property
    def hint(self) -> str: ...   # 每类给「怎么排查」的一句话（输出是动作，不是名词）

@dataclass(frozen=True)
class Failure: stage: str; kind: ErrorKind; detail: str

@dataclass
class CollectionResult:
    host; port; values: dict; failures: list[Failure]; uptime_ticks
    @property
    def verdict(self) -> str: ...   # OK / DEGRADED / FAILED 三态
    @property
    def exit_code(self) -> int: ... # 0 / 2 / 1
```

核心函数（**签名记牢，agent 工具就直接调它们**）：

```python
def classify(indication, status, value) -> ErrorKind | None:
    """pysnmp 不抛异常，把结果放四元组 (indication,status,index,var_binds)。
    按优先级判：indication→超时/协议/不可达；status(int!=0)→tooBig/noSuchName/其它；
    value 是 NoSuchObject/Instance→no_data。三都空→None。"""
    ...

def get_raw(host, oid, *, community, port, timeout) -> tuple[str|None, Failure|None]:
    """一次 GET。永不抛：成功 (值,None)，失败 (None,Failure)。每次新建 SnmpEngine。"""
    ...

def walk(host, base_oid, *, ..., max_rows=200) -> list[tuple[str, str]]:
    """完整 WALK。lexicographicMode=False 走出子树就停。每次新建 SnmpEngine（复用会卡死）。"""
    ...

def collect_interfaces(host, *, ...) -> list[dict]:
    """三列（descr/oper/in_octets）按**行号字典**合并，绝不 zip（少回一行会整体错位）。"""
    ...

def collect_device(host, *, ...) -> CollectionResult:
    """系统组(sysDescr/uptime/sysName) + 接口数；uptime ticks→fmt_ticks 转可读。"""
    ...
```

两个必须踩过的坑（写进注释，别再猜）：
1. **SNMPv2c 没有「认证失败」**：community 错 = 设备静默丢包 = 客户端只看到超时，
   与网络不通不可区分 → 分类里**刻意没有 AUTH**。
2. **`_value_text`**：成功值多是 `DisplayString`（`OctetString` 子类），要用
   `asOctets().decode()` 取文本，否则中文接口名 `prettyPrint` 会变十六进制。

---

## 四、步骤 3：`agent.py` 定义 `TOOLS`（给模型看的工具清单）

**目标**：用 JSON Schema 告诉模型「有哪些工具、各要什么参数」。这是 Function Calling
的契约。

```python
TOOLS = [
  {"type":"function","function":{"name":"list_devices",
     "description":"列出本次要巡检的设备名与地址。第一步通常调用它。",
     "parameters":{"type":"object","properties":{},"required":[]}}},
  {"type":"function","function":{"name":"get_device_health",
     "description":"采单台系统组，返回 OK/DEGRADED/FAILED 与失败明细。",
     "parameters":{"type":"object",
        "properties":{"device":{"type":"string"}},"required":["device"]}}},
  {"type":"function","function":{"name":"get_interface_errors",
     "description":"采接口表（名/状态/入字节），发现 down 口。只对在线设备调用。",
     "parameters":{"type":"object",
        "properties":{"device":{"type":"string"}},"required":["device"]}}},
]
```

> **description 就是 prompt**：写清「什么时候该调它」，模型才用得对。比如明确写
> 「只对在线设备调用」，避免模型对 FAILED 设备白调一次。

---

## 五、步骤 4：工具实现 + `execute_tool`（把异常关进 `{"error"}`）

```python
def tool_get_device_health(devices, device) -> dict:
    spec = _find(devices, device)
    if spec is None: return {"error": f"未知设备 {device}"}
    res = collect_device(spec["host"], community=..., port=..., timeout=...)
    return {"device": device, "verdict": res.verdict, "values": res.values,
            "failures":[{"stage":f.stage,"kind":f.kind.value,"detail":f.detail,"hint":f.kind.hint}
                        for f in res.failures]}

def execute_tool(name, args, devices) -> dict:
    """统一入口：任何工具抛错 → {"error":...} 喂回模型，而不是让程序崩。"""
    fn = TOOL_IMPL.get(name)
    if fn is None: return {"error": f"工具不存在：{name}"}
    try: return fn(devices, **args)
    except TypeError as exc: return {"error": f"参数不对：{exc}"}
    except Exception as exc: return {"error": f"{type(exc).__name__}: {exc}"}
```

**为什么这么写**：Agent 能「自我修正」的前提是——**模型看得到错误**。工具报错被
转成 observation 回填后，模型下一轮才知道「哦，参数错了/设备不在线」，否则它瞎猜。

---

## 六、步骤 5：`RulePolicy`（无 Key 时的备胎大脑）

```python
class RulePolicy:
    """没有 Key 时替代模型：同一套「看 observation 决定下一步」的逻辑，只是换成规则。"""
    def __init__(self, devices): self.devices=[d["name"] for d in devices]; ...; self.listed=False
    def next_call(self):
        if not self.listed: return "list_devices", {}
        for d in self.devices:
            if d not in self.health: return "get_device_health", {"device": d}
        for d in self.devices:
            if self.health.get(d,{}).get("verdict") in ("OK","DEGRADED") and d not in self.ifaces:
                return "get_interface_errors", {"device": d}
        return None            # 信息够了，收工
    def observe(self, name, args, result): ...   # 把结果记进 self.health / self.ifaces
```

> 它是**常驻大脑**，不是玩具：真实项目里高频确定的路径用规则（便宜、可复现），
> 模糊路径才用模型。这正是「规则是地板、LLM 是天花板」。

---

## 七、步骤 6：解读层（规则判断 → LLM 说人话 → 规则兜底）

**分工是关键**：**「判断」由规则做，「表达」才交给 LLM**——这样模型不会凭空编诊断。

```python
def _build_issues(collected) -> list[dict]:
    """纯规则：哪台异常、级别(P0/P1/P2)、处置 hint。规则和 LLM 共用这份结构化 issue。"""
    # timeout/unreachable/protocol → P0；其余采集失败 → P1；接口 down → P1；状态未知 → P2
    ...

def interpret(collected, *, use_llm, llm, task) -> str:
    issues = _build_issues(collected)
    if not use_llm:                      # 规则渲染
        return "\n".join(按 P0>P1>P2 排序的文本)
    return _llm_render(task, issues, "", llm)   # LLM 渲染

def _llm_render(task, issues, fallback, llm) -> str:
    sys_prompt = ("你是网络运维告警解读专家……每条 issue 自带的 advice 要说进"
                  "「可能原因」；按 P0/P1/P2 分级；不编造未采集数据；不省略任何 issue。")
    try:
        return llm.chat([{"role":"system","content":sys_prompt},
                         {"role":"user","content":json.dumps({"task":task,"issues":issues},ensure_ascii=False)}],
                        temperature=0.3, max_tokens=800)
    except Exception as exc:                 # LLM 挂了就退回规则文本
        ...; return fallback or 规则渲染
```

> 注意 `_llm_render` 的 **try/except 兜底**：模型超时/报错时不能让整次巡检失败，
> 退回规则文本，运维照样看得懂。

---

## 八、步骤 7：`run_agent` 主循环（ReAct，一份骨架两种大脑）

```python
def run_agent(task, devices, *, max_steps=12, verbose=True):
    llm = LLMClient(); use_llm = llm.config.ready          # ← 唯一分叉点
    policy = RulePolicy(devices); collected = {}
    messages = [{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":task}]

    for step in range(1, max_steps+1):
        if use_llm:
            out = llm.chat_tools(messages, TOOLS, temperature=0.2)
            calls = out["tool_calls"]
            if not calls: break                            # 模型决定收工
            # 把 assistant 的 tool_calls 追加进 messages，再逐个执行、回填 tool 消息
            for c in calls:
                result = execute_tool(c["name"], c["arguments"], devices)
                _record_observation(...)                   # 收集健康/接口，供解读层
                messages.append({"role":"tool","tool_call_id":c["id"],
                                 "name":c["name"],"content":json.dumps(result,ensure_ascii=False)})
            continue
        else:
            nxt = policy.next_call()
            if nxt is None: break
            name, args = nxt
            result = execute_tool(name, args, devices)
            policy.observe(name, args, result); _record_observation(...)

    diag = interpret(collected, use_llm=use_llm, llm=llm, task=task)   # ← 统一进解读层
    print("诊断（解读层输出）："); print(diag)
    return {"mode":"llm" if use_llm else "rule", "trace":..., "final":diag, "summary":...}
```

> **`SYSTEM_PROMPT` 也是 prompt 工程**：写清「先 list_devices → 逐台查健康 → 只对
> OK/DEGRADED 查接口 → 查完收工 → 不编造」——这就是让模型别乱来、别浪费调用的关键。

---

## 九、步骤 8：`fake_agent.py`（真 UDP+BER 模拟设备，当采集目标）

为什么自己写而不是 mock：**要真跑协议**。`fake_agent` 是一个真的 UDP 服务，收 SNMP
请求、回 BER 编码的应答，支持 `sysDescr/uptime/sysName/ifTable`（4 个接口，含中文名
「上联口-核心」且为 down），以及 `--gen-err / --no-such-name` 等故障开关。
这样「无需真交换机」也能端到端验证超时/协议错/无此 OID/接口 down。

---

## 十、步骤 9：`snmpcheck.py` 加 `agent` 子命令（把上面串起来）

```python
def cmd_agent(args):
    from .agent import run_agent
    # 1) 没传 --config → 本地拉起 N 个 fake_agent（subprocess `-m snmpcheck.fake_agent`）
    #    sleep(0.6) 等 UDP 监听就绪，避免首包丢失
    # 2) 传了 --config → 读真实设备清单 JSON，不起模拟
    # 3) try: run_agent(...)  finally: kill 掉所有 fake_agent（别留僵尸进程）
```

在 `build_parser()` 里注册：

```python
p = sub.add_parser("agent", help="LLM Agent：真采集 + 决策 + 自然语言诊断")
p.add_argument("--ports", type=int, nargs="+", default=[1161])
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--community", default="public")
p.add_argument("--timeout", type=float, default=1.0)
p.add_argument("--max-steps", type=int, default=12)
p.add_argument("--task", default="巡检所有设备，找出异常并给出处理建议。")
p.add_argument("--config", help="真实设备清单 JSON，传了就不起模拟")
p.add_argument("-q", "--quiet", action="store_true", help="只输出最终诊断")
p.set_defaults(func=cmd_agent)
```

> 注意：`-q/--quiet` **只压汇总行，诊断照常打印**——别让「安静模式」把结论也吞了。

---

## 十一、步骤 10：`check_agent.py`（验收 A1–A5）

| 项 | 查什么 |
|---|---|
| A1 | `agent` 子命令退出码 0 |
| A2 | 输出含「模式：」（说明进了主循环，真采集后端就绪） |
| A3 | 至少发生一次采集动作（`ACTION` / `OBSERVE` / 「采集完成」） |
| A4 | 输出了非空最终诊断（含「诊断（解读层输出）：」且正文非空） |
| A5 | [加练] 多台（`--ports 1161 1162`）也能完整跑出诊断 |

**关键**：`check_agent` 把 `LLM_API_KEY` **清空**跑，保证「规则链路」可稳定复现——
所以 CI 永远走规则模式，不代表没 LLM 能力。**有 Key 时验收只验链路，不验模型质量。**

---

## 十二、两条路径怎么切（有 Key / 无 Key）

| | 决策 | 解读 |
|---|---|---|
| **有 Key** | 模型 Function Calling 自主定序 | 模型写自然语言告警 |
| **无 Key** | `RulePolicy` 规则定序 | 规则拼文本 |

**怎么用真实模型**（`llm_client` 读的是**当前工作目录**的 `.env`）：

```powershell
cd week1\snmpcheck
copy .env.example .env          # 填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
python -m snmpcheck.snmpcheck agent        # 看到「模式：LLM 驱动」即走模型
```

---

## 十三、自检 + 门禁（跑通才算数）

```powershell
cd week1

# 1) 端到端
python -m snmpcheck.snmpcheck agent
python -m snmpcheck.snmpcheck agent --ports 1161 1162

# 2) 验收
python snmpcheck/check_agent.py            # A1–A5，5/5

# 3) 质量门禁
ruff check snmpcheck
mypy snmpcheck                              # 9 文件零报错
```

实测（v0.2）：ruff 全绿、mypy 零报错、`check_agent` 5/5；`snmpcheck` 包覆盖率
**74.2%**（`llm_client` 真实分支与 `agent` 的 LLM 决策分支受 Key 门控，CI 不覆盖——
诚实留白）。

---

## 十四、常见坑（都已经踩过）

| 现象 | 原因 | 处理 |
|---|---|---|
| `Object of type DisplayString is not JSON serializable` | 工具直接返回 pysnmp 值对象 | `get_raw` 里 `_value_text()` 转成 str 再返回 |
| 第二次 WALK 永久卡死 | 复用了同一个 `SnmpEngine` | 每次 WALK 新建 engine |
| 接口表三列错位 | 用 `zip` 合并 | 按行号字典合并（`_if_index`） |
| 中文接口名变十六进制 | 用了 `prettyPrint` | `asOctets().decode('utf-8','replace')` |
| `-q` 后什么都不输出 | 把诊断也包进 `if verbose` | 诊断始终打印，只压汇总行 |
| `RulePolicy` 死循环 | `list_devices` 被反复调 | 加 `self.listed` 标志 |
| Windows 退出时报 `Event loop is closed` | Proactor 事件循环 | `WindowsSelectorEventLoopPolicy` |
| `chat_tools` 报 RuntimeError | 没 Key | 正常，说明该走规则；配 `.env` 才走模型 |

---

## 十五、面试要点（这套设计的「为什么」）

1. **决策与解读分开**：模型决定「采什么」，也负责「怎么说」；但**判断由规则做**，
   避免模型编诊断。
2. **规则是地板、LLM 是天花板**：无 Key 全规则可复现，有 Key 才上模型——成本/稳定
   两头都占。
3. **工具层把异常关进 `{"error"}`**：Agent 自我修正的前提是「模型看得见错误」。
4. **非确定性怎么测**：验收清空 Key 跑规则链路；LLM 分支只验结构不验质量。
5. **真协议而不是 mock**：`fake_agent` 真 UDP+BER，错误分类（七类）才有意义。

---

## 十六、和 m1w2_prompt 的衔接

v0.2 解决的是「**能调模型 + 会用工具**」；接下来的 `week1/m1w2_prompt/`（84 天主线
M1·W2）解决「**怎么调得准**」：Prompt 四要素 / few-shot / 结构化输出 / 抗幻觉 /
长上下文，最后落在「设备配置解析器」——本质就是 `_build_issues` + `_llm_render`
这套「规则判断、模型表达」思路的延伸。
