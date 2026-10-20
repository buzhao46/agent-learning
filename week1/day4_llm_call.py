"""Day 4 · 调用大模型 API：OpenAI 兼容接口 / messages 结构 / 流式输出

必懂的三个概念：
  1) messages 是「角色 + 内容」的列表，模型没有记忆，历史全靠你每次带上
     system   —— 定人设/规则（最该花时间写的地方）
     user     —— 用户输入
     assistant—— 模型历史回复
     tool     —— 工具执行结果（Day 6 用）
  2) temperature / max_tokens 是每次请求的参数，不是模型属性
  3) 流式（stream=True）本质是 SSE，逐 token 返回，体验好、首字延迟低

运行：
    python day4_llm_call.py                       # 无 Key 时自动 mock，先把链路跑通
    python day4_llm_call.py --stream              # 流式
    python day4_llm_call.py --once "解释一下 SNMP 的 OID"
    python day4_llm_call.py                       # 不带参数 = 多轮对话，输入 exit 退出
"""
from __future__ import annotations

import argparse
import json

from llm_client import LLMClient

SYSTEM_PROMPT = (
    "你是一名网络运维专家，擅长 SNMP / OTN / 交换机排障。"
    "回答要求：先给结论，再给依据；不超过 150 字；不确定就说不确定，不要编造命令。"
)


def build_messages(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """system 永远放第一条。history 只放 user/assistant 轮次。"""
    return [{"role": "system", "content": SYSTEM_PROMPT}, *history]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", help="只问一句就退出")
    ap.add_argument("--stream", action="store_true", help="流式输出")
    ap.add_argument("--show-payload", action="store_true", help="打印实际发给模型的 JSON")
    args = ap.parse_args()

    llm = LLMClient()
    print(f"模式：{llm.config.describe()}\n")

    if args.once:
        msgs = build_messages([{"role": "user", "content": args.once}])
        if args.show_payload:
            print(json.dumps(msgs, ensure_ascii=False, indent=2), "\n")
        print(llm.chat(msgs, temperature=0.3, max_tokens=400, stream=args.stream))
        return

    # ---- 多轮对话：注意 history 会一直变长，真实项目要按 token 截断或做摘要
    history: list[dict[str, str]] = []
    print("进入多轮对话（输入 exit 退出，输入 /clear 清空历史）\n")
    while True:
        try:
            user = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user in {"exit", "quit"}:
            break
        if user == "/clear":
            history.clear()
            print("（历史已清空）\n")
            continue
        if not user:
            continue

        history.append({"role": "user", "content": user})
        if args.show_payload:
            print("→ 发送消息数：", len(build_messages(history)))

        reply = llm.chat(build_messages(history), temperature=0.3, max_tokens=600, stream=args.stream)
        if not args.stream:
            print(f"AI > {reply}\n")
        else:
            print()
        # 关键：把模型回复也塞回历史，下一轮它才「记得」
        history.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
