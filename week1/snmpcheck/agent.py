"""snmpcheck `agent` 子命令的大脑：把 LLM 决策层接在真采集（netprobe）之上。

两层职责，对应你的诉求「决策 + 解读全做」：

  1. 决策层（ReAct）：由模型（或规则决策器 RulePolicy 降级）自己决定巡检定序——
     先列设备 → 逐台查系统组健康 → 逐台查接口表 → 何时收工。
     模型通过 Function Calling 选工具，每步的观察结果回填后继续决策。
  2. 解读层（interpret）：把采集到的异常整理成「结构化 issue（含失败类别的处置 hint）」，
     再交给 LLM 生成自然语言告警文案 + 分级处置建议；无 API Key 时由规则渲染兜底。

这样「模型决定采什么」和「自然语言告警」都真实用到了 LLM，snmpcheck 才名副其实叫 Agent。
无 Key 时两层都走规则，仍能完整跑通并打印结构化诊断（RulePolicy 是高频确定路径的常驻大脑）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from .llm_client import LLMClient
from .log_setup import get_log
from .netprobe import CollectionResult, collect_device, collect_interfaces

# 一台待巡检设备：{name, host, port, community}
DeviceSpec = dict[str, Any]


# ============================================================ 1. 工具定义（给模型看的）
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_devices",
            "description": "列出本次要巡检的所有网络设备名称与地址。巡检的第一步通常要调用它。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_health",
            "description": "采集单台设备的系统组：sysDescr / sysUpTime / sysName 与接口数量。"
            "返回 OK/DEGRADED/FAILED 判定与失败明细。设备离线时会带失败类别。",
            "parameters": {
                "type": "object",
                "properties": {"device": {"type": "string", "description": "设备名，如 core-sw-01"}},
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_interface_errors",
            "description": "采集指定设备的整张接口表：接口名 / 运行状态(operStatus) / 累计入字节。"
            "只对在线设备调用（离线设备查了也会失败）。用于发现 down 口与高错误包。",
            "parameters": {
                "type": "object",
                "properties": {"device": {"type": "string", "description": "设备名，如 core-sw-01"}},
                "required": ["device"],
            },
        },
    },
]


# ============================================================ 2. 工具实现（真采集后端）
def tool_list_devices(devices: list[DeviceSpec]) -> dict[str, Any]:
    return {
        "devices": [
            {"name": d["name"], "host": d["host"], "port": d.get("port", 161)} for d in devices
        ]
    }


def _find(devices: list[DeviceSpec], name: str) -> DeviceSpec | None:
    return next((d for d in devices if d["name"] == name), None)


def tool_get_device_health(devices: list[DeviceSpec], device: str) -> dict[str, Any]:
    spec = _find(devices, device)
    if spec is None:
        return {"error": f"未知设备 {device}"}
    res: CollectionResult = collect_device(
        spec["host"],
        community=spec.get("community", "public"),
        port=spec.get("port", 161),
        timeout=spec.get("timeout", 1.0),
    )
    return {
        "device": device,
        "verdict": res.verdict,
        "values": res.values,
        "failures": [
            {"stage": f.stage, "kind": f.kind.value, "detail": f.detail, "hint": f.kind.hint}
            for f in res.failures
        ],
    }


def tool_get_interface_errors(devices: list[DeviceSpec], device: str) -> dict[str, Any]:
    spec = _find(devices, device)
    if spec is None:
        return {"error": f"未知设备 {device}"}
    try:
        ifaces = collect_interfaces(
            spec["host"],
            community=spec.get("community", "public"),
            port=spec.get("port", 161),
            timeout=spec.get("timeout", 1.0),
        )
    except Exception as exc:  # noqa: BLE001 - 工具层兜住一切，保证循环不断
        return {"device": device, "error": f"{type(exc).__name__}: {exc}"}
    return {"device": device, "interfaces": ifaces}


TOOL_IMPL: dict[str, Callable[..., dict[str, Any]]] = {
    "list_devices": lambda devices, **a: tool_list_devices(devices),
    "get_device_health": lambda devices, **a: tool_get_device_health(devices, **a),
    "get_interface_errors": lambda devices, **a: tool_get_interface_errors(devices, **a),
}


def execute_tool(name: str, args: dict[str, Any], devices: list[DeviceSpec]) -> dict[str, Any]:
    """统一入口：任何工具抛错都转成 {"error": ...} 喂回模型，而不是让程序崩。

    这是 Agent 能「自我修正」的基础：模型看到错误信息才知道下一步该怎么办。
    """
    fn = TOOL_IMPL.get(name)
    if fn is None:
        return {"error": f"工具不存在：{name}，可用工具：{list(TOOL_IMPL)}"}
    try:
        return fn(devices, **args)
    except TypeError as exc:
        return {"error": f"参数不对：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


# ============================================================ 3. 规则决策器（无 Key 时的备胎）
class RulePolicy:
    """没有 API Key 时替代模型：走同一套「看观察结果决定下一步」的逻辑。

    它不是玩具——真实项目里，高频确定的路径用规则、模糊路径才用模型，成本更低更稳。
    """

    def __init__(self, devices: list[DeviceSpec]) -> None:
        self.devices = [d["name"] for d in devices]
        self.health: dict[str, dict[str, Any]] = {}
        self.ifaces: dict[str, list[dict[str, Any]]] = {}
        self.listed: bool = False

    def next_call(self) -> tuple[str, dict[str, Any]] | None:
        if not self.listed:
            return "list_devices", {}
        for d in self.devices:
            if d not in self.health:
                return "get_device_health", {"device": d}
        for d in self.devices:
            h = self.health.get(d, {})
            if h.get("verdict") in ("OK", "DEGRADED") and d not in self.ifaces:
                return "get_interface_errors", {"device": d}
        return None  # 信息够了，收工

    def observe(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        if name == "list_devices":
            self.listed = True
            return
        dev = result.get("device") or args.get("device", "?")
        if name == "get_device_health":
            self.health[dev] = result
        elif name == "get_interface_errors":
            self.ifaces[dev] = result.get("interfaces", [])


# ============================================================ 4. 解读层（结构化 issue → 自然语言）
def _build_issues(collected: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """把采集结果整理成结构化 issue 列表（规则和 LLM 共用）。

    规则负责「判断」（哪台异常、什么级别、给什么处置建议），LLM 只负责「自然语言化」。
    这样 LLM 不会凭空编造诊断，也不会漏掉失败类别自带的处置 hint。
    """
    issues: list[dict[str, Any]] = []
    for name, data in collected.items():
        h = data.get("health", {})
        verdict = h.get("verdict")
        if verdict in ("FAILED", "DEGRADED"):
            for f in h.get("failures", []):
                severity = "P0" if f["kind"] in ("timeout", "unreachable", "protocol") else "P1"
                issues.append(
                    {
                        "device": name,
                        "level": severity,
                        "title": f"{f['stage']} 采集失败（{f['kind']}）",
                        "detail": f["detail"],
                        "advice": f["hint"],
                    }
                )
        for itf in data.get("interfaces", []):
            status = itf.get("status")
            if status == "down":
                issues.append(
                    {
                        "device": name,
                        "level": "P1",
                        "title": f"接口 {itf.get('name')} 处于 down",
                        "detail": f"index={itf.get('index')}",
                        "advice": "检查光模块与对端配置，确认链路是否拔除。",
                    }
                )
            elif status == "未知":
                issues.append(
                    {
                        "device": name,
                        "level": "P2",
                        "title": f"接口 {itf.get('name')} 状态未知",
                        "detail": f"index={itf.get('index')}",
                        "advice": "设备未回 ifOperStatus，可能接口刚创建或处于 notPresent，先观察。",
                    }
                )
    return issues


def interpret(
    collected: dict[str, dict[str, Any]],
    *,
    use_llm: bool,
    llm: LLMClient,
    task: str,
) -> str:
    """解读层：结构化 issue → 自然语言告警文案 + 分级处置建议。

    use_llm 时把 issue 交给模型润色（含处置 hint）；否则规则渲染。
    """
    issues = _build_issues(collected)
    if not issues:
        base = "巡检完成：所有被采设备的系统组与接口均正常，无异常。"
        return base if not use_llm else _llm_render(task, issues, base, llm)

    if not use_llm:
        lines = ["巡检完成，诊断如下（按优先级）："]
        order = {"P0": 0, "P1": 1, "P2": 2}
        for it in sorted(issues, key=lambda x: order.get(x["level"], 9)):
            lines.append(f"- [{it['level']}] {it['device']}：{it['title']}（{it['detail']}）")
            lines.append(f"      → {it['advice']}")
        lines.append("")
        lines.append("处理优先级：P0 离线/不可达 > P1 端口 down/采集降级 > P2 状态未知。")
        return "\n".join(lines)

    return _llm_render(task, issues, "", llm)


def _llm_render(task: str, issues: list[dict[str, Any]], fallback: str, llm: LLMClient) -> str:
    sys_prompt = (
        "你是网络运维告警解读专家。下面是一份巡检采集到的结构化异常（JSON）。"
        "请输出一份面向运维人员的自然语言告警文案，要求：\n"
        "1) 每台异常设备说清「现象」与「可能原因」（引用每条 issue 自带的 advice）；\n"
        "2) 按 P0/P1/P2 给出分级处置建议；\n"
        "3) 不要编造未采集到的数据，也不要省略任何一条 issue。\n"
        "只输出告警文案本身。"
    )
    user_msg = json.dumps(
        {"task": task, "issues": issues}, ensure_ascii=False, indent=2
    )
    try:
        return llm.chat(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=800,
        )
    except Exception as exc:  # noqa: BLE001 - LLM 不可用就退回规则文本
        get_log().warning("LLM 解读失败，退回规则渲染：%s", exc)
        if fallback:
            return fallback
        lines = ["巡检完成，诊断如下（按优先级）："]
        order = {"P0": 0, "P1": 1, "P2": 2}
        for it in sorted(issues, key=lambda x: order.get(x["level"], 9)):
            lines.append(f"- [{it['level']}] {it['device']}：{it['title']}（{it['detail']}）")
            lines.append(f"      → {it['advice']}")
        return "\n".join(lines)


# ============================================================ 5. 主循环
SYSTEM_PROMPT = (
    "你是网络运维 Agent。任务：用工具采集数据后给出诊断。规则：\n"
    "1) 先调用 list_devices 拿到设备清单；2) 逐台调用 get_device_health 查系统组健康；\n"
    "3) 只对 verdict 为 OK/DEGRADED 的设备调用 get_interface_errors 查接口表"
    "（FAILED 设备查了也会失败，别浪费调用）；\n"
    "4) 所有设备都查完后，停止调用工具，直接输出一句「采集完成，请生成诊断」。\n"
    "不要编造未采集到的数据。"
)


def run_agent(
    task: str,
    devices: list[DeviceSpec],
    *,
    max_steps: int = 12,
    verbose: bool = True,
) -> dict[str, Any]:
    """跑一次完整 ReAct 循环 + 解读层，返回 {mode, trace, final, summary}。"""
    llm = LLMClient()
    use_llm = llm.config.ready
    trace: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    policy = RulePolicy(devices)
    collected: dict[str, dict[str, Any]] = {}

    if verbose:
        print(f"模式：{'LLM 驱动' if use_llm else '规则驱动（未配置 LLM_API_KEY）'}")
        print(f"后端：真 pysnmp 采集（目标 {len(devices)} 台设备）")
        print(f"任务：{task}\n" + "-" * 64)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    def _record_observation(name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        dev = result.get("device") or args.get("device")
        if dev is not None:
            collected.setdefault(dev, {})
            if name == "get_device_health":
                collected[dev]["health"] = result
            elif name == "get_interface_errors":
                collected[dev]["interfaces"] = result.get("interfaces", [])

    for step in range(1, max_steps + 1):
        if use_llm:
            out = llm.chat_tools(messages, TOOLS, temperature=0.2)
            calls = out["tool_calls"]
            if not calls:
                # 模型决定收工。决策到此为止，解读层统一生成自然语言诊断。
                trace.append({"step": step, "type": "final", "text": "模型决定收工，进入解读层"})
                break
            if verbose:
                print(f"\n[STEP {step}] 模型要求调用 {len(calls)} 个工具")
            messages.append(
                {
                    "role": "assistant",
                    "content": out["content"],
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["raw_arguments"]},
                        }
                        for c in calls
                    ],
                }
            )
            for c in calls:
                result = execute_tool(c["name"], c["arguments"], devices)
                _record_observation(c["name"], c["arguments"], result)
                if verbose:
                    print(f"   ACTION  {c['name']}({json.dumps(c['arguments'], ensure_ascii=False)})")
                    print(f"   OBSERVE {json.dumps(result, ensure_ascii=False)[:200]}")
                trace.append(
                    {
                        "step": step,
                        "type": "action",
                        "tool": c["name"],
                        "args": c["arguments"],
                        "result": result,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "name": c["name"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            continue

        # ---- 规则驱动：形状和 LLM 版完全一样（同一套骨架，可替换的大脑）
        nxt = policy.next_call()
        if nxt is None:
            trace.append({"step": step, "type": "final", "text": "信息已足够，进入解读层"})
            break
        name, args = nxt
        result = execute_tool(name, args, devices)
        policy.observe(name, args, result)
        _record_observation(name, args, result)
        trace.append({"step": step, "type": "action", "tool": name, "args": args, "result": result})
        if verbose:
            print(f"\n[STEP {step}]")
            print(f"   ACTION  {name}({json.dumps(args, ensure_ascii=False)})")
            print(f"   OBSERVE {json.dumps(result, ensure_ascii=False)[:200]}")

    # ---- 兜底：步数用尽
    if not any(t["type"] == "final" for t in trace):
        trace.append(
            {"step": max_steps, "type": "abort", "text": f"达到最大步数 {max_steps}，强制结束"}
        )

    diag = interpret(collected, use_llm=use_llm, llm=llm, task=task)
    summary = {
        "devices": len(devices),
        "tool_calls": sum(1 for t in trace if t["type"] == "action"),
        "steps": len(trace),
        "issues": len(_build_issues(collected)),
    }
    print("\n" + "=" * 64)
    print("诊断（解读层输出）：")
    print(diag)
    if verbose:
        print("-" * 64)
        print(
            f"完成 | 模式={('llm' if use_llm else 'rule')} 步数={summary['steps']} "
            f"工具调用={summary['tool_calls']} 异常={summary['issues']} "
            f"耗时={round(time.perf_counter() - t0, 2)}s"
        )
    return {
        "mode": "llm" if use_llm else "rule",
        "trace": trace,
        "final": diag,
        "summary": summary,
    }
