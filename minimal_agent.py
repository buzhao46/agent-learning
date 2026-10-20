#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小可用 Agent 示例 —— ReAct 循环（网络运维场景）
=================================================
核心机制：Thought(思考) -> Action(调用工具) -> Observation(观察结果) -> 再思考
本示例不依赖任何外部 API 即可运行：
  - 工具层：模拟 SNMP / 网管采集（真实环境换成 pysnmp 即可）
  - 决策层：内置规则决策器 RuleLLM（替代大模型，驱动同一个 Agent 循环）
若设置环境变量 OPENAI_API_KEY / DEEPSEEK_API_KEY，可切换真实大模型（需 pip install openai）。

运行：python minimal_agent.py
"""

import os
import json

# ============================================================
# 1. 工具层：模拟 SNMP / 网管能力（真实项目替换为 pysnmp 调用）
# ============================================================
DEVICES = {
    "core-sw-01": {
        "ip": "10.10.0.1", "vendor": "Huawei", "cpu": 92, "mem": 63, "status": "up",
        "interfaces": {
            "GE0/0/1": {"oper": "up",   "in_err": 0,    "out_err": 0},
            "GE0/0/2": {"oper": "up",   "in_err": 1543, "out_err": 27},   # 错误包异常
        },
    },
    "core-rtr-02": {
        "ip": "10.10.0.2", "vendor": "Cisco", "cpu": 41, "mem": 38, "status": "up",
        "interfaces": {
            "Gi0/1": {"oper": "up",   "in_err": 2, "out_err": 0},
            "Gi0/2": {"oper": "down", "in_err": 0, "out_err": 0},         # 端口 down
        },
    },
    "agg-sw-03": {
        "ip": "10.10.0.3", "vendor": "ZTE", "cpu": 0, "mem": 0, "status": "down",
        "interfaces": {},                                                  # 设备不可达
    },
}


def list_devices():
    """返回纳管设备列表"""
    return list(DEVICES.keys())


def get_device_health(device: str):
    """采集单台设备的 CPU/内存/在线状态"""
    d = DEVICES[device]
    return {"device": device, "status": d["status"], "cpu": d["cpu"], "mem": d["mem"]}


def get_interface_errors(device: str):
    """采集单台设备各接口的收发包错误计数与 oper 状态"""
    d = DEVICES[device]
    if d["status"] != "up":
        return {"device": device, "error": "设备不可达，无法采集接口信息"}
    return {"device": device, "interfaces": d["interfaces"]}


TOOLS = {
    "list_devices": list_devices,
    "get_device_health": get_device_health,
    "get_interface_errors": get_interface_errors,
}

TOOL_SPECS = """
可用工具：
  list_devices()                      -> 列出所有纳管设备
  get_device_health(device)           -> 采集设备 CPU/内存/在线状态
  get_interface_errors(device)        -> 采集接口错误计数与端口状态
"""


# ============================================================
# 2. 决策层：RuleLLM —— 规则决策器，替代大模型驱动 ReAct 循环
#    它基于「已有观察」决定下一步动作，体现真正的观察驱动
# ============================================================
class RuleLLM:
    def decide(self, task, transcript):
        # 从历史观察中还原世界状态
        devices = None
        health = {}
        errors = {}

        for m in transcript:
            act, arg, obs = m["action"], m["args"], m["observation"]
            if act == "list_devices":
                devices = obs
            elif act == "get_device_health":
                health[arg["device"]] = obs
            elif act == "get_interface_errors":
                errors[arg["device"]] = obs

        # 步骤 1：还没拿到设备列表
        if devices is None:
            return {"thought": "我需要先知道有哪些设备纳管，才能逐一巡检。",
                    "action": "list_devices", "args": {}}

        # 步骤 2：逐台做健康检查
        for d in devices:
            if d not in health:
                return {"thought": f"设备列表已获取，接下来检查 {d} 的 CPU/内存/在线状态。",
                        "action": "get_device_health", "args": {"device": d}}

        # 步骤 3：对在线设备逐台检查接口错误
        for d in devices:
            if health.get(d, {}).get("status") == "up" and d not in errors:
                return {"thought": f"{d} 在线，继续采集它的接口错误计数。",
                        "action": "get_interface_errors", "args": {"device": d}}

        # 步骤 4：信息齐了，输出诊断结论
        return {"thought": "所有设备已巡检完毕，可以汇总异常并给出处置建议。",
                "type": "final", "content": self._diagnose(devices, health, errors, task)}

    @staticmethod
    def _diagnose(devices, health, errors, task):
        lines = [f"任务：{task}", "", "巡检结论："]
        problems = []
        for d in devices:
            h = health.get(d, {})
            if h.get("status") == "down":
                problems.append(f"  · {d}：设备离线 / SNMP 不可达，需现场确认供电与上联链路。")
                continue
            if h.get("cpu", 0) >= 80:
                problems.append(f"  · {d}：CPU 使用率 {h['cpu']}% 偏高，建议排查进程与流量突增。")
            ifs = errors.get(d, {}).get("interfaces", {})
            for name, st in ifs.items():
                if st["oper"] == "down":
                    problems.append(f"  · {d} / {name}：端口 down，检查光模块与对端配置。")
                elif st["in_err"] > 100:
                    problems.append(
                        f"  · {d} / {name}：入方向错误包 {st['in_err']} 个，疑似链路误码/光衰，建议清洁或更换光纤。")
        if problems:
            lines += problems
        else:
            lines.append("  · 全部设备健康，无异常。")
        lines += ["", "处理优先级：离线设备 > 端口 down > 高错误包 > CPU 偏高。"]
        return "\n".join(lines)


# ============================================================
# 3. Agent 主循环（ReAct）
# ============================================================
class Agent:
    def __init__(self, llm, tools, max_steps=20):
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps

    def run(self, task):
        print("=" * 62)
        print(f"任务：{task}")
        print("=" * 62)
        transcript = []
        for step in range(1, self.max_steps + 1):
            decision = self.llm.decide(task, transcript)

            # 终结：给出最终答案
            if decision.get("type") == "final":
                print(f"\n[STEP {step}] [THOUGHT] {decision['thought']}")
                print("\n" + "─" * 62)
                print("[FINAL ANSWER]")
                print("─" * 62)
                print(decision["content"])
                print("─" * 62)
                print(f"完成：共 {step} 步，调用工具 {len(transcript)} 次。")
                return decision["content"]

            # 执行一步：思考 -> 行动 -> 观察
            action, args = decision["action"], decision.get("args", {})
            print(f"\n[STEP {step}] [THOUGHT] {decision['thought']}")
            arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
            print(f"           [ACTION ] {action}({arg_str})")

            if action not in self.tools:
                print(f"           [ERROR  ] 未知工具 {action}，终止。")
                return None
            observation = self.tools[action](**args)
            print(f"           [OBSERVE] {json.dumps(observation, ensure_ascii=False)}")

            transcript.append({"action": action, "args": args, "observation": observation})

        print("达到最大步数仍未完成，已停止。")
        return None


if __name__ == "__main__":
    task = "巡检所有核心设备，找出异常并给出处理建议。"
    agent = Agent(llm=RuleLLM(), tools=TOOLS)
    agent.run(task)
