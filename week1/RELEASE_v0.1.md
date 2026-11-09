# Release v0.1 — SNMP 网络巡检 → 告警闭环 Agent 原型

- **Tag**：`v0.1`
- **日期**：2026-10-19（21 天训练收官 + 工程化收尾）
- **范围**：`week1/` 全部内容（Python 工程基础 / 真实协议采集 / 运维 Agent 闭环）

---

## 一、这个版本是什么

从零手写的一个「SNMP 网络巡检 → 告警闭环」运维 Agent 原型。用 `pysnmp` 真采集
交换机/设备状态，自动发现故障、收敛风暴、分级升级、生成可一键确认的签名链接、
配值班看板。全程不依赖任何告警 / SNMP 商业框架，标准库 + pysnmp 搞定。

自带的 `fake_agent` UDP+BER 假设备模拟器让**无需真实交换机**即可端到端验证协议层
错误（超时 / 协议错 / 无此 OID），这也是每日自动化验收能稳定全绿的基础。

## 二、三阶段能力（对应简历「项目经历」）

| 阶段 | 天数 | 关键能力 |
|---|---|---|
| Python 工程基础 | D1–D7 | 数据外置 / logging 分级 / argparse / 拆模块 / pytest / 重试计时 / 合成 CLI `snmpcheck` |
| 真实协议采集 | D8–D14 | pysnmp 真采集（GET/WALK）/ 结构化 / 七类错误分层 / 配置外置 / asyncio 并发 |
| 运维 Agent 闭环 | D15–D21 | 增量巡检 / 趋势降噪 / 告警状态机 / 收敛升级 / HMAC 签名闭环 / 通知聚合 / 值班排班 |

## 三、工程化收尾（本版本相对「原始 21 天训练」补齐的门禁）

- ✅ **ruff**：lint + format 全绿（`pyproject.toml` 内配置，line-length=100）
- ✅ **mypy**：`snmpcheck`(6 文件) / `d17`(11 文件) / `d18`(16 文件) 零报错
  （务实配置：pysnmp 无类型 stub → `disable_error_code=["import-untyped"]`；
  非包脚本同名模块 → 逐目录单独检查，配置 `exclude` 避免误伤）
- ✅ **pytest**：每日验收脚本 8/8 ~ 9/9，后期每日带回归全绿
- ✅ **覆盖率**：核心交付目录（snmpcheck + d17 + d18）实测 **78.7%**
  （pytest-cov + 子进程追踪；全仓库本地模块 55.2%）
- ✅ **打包**：`pip install -e .` 后任意目录可调 `snmpcheck`
  （改为正规包 `snmpcheck`，含 `pyproject.toml` + `entry_points` + `__init__.py`）
- ✅ **发版**：本 tag `v0.1` + 本 release note

## 四、怎么跑

```bash
cd week1
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -e .                                     # 装 snmpcheck 入口
pip install pytest pytest-cov mypy ruff pysnmp        # 质量门禁依赖

# 1) 采集器 CLI（用内置假设备，无需真交换机）
snmpcheck list
snmpcheck status

# 2) 每日验收（含故障注入 / 变异测试）
python snmpcheck/check_d7.py      # D7：7/7
python d17/check_d17.py           # D17 回归
python d18/check_d18.py           # D18 回归（含告警闭环端到端）

# 3) 质量门禁
ruff check .
mypy snmpcheck d17 d18
pytest snmpcheck/check_d7.py d17/check_d17.py d18/check_d18.py
```

> 告警闭环系统（D15–D21）位于 `d17/`、`d18/`，含 `fake_agent.py` 模拟器、
> `ack_server.py` 标准库 HTTP 确认服务、`board.py` 生命周期看板。
> 日课设计决策记录：`week1/*/README*.md`（共 23 份）。

## 五、已知限制（面试被问「边界」时如实说）

1. 仅实现 **SNMPv2c**（community 字符串）；SNMPv3 / v1 未做。
2. SNMPv2c **没有「认证失败」**：community 错 = 设备静默丢包 = 客户端只看到超时，
   与网络不通不可区分——错误分类里**刻意不含 AUTH 类别**。
3. `pysnmp` 每次 `getCmd` 有约 0.7s 固定开销（新建 asyncio dispatcher），
   并发扫描已用 `Semaphore` 限流，但单台开销无法消除。
4. 覆盖率统计**排除** `check_*.py`（验收脚本）、`fake_agent.py`、`__init__.py`、
   `out/`（运行产物）；这些是测试/模拟/生成物，不算被测产品代码。
5. 代码量统计：剔除验收脚本与模拟器后，核心产品代码约 1.26 万行 / 85 文件；
   连同 33 个验收脚本 + 23 份日课文档，全仓库约 2.8 万行。

## 六、仓库与远端

- 项目根：`week1/`
- 主入口：`snmpcheck`（采集器 CLI，打包后任意目录可调）
- 双远端：`origin` = GitHub（`buzhao46/agent-learning`）、
  `gitee` = Gitee（`huangyb1010/agent-learning`）备用同步
- tag：`v0.1`（含上述工程化收尾；后续 84 天 Agent 主线在另一条分支/目录演进）
