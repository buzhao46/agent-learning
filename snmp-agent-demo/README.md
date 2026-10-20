# 网络运维 Agent Demo（Vue 控制台 + 真实 SNMP）

一个**可运行、可解释**的最小 Agent 产品：Vue 控制台发任务，Python 后端跑 ReAct 循环（思考 → 调用工具 → 观察 → 再思考），工具层支持**真实 SNMP** 采集或开箱即用的模拟数据。

## 目录结构

```
snmp-agent-demo/
├── app.py                # HTTP 服务（stdlib，无 Web 框架依赖）
├── agent.py              # ReAct Agent 核心 + 规则决策器 RuleLLM
├── snmp_client.py        # 真实 SNMP（pysnmp）+ 模拟数据回退
├── requirements.txt
├── static/
│   ├── index.html        # Vue 3 控制台
│   └── vue.global.prod.js# 本地化 Vue（无需外网）
└── README.md
```

## 快速开始

```bash
# 1. 安装依赖（仅真实 SNMP 需要）
pip install -r requirements.txt

# 2. 启动
python app.py            # 默认 8765 端口
python app.py 9000       # 或指定端口

# 3. 浏览器打开
#    http://127.0.0.1:8765
```

> 没装 pysnmp 也能跑：控制台会显示「pysnmp 未安装」，此时请用「模拟设备」模式；「真实 SNMP」会返回明确的错误提示。

## 两种模式

| 模式 | 说明 |
|---|---|
| **模拟设备** | 内置 3 台仿真设备（1 台离线 / 1 台高 CPU / 1 台接口错误），开箱即用，用来演示 Agent 的诊断能力 |
| **真实 SNMP** | 对真实设备发 SNMPv1/v2c GET/WALK，采集 sysDescr、接口状态与错误计数 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 运行环境、pysnmp 可用性、预置设备 |
| GET | `/api/snmp/test?ip=&community=&version=` | 测试真实设备连通性 |
| POST | `/api/run` | 执行 Agent，返回完整 trace |

`POST /api/run` 请求体：

```json
{
  "task": "巡检所有设备，找出异常并给出处理建议。",
  "mode": "real",
  "devices": [
    {"name": "sw-01", "ip": "192.168.1.1", "community": "public", "version": "2c"}
  ]
}
```

## 采样的标准 OID（MIB-II）

| 字段 | OID |
|---|---|
| sysDescr | 1.3.6.1.2.1.1.1.0 |
| sysUpTime | 1.3.6.1.2.1.1.3.0 |
| sysName | 1.3.6.1.2.1.1.5.0 |
| ifDescr | 1.3.6.1.2.1.2.2.1.2 |
| ifOperStatus | 1.3.6.1.2.1.2.2.1.8 |
| ifInErrors | 1.3.6.1.2.1.2.2.1.14 |
| ifOutErrors | 1.3.6.1.2.1.2.2.1.20 |

## 如何升级成「真 Agent」

1. **换真模型**：`agent.py` 里的 `RuleLLM.decide()` 改成调用 LLM 的 Function Calling（把 `TOOL_SPECS` 作为 tools），由模型自己决定工具与参数。
2. **扩工具**：在 `snmp_client.py` 增加 `snmp_set`、批量采集、配置下发等；高风险动作加 human-in-the-loop。
3. **加护栏**：超时、最大步数、成本预算、权限校验。
4. **加可观测**：接入 Langfuse / LangSmith 记录每条 trace。

## 注意

- 仅监听 `127.0.0.1`，不要把 demo 直接暴露到公网。
- 真实 SNMP 请只在**你有权限管理**的设备上使用。
