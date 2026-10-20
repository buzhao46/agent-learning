# Week 1 · 逐日操作清单（Day 1–7）

> 配套代码都在本目录，全部已实测跑通。**没有 API Key 也能跑**（自动 mock），有 Key 就切真实模型。
> 每天按「准备 → 动手 → 练习 → 验收」四步走，不要只看不写。

> **节奏校正（重要）**：Java 只是「皮毛」这件事意味着 —— **Day 1 和 Day 3 不是复习，是新课**，请按满时长（1.5–2h）走，别压缩。
> Day 29–30（SNMP 原理 / GET）仍然可以提速，那是你真做过项目的领域。
> 判断标准很简单：能看懂 `class`、`interface`、`Map` 这类概念就够了，**不需要为了转 Agent 去补 Java**。

> **「不常写脚本」的解法**：不要先学完再写，而是**每天 30–50 行、连续 21 天**。
> 配套文档：同目录上一级的 `python_21days_drill.html`（21 个微任务，全部围绕本目录代码展开）。
> 每天开工前先跑一遍 `python snippets.py` —— 里面 10 个高频片段（日志/JSON/表格/CSV/重试/计时）就是你当天的素材，不用从空白文件开始。

---

## 0. 一次性准备（约 10 分钟，只做一次）

```bash
cd week1

# 建虚拟环境（等价 Java 的 per-project JDK + Maven 隔离）
python -m venv .venv

# Windows 激活
.venv\Scripts\activate
# macOS / Linux 激活
# source .venv/bin/activate

# 装依赖
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

激活成功的标志：命令行前面出现 `(.venv)`。

`.gitignore` 里必须有这两行（**Key 千万别提交**）：

```
.venv/
.env
out/
```

---

## Day 1 · Python 基础速通（1.5h）

**目标**：把 Java 概念映射到 Python；能写出带类型注解、有异常处理、会读写文件的小脚本。
**产出**：`out/day1_devices.json`

| 时间 | 做什么 |
|---|---|
| 0:00–0:15 | 建 venv、装依赖（上面第 0 节） |
| 0:15–0:50 | 读 `day1_types.py`，对照注释里的「Java → Python」映射表 |
| 0:50–1:20 | 跑通 + 做 3 个练习 |
| 1:20–1:30 | 写 3 行笔记，commit |

**命令**

```bash
python day1_types.py
python day1_types.py --device core-sw-01
python day1_types.py --device not-exist   # 应该打印友好错误、退出码 2
```

**类型注解加练（推荐，30 分钟）**

`day1_typing_drill.py` 里埋了 3 处故意写错的类型，目标是把下面这条命令跑到 `Success`：

```bash
pip install mypy ruff
mypy day1_typing_drill.py      # 实测会报 4 条，修完即通关
ruff check .                   # 顺手统一代码风格
```

> 学这些概念的**资源该看哪几节**，见同目录上一级的 `week1_day1_resources.html`（含官方文档精读位置、mypy 速查表、避坑清单）。
>
> **想直接动手改代码**：进 `d1/`，那里有一个只留 `load_devices()` 给你写的半成品（数据外置），配 `python check_d1.py` 自动验收。见 `d1/README.md`。

**练习（必须动键盘）**

1. 给 `Device` 加 `uptime: int = 0` 字段，输出表增加一列「运行时长」。
2. 写 `load_devices(path: str) -> list[Device]`，从 `out/day1_devices.json` 读回来（提示：`json.loads` + `Device(**d)`）。
3. 在 `main()` 里加计时：`t = time.perf_counter()`，结束打印耗时。

**验收**：三条命令都不报错；`--device not-exist` 退出码为 2；能说出 `field(default_factory=list)` 为什么必须这么写。

**坑**

- `tags: list[str] = []` 会让所有实例**共享同一个 list**，必须用 `field(default_factory=list)`。
- Windows 上 `python` 可能指向 Microsoft Store 的假 Python，用 `py -3` 或 `python3`。
- 没激活 venv 就 `pip install`，包装到全局，后面会找不到。

**参考**
- 类型注解：https://docs.python.org/zh-cn/3/library/typing.html
- dataclasses：https://docs.python.org/zh-cn/3/library/dataclasses.html
- venv：https://docs.python.org/zh-cn/3/library/venv.html

---

## Day 2 · asyncio 异步编程（1.5h）

**目标**：理解 `async/await`、`gather` 并发、超时与限流。
**产出**：一份「串行 vs 并发」耗时对比（脚本会自己打印）。

| 时间 | 做什么 |
|---|---|
| 0:00–0:30 | 读 `day2_async.py` 前 3 个函数，理解 `await` 在哪让出 CPU |
| 0:30–1:00 | 跑脚本，看 2.5x 提速；再跑 `--real` 看真实 HTTP |
| 1:00–1:20 | 做练习 |
| 1:20–1:30 | 笔记 + commit |

**命令**

```bash
python day2_async.py
python day2_async.py --real     # 需要联网
```

**实测输出**（你的机器上应该接近）

```
串行：1.52s
并发：0.61s
提速约 2.5x
超时控制：✓ wait_for 在 0.5s 触发超时
并发限流：4 个任务、并发上限 2 → 0.63s
```

**练习**

1. 把 `DEVICES` 加到 10 台，观察并发耗时是否仍然 ≈ 0.6s（说明并发和时间无关，只和最慢的那个有关）。
2. 给 `fetch_health` 加 retry：失败重试 2 次，退避 `await asyncio.sleep(0.1 * 2 ** i)`。
3. 把 `with_semaphore` 的上限从 2 改成 4，看耗时变成多少。

**验收**：能解释「为什么 `time.sleep` 会卡死整个 Agent，而 `asyncio.sleep` 不会」。

**坑**

- `async def` 函数**调用后不执行**，必须 `await` 或丢进 `gather`，否则静默无输出。
- `gather` 不加 `return_exceptions=True`，一台设备失败会拖垮整轮巡检。
- 在协程里用 `requests`（同步库）会阻塞事件循环，异步要用 `httpx`。

**参考**
- asyncio 协程与任务：https://docs.python.org/zh-cn/3/library/asyncio-task.html
- httpx 异步：https://www.python-httpx.org/async/

---

## Day 3 · FastAPI 入门（1.5h）

**目标**：路由、Pydantic 模型、依赖注入——把 Java Spring 那套映射过来。
**产出**：`/health` + `/echo` 可用，Swagger 能打开。

| 时间 | 做什么 |
|---|---|
| 0:00–0:30 | 读 `day3_fastapi.py`，对照注释里的注解映射 |
| 0:30–1:00 | 启动服务，打开 `/docs` 点着玩 |
| 1:00–1:20 | 做练习 |
| 1:20–1:30 | 笔记 + commit |

**命令**

```bash
python day3_fastapi.py        # 浏览器打开 http://127.0.0.1:8000/docs
```

另开一个终端验收：

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/echo -H "Content-Type: application/json" -d "{\"message\":\"hello\",\"upper\":true}"
curl http://127.0.0.1:8000/devices/core-sw-01
curl -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/devices/nope     # 404
curl -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/echo \
     -H "Content-Type: application/json" -d "{\"message\":\"\"}"              # 422
```

**练习**

1. 加 `POST /devices`：接收 `DeviceOut`，写进 `DEVICES` 字典，返回 201。
2. 给 `/devices` 加查询参数 `status: str | None = None`，支持 `?status=down` 过滤。
3. 把 `Settings.api_token` 设成 `"abc"`，重跑，体会 401；再带 `-H "X-Token: abc"` 通过。

**验收**：`/docs` 能看到自动生成的接口文档；404 与 422 的差别你能讲清楚（资源不存在 vs 参数不合法）。

**坑**

- 端口被占用：`netstat -ano | findstr :8000` 找到 PID，`taskkill /PID xxx /F`。
- Pydantic v2 用 `model_dump()`，老的 `.dict()` 已废弃。
- `uvicorn.run(app, reload=True)` 写在脚本里在 Windows 上容易反复重启，开发期用命令行 `uvicorn day3_fastapi:app --reload`。

**参考**
- FastAPI 中文教程：https://fastapi.tiangolo.com/zh/tutorial/
- Pydantic 模型：https://docs.pydantic.dev/latest/concepts/models/

---

## Day 4 · 调用大模型 API（1.5h）

**目标**：搞懂 `messages` 结构、参数、流式输出。
**产出**：命令行成功调通 DeepSeek / Qwen 任一模型。

**先解决 Key（三选一，都有免费或极低门槛）**

| 服务 | 地址 | 特点 |
|---|---|---|
| DeepSeek | https://platform.deepseek.com | 便宜、国内直连，充 10 元够学一个月 |
| 硅基流动 | https://cloud.siliconflow.cn | 有免费额度，模型多 |
| 阿里百炼 | https://bailian.console.aliyun.com | 有免费 token，企业常用 |

拿到 Key 后：

```bash
cp .env.example .env     # Windows: copy .env.example .env
# 编辑 .env，填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
```

| 时间 | 做什么 |
|---|---|
| 0:00–0:20 | 申请 Key、配 `.env`、跑 `python llm_client.py` 自检 |
| 0:20–0:50 | `day4_llm_call.py --once` 跑通单轮 |
| 0:50–1:10 | 跑多轮模式，体会「模型没有记忆」；再试 `--stream` |
| 1:10–1:30 | 练习 + 笔记 + commit |

**命令**

```bash
python llm_client.py                                  # 自检：看是 mock 还是真实模式
python day4_llm_call.py --once "解释一下 SNMP 的 OID" --show-payload
python day4_llm_call.py --stream                      # 多轮 + 流式
```

**练习**

1. 把 `SYSTEM_PROMPT` 改成「只输出 JSON，不要任何解释」，连问 3 次，看格式是否稳定。
2. 在多轮里先问「我叫巨人，记住」，再问「我叫什么」——如果它答错，说明你没把 assistant 回复塞回 history。
3. 打印 `resp.usage`，看看一次调用花了多少 token。

**验收**：能画出一次请求的完整 JSON（system + user），并解释 `temperature` 与 `max_tokens` 各自控制什么。

**坑**

- `base_url` 必须带 `/v1`（DeepSeek 是 `https://api.deepseek.com/v1`），否则 404。
- `max_tokens` 单位是 **token** 不是汉字，1 汉字 ≈ 1~1.5 token。
- Key 写进代码再 push 到 GitHub，会被扫号盗刷，务必走 `.env` + `.gitignore`。

**参考**
- DeepSeek API 文档：https://api-docs.deepseek.com/zh-cn/
- 硅基流动 API：https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions
- openai-python：https://pypi.org/project/openai/

---

## Day 5 · 模型参数调优（1.5h）

**目标**：用实验数据回答「Agent 决策该用什么 temperature」。
**产出**：`out/day5_param_lab.md`（含你自己的结论）

| 时间 | 做什么 |
|---|---|
| 0:00–0:15 | 读 `day5_param_lab.py`，看实验矩阵设计 |
| 0:15–0:50 | 跑全矩阵（真实模式下 12 次调用，成本约几分钱） |
| 0:50–1:15 | 填报告里「我的结论」四项 |
| 1:15–1:30 | 笔记 + commit |

**命令**

```bash
python day5_param_lab.py            # 2 个 system × 3 个温度 × 2 个 max_tokens = 12 次
python day5_param_lab.py --quick    # 只跑温度对比
```

**练习**

1. 加 `top_p` 维度（0.5 / 1.0），观察它和 temperature 是否叠加。
2. `temperature=0` 连跑两次，比较两次输出是否逐字相同。
3. 把 `max_tokens` 设成 20，看 `finish_reason` 是不是 `length`（被截断）。

**验收**：报告里那四行结论是你自己写的，且能说出「为什么 Agent 的工具决策要用低温度」。

**坑**

- 别同时大改 temperature 和 top_p，否则分不清是谁的影响。
- 输出被截断时 JSON 会解析失败——这是 Agent 报错的高频原因，`max_tokens` 要留余量。

**参考**
- Prompt 工程指南（中文）：https://www.promptingguide.ai/zh

---

## Day 6 · 项目：LLM 版 Agent（3.5h）

**目标**：把 `minimal_agent.py` 的规则决策换成**真实模型的 Function Calling**。
**产出**：可运行的 LLM 版 Agent + 一份完整 trace。

**先看结构（这是重点，不是代码量）**

`day6_agent_llm.py` 只有四块：
1. `TOOLS` —— 给模型看的 JSON Schema（你的「能力清单」）
2. `TOOL_IMPL` —— 真正干活的函数（将来换成 pysnmp）
3. `execute_tool` —— 统一入口，**任何异常都转成 `{"error": ...}` 喂回模型**
4. `run_agent` —— while 循环：问模型 → 执行工具 → 把结果塞回 messages → 再问

| 时间 | 做什么 |
|---|---|
| 0:00–0:40 | 读代码，手画一遍 messages 的增长过程 |
| 0:40–1:10 | 无 Key 跑规则模式，把 7 步轨迹看懂 |
| 1:10–2:10 | 配 Key 跑 LLM 模式，对比「模型自己选工具」和规则版的差别 |
| 2:10–3:10 | 做改造练习 1、2 |
| 3:10–3:30 | 写 trace、笔记、commit |

**命令**

```bash
python day6_agent_llm.py
python day6_agent_llm.py --task "只诊断 core-sw-01 这台设备"
python day6_agent_llm.py --max-steps 3      # 验证护栏：到 3 步强制停
```

**实测（规则模式）输出**

```
[STEP 7] 信息已足够，输出诊断
- [P0] agg-sw-03：设备离线 / SNMP 不可达 → 先确认电源、链路、community
- [P1] core-sw-01：CPU 92% 偏高 → 查是否有环路/广播风暴/异常流量
- [P1] core-sw-01：接口 GE0/0/2 入方向错误包 1543 → 疑光衰/误码
- [P1] core-rtr-02：接口 Gi0/2 处于 down → 查光模块与对端
完成 | 模式=rule 步数=7
```

**改造练习（这才是简历素材）**

1. **加一个高危工具** `reboot_port(device, interface)`，并加人工确认：执行前 `input("确认重启？(y/N)")`，非 y 就返回 `{"error": "用户拒绝执行"}`。→ 这就是 human-in-the-loop。
2. **换真实 SNMP**：把 `tool_get_device_health` 的函数体换成 pysnmp 的 `getCmd`（参考同级目录 `snmp-agent-demo/snmp_client.py`），schema 一行不用改。
3. **并行化**：第 2 步「逐台查健康」现在是一条条串行，用 Day 2 的 `gather` 改成并发。

**验收**：能回答三个问题——模型怎么知道你有哪些工具？工具结果怎么回到模型手里？为什么要把工具异常转成 `{"error": ...}` 而不是抛出去？

**坑**

- 回填 `assistant` 消息时必须带 `tool_calls`，否则下一轮报 400。
- `arguments` 是**字符串**，要 `json.loads`；模型偶尔给非法 JSON，`execute_tool` 外面要兜。
- 每个 `role: tool` 消息必须有 `tool_call_id`，且和上一条 `tool_calls[].id` 一一对应。
- 没有最大步数护栏，模型会反复调同一个工具烧钱。

**参考**
- Function Calling 概念：https://api-docs.deepseek.com/zh-cn/guides/function_calling

---

## Day 7 · 周复盘（1.5h）

**目标**：把一周产出变成 GitHub 上的仓库 + 一篇周记。

**命令**

```bash
cd week1
git init
# 先建 .gitignore（见第 0 节），再 add
git add .
git commit -m "feat(week1): day1-6 python/async/fastapi/llm/param-lab/agent"
git branch -M main
git remote add origin https://github.com/<你的用户名>/ai-agent-roadmap.git
git push -u origin main
```

**周记模板**（复制到 `week1/NOTES.md`）

```markdown
## Week 1 复盘（YYYY-MM-DD）
- 本周最大的认知变化：
- 卡最久的一个问题 & 怎么解决的：
- 代码产出：day1~day6 六个脚本，全部可跑
- 数据结论：串行 1.52s vs 并发 0.61s；temperature=0 相对稳定
- 还没搞懂的：
- 下周第一件事：
```

**验收清单**

- [ ] GitHub 仓库能打开，README 写了怎么跑
- [ ] `.env` 没被提交（去仓库里点开确认一次）
- [ ] 六个脚本在你本机全部能跑
- [ ] 周记写完，且写了「还没搞懂的」

---

## 命令速查

| 场景 | 命令 |
|---|---|
| 激活环境 | `.venv\Scripts\activate` |
| 装包加速 | `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 看端口占用 | `netstat -ano \| findstr :8000` |
| 杀进程 | `taskkill /PID 12345 /F` |
| 装 pysnmp | `pip install pysnmp==6.1.4` |
| 检查 Key 是否生效 | `python llm_client.py` |

## 常见报错对照

| 报错 | 原因 | 解法 |
|---|---|---|
| `ModuleNotFoundError: openai` | 没装或没激活 venv | 激活后 `pip install -r requirements.txt` |
| `AuthenticationError 401` | Key 错 / 没读 `.env` | `python llm_client.py` 看是否 mock 模式 |
| `NotFoundError 404` | `base_url` 漏了 `/v1` | 补上 |
| `BadRequestError 400 tool_calls` | 没回填 assistant 的 tool_calls | 按 Day 6 坑列表检查 |
| `UnicodeEncodeError` | Windows 终端编码 | `chcp 65001` 或 `set PYTHONUTF8=1` |
| 输出被截断 | `max_tokens` 太小 | 调大，`finish_reason` 会显示 `length` |
