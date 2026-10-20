"""Day 6 · 项目：LLM 版 Agent（把 RuleLLM 换成真实模型决策）

和前几天比，这一步才真正叫 Agent：**由模型自己决定调哪个工具、传什么参数**。
你只提供两样东西：
  1) tools 的 JSON Schema（告诉模型「你有什么能力」）
  2) 一个 while 循环（执行模型要的工具，再把结果喂回去）

运行：
    python day6_agent_llm.py                       # 无 Key：规则决策器，仍能看完整 ReAct 轨迹
    python day6_agent_llm.py --task "只看 core-sw-01"  # 换任务
    python day6_agent_llm.py --max-steps 10

验收：trace 里能看到 tool_calls 由模型给出（真实模式下）；最终给出结构化诊断；
      且 MAX_STEPS 能兜住死循环。

真实设备接入：把 TOOL_IMPL 里的函数体换成 pysnmp 采集即可，schema 一行不用改。
"""
from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from llm_client import LLMClient

# ============================================================ 1. 工具定义（给模型看的）
# 这份 schema 就是「模型的说明书」，description 写得越清楚，模型选错工具的概率越低。
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_devices",
            "description": "列出纳管的所有网络设备名称与 IP。巡检的第一步通常要调用它。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_health",
            "description": "查询单台设备的存活状态与 CPU/内存利用率。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "设备名，如 core-sw-01"}
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_interface_errors",
            "description": "查询指定设备的接口错误包数量。设备离线时会失败，调用前应先确认在线。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "设备名，如 core-sw-01"}
                },
                "required": ["device"],
            },
        },
    },
]

# ============================================================ 2. 工具实现（真实逻辑）
FAKE_DB: dict[str, dict[str, Any]] = {
    "core-sw-01": {"ip": "10.0.0.1", "status": "up", "cpu": 92, "mem": 61},
    "core-rtr-02": {"ip": "10.0.0.2", "status": "up", "cpu": 41, "mem": 38},
    "agg-sw-03": {"ip": "10.0.0.3", "status": "down", "cpu": -1, "mem": -1},
}
FAKE_IF: dict[str, list[dict[str, Any]]] = {
    "core-sw-01": [
        {"if": "GE0/0/1", "status": "up", "inErrors": 0, "outErrors": 2},
        {"if": "GE0/0/2", "status": "up", "inErrors": 1543, "outErrors": 12},
    ],
    "core-rtr-02": [
        {"if": "Gi0/1", "status": "up", "inErrors": 3, "outErrors": 0},
        {"if": "Gi0/2", "status": "down", "inErrors": 0, "outErrors": 0},
    ],
}


def tool_list_devices() -> dict[str, Any]:
    return {"devices": [{"name": k, "ip": v["ip"]} for k, v in FAKE_DB.items()]}


def tool_get_device_health(device: str) -> dict[str, Any]:
    dev = FAKE_DB.get(device)
    if dev is None:
        return {"error": f"未知设备 {device}"}
    return {"device": device, **dev}


def tool_get_interface_errors(device: str) -> dict[str, Any]:
    dev = FAKE_DB.get(device)
    if dev is None:
        return {"error": f"未知设备 {device}"}
    if dev["status"] != "up":
        return {"error": f"{device} 离线，SNMP 不可达，无法采集接口"}
    return {"device": device, "interfaces": FAKE_IF.get(device, [])}


TOOL_IMPL: dict[str, Callable[..., dict[str, Any]]] = {
    "list_devices": tool_list_devices,
    "get_device_health": tool_get_device_health,
    "get_interface_errors": tool_get_interface_errors,
}


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """统一入口：任何工具抛错都转成 {"error": ...} 喂回模型，而不是让程序崩。

    这是 Agent 能「自我修正」的基础：模型看到错误信息才知道下一步该怎么办。
    """
    fn = TOOL_IMPL.get(name)
    if fn is None:
        return {"error": f"工具不存在：{name}，可用工具：{list(TOOL_IMPL)}"}
    try:
        return fn(**args)
    except TypeError as exc:
        return {"error": f"参数不对：{exc}"}
    except Exception as exc:  # noqa: BLE001 —— 工具层要兜住一切，保证循环不断
        return {"error": f"{type(exc).__name__}: {exc}"}


# ============================================================ 3. 规则决策器（无 Key 时的备胎）
class RulePolicy:
    """没有 API Key 时替代模型：走同一套「看观察结果决定下一步」的逻辑。

    它不是玩具——真实项目里，高频确定的路径用规则、模糊路径才用模型，成本更低更稳。
    """

    def __init__(self) -> None:
        self.devices: list[str] = []
        self.health: dict[str, dict[str, Any]] = {}
        self.if_errors: dict[str, list[dict[str, Any]]] = {}

    def next_call(self) -> tuple[str, dict[str, Any]] | None:
        if not self.devices:
            return "list_devices", {}
        for d in self.devices:
            if d not in self.health:
                return "get_device_health", {"device": d}
        for d in self.devices:
            if self.health[d].get("status") == "up" and d not in self.if_errors:
                return "get_interface_errors", {"device": d}
        return None  # 信息够了，收工

    def observe(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        if name == "list_devices":
            self.devices = [d["name"] for d in result["devices"]]
        elif name == "get_device_health":
            self.health[result.get("device", args.get("device", "?"))] = result
        elif name == "get_interface_errors":
            self.if_errors[result.get("device", args.get("device", "?"))] = result.get("interfaces", [])

    def final_answer(self) -> str:
        """把观察到的数据整理成分级诊断——注意：规则版和 LLM 版最终都得给这个东西。"""
        issues: list[str] = []
        for d in self.devices:
            h = self.health.get(d, {})
            if h.get("status") != "up":
                issues.append(f"[P0] {d}：设备离线 / SNMP 不可达 → 先确认电源、链路、community")
                continue
            cpu = int(h.get("cpu", 0))
            if cpu >= 80:
                issues.append(f"[P1] {d}：CPU {cpu}% 偏高 → 查是否有环路/广播风暴/异常流量")
            for itf in self.if_errors.get(d, []):
                if itf.get("status") != "up":
                    issues.append(f"[P1] {d}：接口 {itf['if']} 处于 down → 查光模块与对端")
                elif int(itf.get("inErrors", 0)) > 100:
                    issues.append(
                        f"[P1] {d}：接口 {itf['if']} 入方向错误包 {itf['inErrors']} → 疑光衰/误码，建议换纤或清洗尾纤"
                    )
            if not any(x.startswith(("[P0]", "[P1]")) and d in x for x in issues):
                issues.append(f"[P2] {d}：设备与接口均正常")
        order = {"P0": 0, "P1": 1, "P2": 2}
        issues.sort(key=lambda s: order.get(s[1:3], 9))
        return "巡检完成，诊断如下（按优先级）：\n" + "\n".join(f"- {i}" for i in issues)


# ============================================================ 4. 主循环
SYSTEM_PROMPT = (
    "你是网络运维 Agent。任务是用工具采集数据后给出诊断。规则：\n"
    "1) 先调用 list_devices 拿到设备清单；2) 逐台查健康状态；\n"
    "3) 只对在线设备查接口错误（离线设备查了也会失败，别浪费调用）；\n"
    "4) 信息够了就停止调用工具，直接输出诊断：按 P0/P1/P2 分级，每条给出依据。\n"
    "不要编造未采集到的数据。"
)


def run_agent(task: str, max_steps: int = 8, verbose: bool = True) -> dict[str, Any]:
    llm = LLMClient()
    use_llm = llm.config.ready
    trace: list[dict[str, Any]] = []
    t0 = time.perf_counter()

    if verbose:
        print(f"模式：{'LLM 驱动' if use_llm else '规则驱动（未配置 LLM_API_KEY）'}\n")
        print(f"任务：{task}\n" + "-" * 64)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    policy = RulePolicy()

    for step in range(1, max_steps + 1):
        if use_llm:
            out = llm.chat_tools(messages, TOOLS, temperature=0.2)
            calls = out["tool_calls"]
            if not calls:
                text = out["content"] or "（模型未返回内容）"
                trace.append({"step": step, "type": "final", "text": text})
                if verbose:
                    print(f"\n[STEP {step}] 决定收工\n{text}")
                return {"mode": "llm", "final": text, "trace": trace, "seconds": round(time.perf_counter() - t0, 2)}

            if verbose:
                print(f"\n[STEP {step}] 模型要求调用 {len(calls)} 个工具")
            # 把 assistant 的 tool_calls 原样回填，否则下一轮对话不合法
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
                result = execute_tool(c["name"], c["arguments"])
                if verbose:
                    print(f"   ACTION  {c['name']}({json.dumps(c['arguments'], ensure_ascii=False)})")
                    print(f"   OBSERVE {json.dumps(result, ensure_ascii=False)[:160]}")
                trace.append({"step": step, "type": "action", "tool": c["name"], "args": c["arguments"], "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "name": c["name"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            continue

        # ---- 规则驱动：形状和 LLM 版完全一样（这叫「同一套骨架，可替换的大脑」）
        nxt = policy.next_call()
        if nxt is None:
            text = policy.final_answer()
            trace.append({"step": step, "type": "final", "text": text})
            if verbose:
                print(f"\n[STEP {step}] 信息已足够，输出诊断\n{text}")
            return {"mode": "rule", "final": text, "trace": trace, "seconds": round(time.perf_counter() - t0, 2)}

        name, args = nxt
        result = execute_tool(name, args)
        policy.observe(name, args, result)
        trace.append({"step": step, "type": "action", "tool": name, "args": args, "result": result})
        if verbose:
            print(f"\n[STEP {step}]")
            print(f"   THOUGHT {thought_text(name, args)}")
            print(f"   ACTION  {name}({json.dumps(args, ensure_ascii=False)})")
            print(f"   OBSERVE {json.dumps(result, ensure_ascii=False)[:160]}")

    # ---- 兜底：步数用尽
    msg = f"达到最大步数 {max_steps}，强制结束（防止死循环）。已执行 {len(trace)} 次动作。"
    trace.append({"step": max_steps, "type": "abort", "text": msg})
    return {"mode": "llm" if use_llm else "rule", "final": msg, "trace": trace, "seconds": round(time.perf_counter() - t0, 2)}


def thought_text(name: str, args: dict[str, Any]) -> str:
    return {
        "list_devices": "先拿到设备清单，否则不知道要巡检什么。",
        "get_device_health": f"查 {args.get('device')} 是否在线，再决定要不要查接口。",
        "get_interface_errors": f"{args.get('device')} 在线，可以查接口错误包了。",
    }.get(name, "继续采集。")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="巡检所有设备，找出异常并给出处理建议。")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--out", default="out/day6_trace.json")
    args = ap.parse_args()

    result = run_agent(args.task, args.max_steps)
    print("-" * 64)
    print(f"完成 | 模式={result['mode']} 步数={len(result['trace'])} 耗时={result['seconds']}s")

    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完整 trace 已写入 {p}")


if __name__ == "__main__":
    main()
