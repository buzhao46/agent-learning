# 21 天落地成果 · 简历可讲解总结

> 用途：本文件是「SNMP 网络巡检 → 告警闭环」个人项目的简历素材库。
> 直接复制第三节到简历「项目经历」，第四节到「技能/工具」，第五节用于面试口述。
> 代码量统计与质量门禁（ruff/mypy/覆盖率）以仓库实际状态为准，见第六节待补项。

---

## 一、这个项目到底是什么（一句话定位）

一个**从零手写**的「SNMP 网络巡检 → 告警闭环」运维 Agent 原型：用 pysnmp 真采集
交换机/设备状态，自动发现故障、收敛风暴、分级升级、生成可一键确认的签名链接、
配值班看板。全程**不依赖任何告警 / SNMP 商业框架**，标准库 + pysnmp 搞定。

真实数据：核心产品代码约 1.26 万行（85 文件）；连同 33 个自动化验收脚本和 23 份
日课文档，全仓库约 2.8 万行。每日验收 8/8 ~ 9/9 通过，且后期每日带回归测试全绿。

---

## 二、三阶段演进（讲 20 分钟的主线）

| 阶段 | 天数 | 做了什么 | 关键产出 |
|---|---|---|---|
| **Python 工程基础** | D1–D7 | 数据外置 / logging 分级 / argparse / 拆模块 / pytest / 重试计时 / 合成 CLI | `snmpcheck`（list·status 子命令） |
| **真实协议采集** | D8–D14 | pysnmp 真采集 / 结构化 / WALK 接口表 / 落盘 / 错误分类 / 配置外置 / asyncio 并发 | 真采集引擎 + 七类错误分类 + 并发扫描 |
| **运维 Agent 闭环** | D15–D21 | 增量巡检 / 趋势降噪 / 告警状态机 / 收敛升级确认 / 签名闭环看板 / 通知聚合 / 值班排班 | 完整告警生命周期系统 |

> 原计划最后一周是「mypy / ruff / 打包 / 覆盖率 / 发版」工程收尾，实际被替换为
> 做真实产品功能（告警系统）。工程收尾动作已补齐并打 tag v0.1（见仓库）。

---

## 三、简历可直接粘贴的项目描述（中文版，建议缩写到 4–5 行）

**网络运维巡检 Agent（个人项目，Python）**
- 基于 pysnmp 实现 SNMPv2c 真采集（GET/WALK），自写 UDP+BER 假设备模拟器，无需真实
  交换机即可端到端验证协议层错误（超时 / 协议错 / 无此 OID）。
- 设计七类 `ErrorKind` + `Failure` 三层异常分层模型，把「失败」变成结构化、可分类、
  可部分成功的产出，而非笼统抛异常。
- 用 asyncio + Semaphore 实现多设备并发巡检，单台固定开销与限流可控。
- 实现告警状态机（幂等通知、静默抑制）、同组批量故障收敛、指数退避升级、人工确认；
  HMAC-SHA256 签名确认链接 + 标准库 ThreadingHTTPServer 闭环，防伪造 / 防时序攻击。
- 生命周期看板（CLI/HTML/JSON 三形态）、增量巡检按 host+port 合并历史；每日交付带
  自动化验收脚本（含故障注入 / 变异测试），全程 pytest 绿 + 回归全绿；git 每日提交、
  GitHub + Gitee 双远端。

（英文要点：SNMPv2c collector, pysnmp, asyncio concurrency, alert state machine,
idempotent notification, correlation/escalation, HMAC-signed one-click ack links,
standard-library HTTP closure service, observability board, pytest + fault injection.）

---

## 四、技能矩阵（对应简历「技能 / 工具」栏）

- **语言 / 工程**：Python、pathlib、logging、argparse、dataclass、typing、pytest、pytest-cov
- **网络 / 协议**：SNMPv2c、pysnmp hlapi、OID 树、TimeTicks、GET/WALK、BER 编码、错误码分层
- **并发**：asyncio、gather、Semaphore 限流
- **系统设计**：状态机、幂等、收敛 / 升级策略、指数退避、增量合并
- **安全**：HMAC-SHA256 签名、命名空间隔离、compare_digest 防时序攻击、读写解耦
- **可观测性**：生命周期看板、UTF-8 编码治理、日志分级
- **代码质量**：ruff（lint + format）、mypy（类型检查）、pytest-cov（覆盖率）
- **协作 / 发布**：git 提交规范、双远端（GitHub + Gitee）、pyproject 打包、语义化 tag

---

## 五、面试讲解要点（讲清「为什么这么拆」）

1. **错误分类是重点**：SNMPv2c **没有「认证失败」**，community 错 = 静默丢包 =
   只能看到超时。讲清这一点，比背 API 更显内行。
2. **状态机只管自动跃迁，ack 是独立人为动作**：两条线解耦，`ack_server` 不碰 `step`、
   只开放「确认」不开放「取消」——这是安全设计意识。
3. **签名把「谁能消音」锁死**：device/group 独立命名空间，拿设备 sig 造不出组 sig；
   换密钥让所有旧链接失效。
4. **收敛不是抑制状态**：20 台同机房同时掉线，通知合成一条，但每台状态仍独立可见。
5. **增量巡检**：按 host+port 把增量结果合并回完整报告，避免每次全量重采。
6. **真起 HTTP 服务做端到端验证**（不 mock）：闭环链路用同一密钥签名 + 真实请求验证。

---

## 六、仓库工程状态（面试被问「代码质量」时的实话）

- ✅ ruff：lint + format 全绿
- ✅ mypy：类型检查零报错（务实配置，对未标注代码不强制）
- ✅ pytest：每日验收脚本 8/8 ~ 9/9，回归全绿
- ✅ 覆盖率：核心交付目录（snmpcheck + d17 + d18）实测 **78.7%**（pytest-cov + 子进程追踪；全仓库本地模块 55.2%）
- ✅ 打包：`pip install -e .` 后任意目录可调用 `snmpcheck`
- ✅ 发版：git tag `v0.1` + release note（做了什么 / 怎么跑 / 已知限制）
- ✅ 双远端：GitHub（origin）+ Gitee（gitee）同步

> 项目根：`week1/`；主入口：`snmpcheck`（采集器 CLI）；告警系统位于 `d17/` `d18/`
> 日课文档：`week1/*/README*.md`（共 23 份，记录每天的设计决策与踩坑）。
