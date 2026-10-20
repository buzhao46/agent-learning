"""Day 5 · 模型参数调优：temperature / top_p / max_tokens / system prompt 对比实验

为什么值得花一天？
    Agent 里「决策」和「生成」要用不同参数：
      决策（选哪个工具、给什么参数）-> temperature 0~0.2，要稳定可复现
      写报告 / 解释原因          -> temperature 0.6~1.0，要自然
    这个结论你要自己跑出来，比看十篇博客记得牢。

运行：
    python day5_param_lab.py            # 全矩阵，结果写 out/day5_param_lab.md
    python day5_param_lab.py --quick    # 只跑 temperature 对比
验收：生成 md 文件，且你能说出「temperature=0 和 1.3 的差别在哪」。
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from llm_client import LLMClient

# 故意选一个「有标准答案、但表达空间大」的问题
QUESTION = "核心交换机 CPU 突然从 30% 涨到 92%，请给出最可能的 3 个原因，按可能性排序，每点不超过 20 字。"

SYSTEMS = {
    "A_无约束": "你是一个助手。",
    "B_运维专家": (
        "你是资深网络运维专家。只输出编号列表，每点不超过 20 字，"
        "不要寒暄，不要解释你是谁。按可能性从高到低排序。"
    ),
}

TEMPERATURES = [0.0, 0.7, 1.3]
MAX_TOKENS = [40, 200]


def run_matrix(llm: LLMClient, quick: bool) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    temps = [0.0, 1.3] if quick else TEMPERATURES
    for sys_name, sys_prompt in SYSTEMS.items():
        for temp in temps:
            for max_tokens in (MAX_TOKENS if not quick else [200]):
                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": QUESTION},
                ]
                t0 = time.perf_counter()
                text = llm.chat(messages, temperature=temp, max_tokens=max_tokens)
                cost = time.perf_counter() - t0
                rows.append(
                    {
                        "system": sys_name,
                        "temperature": temp,
                        "max_tokens": max_tokens,
                        "seconds": round(cost, 2),
                        "chars": len(text),
                        "text": text.strip().replace("\n", " ⏎ "),
                    }
                )
                print(f"[{sys_name} T={temp} mt={max_tokens}] {cost:.2f}s {len(text)}字")
    return rows


def write_report(rows: list[dict[str, object]], path: Path) -> None:
    lines = [
        "# Day 5 参数对比实验记录",
        "",
        f"- 问题：{QUESTION}",
        f"- 样本数：{len(rows)}",
        "",
        "| system prompt | temperature | max_tokens | 耗时(s) | 字数 | 输出摘要 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        snippet = str(r["text"])[:70].replace("|", "/")
        lines.append(
            f"| {r['system']} | {r['temperature']} | {r['max_tokens']} | "
            f"{r['seconds']} | {r['chars']} | {snippet} |"
        )
    lines += [
        "",
        "## 我的结论（自己填，别抄）",
        "",
        "- temperature=0 时多次运行是否一致：",
        "- max_tokens 太小会怎样（是否被截断）：",
        "- 加 system 约束后输出格式变化：",
        "- 以后 Agent 决策我会用 temperature=____，写报告用 ____",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入 {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="out/day5_param_lab.md")
    args = ap.parse_args()

    llm = LLMClient()
    print(f"模式：{llm.config.describe()}\n")
    rows = run_matrix(llm, args.quick)
    write_report(rows, Path(args.out))

    print("\n看这三点就够了：")
    print("  1. temperature=0 跑两次，输出是否几乎一样（可复现性）")
    print("  2. max_tokens=40 是否被硬截断（截断 = 解析会失败，Agent 决策必须留够）")
    print("  3. 有 system 约束后，格式是否稳定（Agent 靠格式解析，稳定 > 好看）")


if __name__ == "__main__":
    main()
