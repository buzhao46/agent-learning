# M1·W2 · Prompt Engineering 系统训练（里程碑：设备配置解析器）

> **这个文件夹是什么**：84 天主线的**第 1 月 · 第 2 周（D8–D14）**，主攻 **Prompt
> Engineering**，最后一天产出一个**设备配置解析器**——把交换机/OTN 设备的配置文本
> （`display current-configuration` 那种）解析成结构化 JSON。正好落在你的
> OTN / SNMP 领域舒适区，同时把上周的 LLM 能力往前推一大步。

> ⚠️ **天号别撞车（重要）**：本模块的 **D8–D14 是「84 天主线」的 Prompt 训练周**，
> 跟 `week1/d8` ~ `week1/d14`（**21 天训练**里的 pysnmp 真采集，你已做完）**完全是
> 两回事**。天数相同纯属两套编号巧合。所以本文件夹叫 `m1w2_prompt`，**不叫 `d8`**，
> 别再放进 `d18`——`d18` 是告警系统（D15–D21）的地盘。

---

## 一、为什么是现在（承接上周 v0.2）

上周你已经把 LLM 决策层接进 `snmpcheck`（tag `v0.2`）：**能调模型、能 Function
Calling、能规则兜底**。但——

> **「能调」≠「调得准」。** 同一个模型，prompt 写得好不好，输出质量差一个档次。

本周把 prompt 当**一门工程**来练，五件事全都直接服务于「让模型稳定产出**可被程序
消费**的结果」：

| 天 | 能力 | 一句话 |
|---|---|---|
| D8 | 四要素 | 角色 / 任务 / 约束 / 输出格式，把「随口一问」变成「可复用指令」 |
| D9 | Few-shot | 用示例教会模型你的格式，比堆形容词有效 |
| D10 | 结构化输出 | 要 JSON 就给 JSON，还要能校验、能重试 |
| D11 | 抗幻觉 | 只依据给定材料回答，没有就说没有，不编 |
| D12 | 长上下文 | 太长的配置/手册，怎么切、怎么压、怎么只喂该喂的 |
| D13 | **项目** | 把上面五件事合成一个**设备配置解析器** |
| D14 | 复盘 | 把本周的 prompt 沉淀成**模板库** |

---

## 二、目录约定（本文件夹）

```
week1/m1w2_prompt/
├── README.md                ← 这份
├── llm_client.py            ← 已从 snmpcheck 拷来（双模式：有 Key 走真实，无 Key 走 mock）
├── .env.example             ← 复制成 .env，填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（可选）
├── sample_configs/
│   └── huawei_s5735.cfg     ← D13 解析器的输入样例（真实 Huawei 风格配置）
├── d08_four_elements.py     ← 每天你写一个（下面逐日给骨架 + 自检）
├── d09_fewshot.py
├── d10_structured.py
├── d11_grounding.py
├── d12_longctx.py
├── d13_config_parser.py     ← 里程碑项目
├── prompts/                 ← D14 把本周 prompt 整理成模板库（每个 .md 一个模板）
└── check_w2.py              ← 验收脚本（按第十二节「验收设计」；我下一步可生成，或你自己写）
```

> **约定沿用前几周**：每天一个脚本，能单独 `python dNN_xxx.py` 跑；验收脚本
> `check_w2.py` 由我来写、你**不要改**；你只管写 `d08`~`d13` 的代码。

---

## 三、前置（5 分钟，先确认能调通）

1. **进入 venv**（沿用前几周）：

   ```powershell
   cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1
   .\.venv\Scripts\activate
   ```

2. **（强烈建议）配一个真实 Key**。本周练的是 prompt，**没有真实模型反馈等于闭眼
   练**——mock 只会回固定话术，你没法比较 prompt 好坏。去 DeepSeek / 通义 / 任意
   OpenAI 兼容端点拿一个 key：

   ```powershell
   cd m1w2_prompt
   copy .env.example .env
   # 编辑 .env，填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
   ```

   > 没 Key 也能跑（mock 模式），但 D8/D9/D11 的「对比效果」会失真；D10/D13 的
   > **结构化/规则部分**仍然可练、可验收。

3. **自检：跑通一次调用**（这行就是你 D4 干过的事，确认环境没坏）：

   ```powershell
   python llm_client.py
   ```

   预期：打印 `配置：真实模式 | ...` 或 `配置：mock 模式（...）`，然后一句回复。
   看到这两行，环境就 OK。

---

## 四、D8 · Prompt 四要素（角色 / 任务 / 约束 / 输出格式）

**今天一句话**：把「用一句话解释 X」升级成「带角色、带任务、带约束、带输出格式」的
完整指令，并**写 3 版对比**，亲眼看见差异。

**写 `d08_four_elements.py`**：

```python
from llm_client import LLMClient

llm = LLMClient()

# 同一个问题的 3 版 prompt：从"裸问"到"四要素齐全"
PROMPTS = {
    "v1_裸问": "解释一下 SNMP 的 community 是什么",
    "v2_加角色": "你是一名网络运维工程师。解释一下 SNMP 的 community 是什么",
    "v3_四要素": (
        "你是一名网络运维工程师（角色）。\n"
        "任务：向刚入职的同事解释 SNMPv2c 的 community 是什么。\n"
        "约束：不超过 120 字；用一个比喻；不要展开 SNMPv3。\n"
        "输出格式：先一句话定义，再一行『注意：』开头讲安全风险。"
    ),
}

for name, p in PROMPTS.items():
    print(f"\n===== {name} =====")
    print(llm.chat([{"role": "user", "content": p}], temperature=0.3, max_tokens=300))
```

**自检**：`python d08_four_elements.py` → 打出三段回复，肉眼对比 `v1` 和 `v3` 的
信息密度/格式差异。

**验收标准（本周之后都适用）**：至少能说出 **v3 比 v1 好在哪**（长度可控？格式可
预期？风险点被覆盖？），并把 `v3` 存进 `prompts/`（D14 用）。

> **要点**：四要素里**最容易被忽略的是「约束」**——不给字数/范围/禁止项，模型就会
> 自由发挥。你后面写的所有 prompt 都先过一遍这四条。

---

## 五、D9 · Few-shot 与示例选择

**今天一句话**：与其用形容词教模型（「要简洁、要专业」），不如**给 2~3 个示例**让它
照着学——尤其当你要的格式很特定时。

**写 `d09_fewshot.py`**：做一个「设备告警文案分类器」，把告警分到
`P1/P2/P3` 三档，用 few-shot 教它输出格式。

```python
from llm_client import LLMClient
llm = LLMClient()

# 关键：示例 = 输入/输出成对，且覆盖易混淆的边界
SHOTS = [
    ("核心交换机整机不可达", "P1"),
    ("某接入口错包率 2%", "P3"),
    ("上联口 down", "P1"),
    ("CPU 使用率 85% 持续 5 分钟", "P2"),
]

def classify(text: str, n_shots: int = 4) -> str:
    demo = "\n".join(f"告警：{t}\n级别：{l}" for t, l in SHOTS[:n_shots])
    prompt = (
        "把告警按严重程度分为 P1/P2/P3。参考下面的例子，只输出级别本身。\n"
        f"{demo}\n告警：{text}\n级别："
    )
    return llm.chat([{"role": "user", "content": prompt}], temperature=0).strip()

# 实验：示例数量对效果的影响
for n in (0, 2, 4):
    print(n, "个示例 →", classify("光模块收光功率低于门限", n_shots=n))
```

**自检**：跑一下，观察 `0 个示例`（纯指令）vs `4 个示例` 的输出是否更像你要的
`P1/P2/P3` 单值。

**验收标准**：能讲清 **示例数量 / 顺序 / 是否覆盖边界情况** 三者对结果的影响（哪怕
只在 1~2 个样本上观察）。

> **要点**：few-shot 不是越多越好。**示例要「代表你真正在意的那类输入」**，把最容
> 易判错的边界样本放进去，比放 10 个典型样本更有效。

---

## 六、D10 · 结构化输出（JSON + schema 校验 + 失败重试）

**今天一句话**：要程序消费模型输出，就得让它**稳定吐 JSON**，并且**坏 JSON 要能
被发现、能重试**——这一步是 D13 的地基。

**写 `d10_structured.py`**，三个函数：

```python
import json
from llm_client import LLMClient
llm = LLMClient()

def ask_json(text: str) -> str:
    """让模型只输出 JSON，不要解释。"""
    prompt = (
        "你是配置解析器。只输出 JSON，不要任何解释或 markdown 代码块。\n"
        "把这句话拆成 {\"host\": str, \"port\": int}：\n" + text
    )
    return llm.chat([{"role": "user", "content": prompt}], temperature=0)

def validate(obj: dict) -> list[str]:
    """返回问题列表；空列表 = 合格。这是你要重点写的——它决定"能不能信"。"""
    errs = []
    if not isinstance(obj, dict):
        return ["顶层不是对象"]
    if not isinstance(obj.get("host"), str):
        errs.append("host 缺失或不是字符串")
    if not isinstance(obj.get("port"), int):
        errs.append("port 缺失或不是整数")
    return errs

def parse_with_retry(text: str, max_try: int = 3) -> dict | None:
    """问模型 → 剥壳 → json.loads → validate；失败就带错误信息重问。"""
    # 提示：用 _strip_fence() 去掉 ```json ... ``` 外壳；重试时把 errs 拼回 prompt
    ...
```

**你必须自己补的**：剥 ```` ```json ```` 外壳的小函数、`parse_with_retry` 的重试
循环。

**自检**：

```powershell
python -c "import d10_structured as d; print(d.parse_with_retry('连接 10.0.0.1 端口 161'))"
```

**验收标准**：对 3 个不同输入都返回**通过 `validate` 的 dict**；故意喂一个会让模型
跑偏的输入时，能看到**重试**发生（打印重试次数）。

> **要点**：LLM 的 JSON 不可信是常态。**「校验 + 重试」比「祈祷它格式对」专业得多。**
> 这也是你 v0.2 里 `_safe_json` 的升级版——从「兜住不崩」到「判不合格并重来」。

---

## 七、D11 · 控制幻觉（grounding / 引用 / 该拒答就拒答）

**今天一句话**：模型不知道的会**编**。今天学两招——**只依据给定材料回答（grounding）**
和**没有依据就明说不知道（拒答）**。

**写 `d11_grounding.py`**：给一段设备手册片段，问只有材料里能回答的问题，另附一个
材料里**没有**的问题，看它会不会编。

```python
from llm_client import LLMClient
llm = LLMClient()

MANUAL = """S5735-L 支持最大 48 个千兆电口 + 4 个万兆光口。
POE 型号整机供电预算 740W。工作温度 0~45℃。"""

def answer(question: str) -> str:
    prompt = (
        "只依据下面的资料回答。资料里没有的信息，直接回答『资料未提及』，不要推测。\n"
        f"【资料】\n{MANUAL}\n"
        f"【问题】{question}\n"
        "【回答】"
    )
    return llm.chat([{"role": "user", "content": prompt}], temperature=0).strip()

print(answer("POE 型号的供电预算是多少？"))   # 资料有 → 740W
print(answer("这台设备支持几个 40G 口？"))     # 资料没有 → 应拒答
```

**自检**：第二个问题如果模型答了个数字（而不是「资料未提及」），说明 grounding 没
管住——加强约束（「只允许引用资料原文」「不确定就说不知道」），再试。

**验收标准**：有依据的问题答案正确；**无依据的问题明确拒答**，不编造。

> **要点**：抗幻觉不是「求模型诚实」，是**用 prompt 结构把它的自由度关掉**——
> 限定资料范围 + 显式允许拒答。这直接决定 D13 解析器「会不会瞎填字段」。

---

## 八、D12 · 长上下文管理（切 / 压 / 只喂该喂的）

**今天一句话**：配置/手册动辄几千行，全塞进去又贵又容易「中间遗忘」。今天练**分段
摘要**和**按需检索前置**两种压缩手段。

**写 `d12_longctx.py`**：把 `sample_configs/huawei_s5735.cfg` 按段落切，逐段摘要，
再合成一段总览。

```python
from llm_client import LLMClient
llm = LLMClient()

def split_by_section(text: str) -> list[str]:
    """按 '#' 分隔符切段（Huawei 配置天然用 # 分段）。"""
    return [s.strip() for s in text.split("#") if s.strip()]

def summarize_section(sec: str) -> str:
    return llm.chat([
        {"role": "user", "content": "用不超过 15 字概括这段设备配置在配什么：" + sec}
    ], temperature=0).strip()

def overview(path: str) -> list[str]:
    text = open(path, encoding="utf-8").read()
    return [summarize_section(s) for s in split_by_section(text)]
```

**自检**：`python -c "import d12_longctx as d; [print(x) for x in d.overview('sample_configs/huawei_s5735.cfg')]"`

**验收标准**：能对每个段落给出简短概括；能说清**为什么不能把整份配置一次性丢给
模型**（成本 / 中间遗忘 / 噪声）。

> **要点**：长上下文的两板斧——**分段摘要**（先压缩再汇总）和**检索前置**（只把
> 相关段落喂进去）。W3 的 RAG 就是把「检索前置」做成系统。

---

## 九、D13 · 项目：设备配置解析器（里程碑）

**今天一句话**：把 D8~D12 合成一个真东西——**输入一份设备配置文本，输出结构化 JSON**。
而且要用 **规则 + LLM 双路**：规则做确定性兜底，LLM 处理规则搞不定的变体。

### 9.1 输入 → 输出（这就是你和 `check_w2.py` 的契约）

输入：`sample_configs/huawei_s5735.cfg`（已给你）。

输出 JSON（**金标准**，字段名/类型照抄）：

```json
{
  "hostname": "S5735-Core-01",
  "vlans": [10, 20, 30, 100, 200],
  "interfaces": [
    {"name": "Vlanif10",  "kind": "vlanif",   "ip": "10.10.10.1/24",
     "description": null, "link_type": null, "access_vlan": null,
     "allow_vlans": [], "shutdown": false},
    {"name": "Vlanif100", "kind": "vlanif",   "ip": "192.168.100.1/24",
     "description": null, "link_type": null, "access_vlan": null,
     "allow_vlans": [], "shutdown": false},
    {"name": "GigabitEthernet0/0/1", "kind": "physical", "ip": null,
     "description": "Uplink-To-Core-Switch", "link_type": "trunk",
     "access_vlan": null, "allow_vlans": [10, 20, 30], "shutdown": false},
    {"name": "GigabitEthernet0/0/2", "kind": "physical", "ip": null,
     "description": "Access-OA-PC", "link_type": "access",
     "access_vlan": 100, "allow_vlans": [], "shutdown": false},
    {"name": "GigabitEthernet0/0/3", "kind": "physical", "ip": null,
     "description": null, "link_type": "access",
     "access_vlan": 200, "allow_vlans": [], "shutdown": false},
    {"name": "GigabitEthernet0/0/4", "kind": "physical", "ip": null,
     "description": "unused", "link_type": null, "access_vlan": null,
     "allow_vlans": [], "shutdown": true}
  ],
  "snmp": {"version": "v2c", "community_configured": true}
}
```

> 注意几个容易错的地方：`ip` 要转成 `x.x.x.x/掩码位数`（`255.255.255.0` → `/24`）；
> trunk 的 `allow_vlans` 是**列表**、access 的 `access_vlan` 是**单值**；没有的字段
> 一律给 `null`/`[]`/`false`，**不要省键**（省键下游会 KeyError）。

### 9.2 写 `d13_config_parser.py`（四个函数）

```python
import json
import re
from llm_client import LLMClient

llm = LLMClient()

def mask_to_prefix(mask: str) -> int:
    """255.255.255.0 -> 24（自己写：把每段转二进制数 1 的个数）"""
    ...

def parse_with_rules(text: str) -> dict:
    """确定性解析：按行扫描 # 分段，正则抓 sysname/vlan/interface。
    这是离线基线，也是 check 断言的对象——必须稳定。"""
    ...

def parse_with_llm(text: str) -> dict:
    """把整份配置丢给模型，要求输出上面 schema 的 JSON；
    复用 D10 的 validate + 重试。规则搞不定的变体靠它。"""
    ...

def parse(text: str, use_llm: bool = False) -> dict:
    """统一入口：默认走规则（离线可跑）；use_llm=True 走模型。"""
    return parse_with_llm(text) if use_llm else parse_with_rules(text)

if __name__ == "__main__":
    text = open("sample_configs/huawei_s5735.cfg", encoding="utf-8").read()
    out = parse(text, use_llm=False)
    print(json.dumps(out, ensure_ascii=False, indent=2))
```

### 9.3 分四步做（别一次写完）

1. **先规则、后模型**：先把 `parse_with_rules` 写到**金标准完全一致**（这是纯字符串
   处理，不依赖模型，能立刻验证）。**规则不通，别急着上 LLM。**
2. **再加 `validate`**（可复用 D10 的）：断言金标准输出能过校验。
3. **再加 `parse_with_llm`**：有 Key 时跑一遍，和规则输出**逐字段对比**，把二者不
   一致的地方记下来（这恰恰是「规则 vs 模型各擅长什么」的最好素材）。
4. **`parse()` 入口**默认规则、`--llm` 开关切模型（命令行加 `--llm` 即可，30 秒的事）。

### 9.4 自检

```powershell
python d13_config_parser.py                        # 默认规则路径，应打印金标准 JSON
# 快速抽查（应输出：S5735-Core-01 [10, 20, 30, 100, 200] 6）
python -c "import d13_config_parser as d; t=open('sample_configs/huawei_s5735.cfg',encoding='utf-8').read(); o=d.parse_with_rules(t); print(o['hostname'], o['vlans'], len(o['interfaces']))"
```

**验收标准**：`parse_with_rules` 的输出**等于金标准**（字段级全等）；`parse()` 默认
离线可跑；有 Key 时 `--llm` 也能返回通过 `validate` 的 JSON。

> **为什么规则是必须的（重点）**：模型会漂、会编、会漏字段。**规则是「确定性地板」，
> LLM 是「柔性天花板」。** 这正是你 v0.2 的 `RulePolicy` 兜底思想在解析任务上的复用。
> 面试讲这条，比说「我调了个大模型」高一个层次。

---

## 十、D14 · 周复盘：把 prompt 沉淀成模板库

**今天一句话**：本周写的 prompt 别散在脚本里——抽出来放进 `prompts/`，每个模板一个
`.md`，含「用途 / 四要素 / 示例输入输出 / 已知坑」。

**做三件事**：

1. 建 `prompts/`，把 D8 四要素版、D9 分类器版、D10 JSON 版、D11 grounding 版、
   D13 解析器版各存一个 `.md`。
2. 写一段 200 字**周记**（放 `prompts/WEEK2.md`）：本周最大的 1 个收获 + 1 个还
   没搞懂的点。
3. `git commit`（沿用你每天收尾习惯）。

**验收标准**：`prompts/` 下 ≥4 个模板 + 1 篇周记；每个模板都能被直接复制复用。

---

## 十一、验收设计（`check_w2.py` 该查什么）

> 本周的难点是：**LLM 输出非确定，怎么测？** 答案和 v0.2 一样——**把确定性部分
> 做成硬断言（规则/校验/重试），把模型质量做成可选冒烟**。下面是我建议的 6 项：

| 项 | 查什么 | 是否离线可跑 | 不通过最常见原因 |
|---|---|---|---|
| C1 | `validate()` 能抓出「缺字段 / 类型错」的坏对象，且放过好对象 | ✅ 纯函数 | 校验只判了 key 存在、没判类型 |
| C2 | `parse_with_rules(sample)` **完全等于**金标准 JSON | ✅ 确定性 | 掩码没转 / trunk 与 access 弄反 / 漏 `shutdown` |
| C3 | `parse(text, use_llm=False)` 返回 dict 且过 `validate` | ✅ | 入口接错、默认走了模型 |
| C4 | 用**注入的假 LLM**（先返回坏 JSON、再返回好 JSON）跑 `parse_with_retry`，确认**重试生效**且第 2 次拿到合法 dict | ✅ 不依赖真 Key | 没有重试 / 重试时不把错误带回 prompt |
| C5 | 每个 `d08`~`d12` 脚本 `import` 不报错、有 `__main__` 守卫（冒烟） | ✅ | 粗心写坏语法 |
| C6（可选） | **有真实 Key 时**：`parse_with_llm(sample)` 返回过 `validate` 的 dict（只查结构，不查逐字段相等） | 🟡 需 Key | schema 没约束住 |

**C4 是本周的技术亮点**——它演示了「非确定性 LLM 功能如何被确定性测试框住」：把模型
替换成一个**可脚本化的假模型**，就能断言「重试逻辑」这个真正属于你代码的部分。
（这正是记忆里「Agent 非确定性怎么测」那个缺口的第一块拼图。）

> `check_w2.py` 我可以按上表直接生成（含拉起假 LLM、跑 C1–C5，无 Key 也全绿；C6 有
> Key 才跑）。你说一声就写。

---

## 十二、卡住了怎么办

| 现象 | 原因 | 怎么改 |
|---|---|---|
| `ModuleNotFoundError: llm_client` | 没在 `m1w2_prompt` 目录下跑 | `cd m1w2_prompt` 再跑 |
| 回复是 `[mock] 未配置...` | 没填 `.env` / Key 没生效 | 检查 `.env`，或 `$env:LLM_API_KEY` 临时设 |
| 模型输出带 ```` ```json ```` 外壳 | JSON 前后有 markdown | 写 `_strip_fence()` 剥掉再 `json.loads` |
| `validate` 总报 port 不是 int | 模型给了 `"161"` 字符串 | 校验里要么 `int()` 收编、要么判 `isinstance(str)` 报错 |
| 掩码转前缀算错 | 没按二进制数 1 | `mask_to_prefix('255.255.255.0')` 应得 24，先单测这个函数 |
| trunk 的 vlan 解析成一个数 | 用了 `split()[0]` 只取第一个 | `allow-pass vlan` 后面是**多个** vlan，用正则抓全部 |
| 覆盖率/类型检查报 pysnmp 无 stub | 本模块不碰 pysnmp | 与本周无关，忽略 |

---

## 十三、面试要点（本周能被追问的点）

1. **四要素**：角色/任务/约束/输出格式——尤其「约束」才是控住输出的关键。
2. **few-shot 的取舍**：不是越多越好，**边界样本**比典型样本值钱。
3. **结构化输出 = 生成 + 校验 + 重试**三件套，不能只生成。
4. **抗幻觉靠结构不靠自觉**：限定资料范围 + 显式允许「资料未提及」。
5. **规则是地板、LLM 是天花板**：确定性场景用规则兜底，柔性变体才上模型——
   一句话讲清你 v0.2 的 `RulePolicy` 和 D13 解析器的同一套哲学。
6. **非确定性怎么测**：把模型换成脚本化假模型，断言你的「重试/校验/降级」逻辑。

---

## 十四、之后是什么（W3 RAG → 设备手册问答）

本周的 **D12 长上下文** 和 **D13 解析器** 会直接长成 W3 的 **RAG 设备手册问答**：
- D12 的「检索前置」→ W3 的 embedding + 向量库召回；
- D13 的「结构化抽取」→ RAG 里「答案带出处」的 grounding；
- 你的 `sample_configs/` 和将来的设备手册，就是 RAG 的语料。

一句话：**本周把「怎么问」练扎实，下周开始练「怎么给模型喂对材料」。**

---

## 十五、今天就动手（最短路径）

```powershell
cd F:\workbuddy_workspace\2026-10-19-10-49-35\week1\m1w2_prompt
..\.venv\Scripts\activate
copy .env.example .env      # 填 Key（可选但强烈建议）
python llm_client.py        # 看到「配置：...」+ 一句回复 = 环境 OK
# 然后写 d08_four_elements.py，跑它，开始 D8
```
