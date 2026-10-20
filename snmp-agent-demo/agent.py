# -*- coding: utf-8 -*-
"""
ReAct Agent 核心
----------------
Thought(思考) -> Action(调用工具) -> Observation(观察) -> 再思考 -> ... -> Final

决策层：RuleLLM（规则决策器，无需 LLM API Key 即可运行，靠"观察"驱动下一步）
工具层：真实 SNMP 或模拟数据（由设备上的 simulated 标志决定）
"""

from snmp_client import sim_health, sim_interfaces, real_health, real_interfaces

TOOL_SPECS = [
    {"name": "list_devices", "desc": "列出所有纳管设备", "args": {}},
    {"name": "get_device_health", "desc": "采集设备在线状态 / CPU / 内存 / sysDescr",
     "args": {"device": "设备名"}},
    {"name": "get_interface_errors", "desc": "采集各接口 oper 状态与收发错误计数",
     "args": {"device": "设备名"}},
]


class Tools:
    def __init__(self, devices):
        self.devices = devices  # {name: device_dict}

    def list_devices(self):
        return list(self.devices.keys())

    def get_device_health(self, device):
        d = self.devices[device]
        return sim_health(device) if d.get("simulated") else real_health(d)

    def get_interface_errors(self, device):
        d = self.devices[device]
        return sim_interfaces(device) if d.get("simulated") else real_interfaces(d)


class RuleLLM:
    """规则决策器：根据已有观察决定下一步；可替换为真实 LLM 的 Function Calling。"""

    def decide(self, task, transcript):
        listed = None
        health, errs = {}, {}
        for m in transcript:
            act = m["action"]
            if act == "list_devices":
                listed = m["observation"]
            elif act == "get_device_health":
                health[m["args"]["device"]] = m["observation"]
            elif act == "get_interface_errors":
                errs[m["args"]["device"]] = m["observation"]

        if listed is None:
            return {"thought": "先摸清有哪些设备纳管，才能逐一巡检。",
                    "action": "list_devices", "args": {}}

        for d in listed:
            if d not in health:
                return {"thought": f"设备清单已拿到，接着采集 {d} 的健康状态（在线 / CPU / 内存）。",
                        "action": "get_device_health", "args": {"device": d}}

        for d in listed:
            if health.get(d, {}).get("status") == "up" and d not in errs:
                return {"thought": f"{d} 在线，继续采集它的接口错误计数与端口状态。",
                        "action": "get_interface_errors", "args": {"device": d}}

        diag = self._diagnose(listed, health, errs, task)
        return {"thought": "所有设备巡检完毕，汇总异常并给出处置建议。",
                "type": "final", "content": diag["text"], "issues": diag["issues"]}

    @staticmethod
    def _diagnose(devices, health, errs, task):
        issues = []
        for d in devices:
            h = health.get(d, {})
            if h.get("status") == "down":
                issues.append({"device": d, "level": "critical",
                               "title": "设备离线 / SNMP 不可达",
                               "detail": h.get("error", ""),
                               "advice": "现场确认供电与上联链路，检查 SNMP community 与 ACL。"})
                continue
            cpu = h.get("cpu")
            if isinstance(cpu, int) and cpu >= 80:
                issues.append({"device": d, "level": "warn",
                               "title": f"CPU 使用率 {cpu}% 偏高", "detail": "",
                               "advice": "排查异常进程与流量突增。"})
            for name, st in errs.get(d, {}).get("interfaces", {}).items():
                if st.get("oper") == "down":
                    issues.append({"device": d, "level": "critical",
                                   "title": f"接口 {name} down", "detail": "",
                                   "advice": "检查光模块与对端配置。"})
                elif st.get("in_err", 0) > 100:
                    issues.append({"device": d, "level": "warn",
                                   "title": f"接口 {name} 入方向错误包 {st['in_err']}", "detail": "",
                                   "advice": "疑似链路误码 / 光衰，清洁或更换光纤。"})

        lines = [f"任务：{task}", "", "巡检结论："]
        if not issues:
            lines.append("  · 全部设备健康，无异常。")
        else:
            for it in issues:
                suffix = f"（{it['detail']}）" if it["detail"] else ""
                lines.append(f"  · {it['device']}：{it['title']}{suffix} → {it['advice']}")
            lines.append("")
            lines.append("处理优先级：离线设备 > 端口 down > 高错误包 > CPU 偏高。")
        return {"text": "\n".join(lines), "issues": issues}


def run_agent(task, devices):
    """执行一次完整 ReAct 循环，返回可直接给前端播放的 trace。"""
    tools = Tools(devices)
    llm = RuleLLM()
    transcript, trace = [], []
    final_issues, step = [], 0
    max_steps = max(6, len(devices) * 3 + 3)

    for step in range(1, max_steps + 1):
        dec = llm.decide(task, transcript)
        trace.append({"type": "thought", "step": step, "text": dec["thought"]})

        if dec.get("type") == "final":
            trace.append({"type": "final", "step": step, "text": dec["content"],
                          "issues": dec.get("issues", [])})
            final_issues = dec.get("issues", [])
            break

        action, args = dec["action"], dec.get("args", {})
        trace.append({"type": "action", "step": step, "tool": action, "args": args})
        try:
            obs = getattr(tools, action)(**args)
        except Exception as e:
            obs = {"error": str(e)}
        trace.append({"type": "observation", "step": step, "tool": action, "data": obs})
        transcript.append({"action": action, "args": args, "observation": obs})

    health = {}
    for m in transcript:
        if m["action"] == "get_device_health":
            health[m["args"]["device"]] = m["observation"]
    online = sum(1 for v in health.values() if v.get("status") == "up")

    return {
        "task": task,
        "steps": trace,
        "summary": {
            "devices": len(devices),
            "online": online,
            "offline": len(devices) - online,
            "issues": len(final_issues),
            "steps": step,
            "tool_calls": sum(1 for t in trace if t["type"] == "action"),
        },
    }
