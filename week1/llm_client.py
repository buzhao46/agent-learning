"""Day 4-6 共用的 LLM 客户端。

设计原则：有 Key 走真实 API，没 Key 自动降级为 mock —— 保证任何一天都能跑通，
不会因为「还没申请到 Key」把学习卡死。

配置来源（任选其一）：
  1) 同目录 .env 文件（推荐，参考 .env.example）
  2) 环境变量 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

用法：
    from llm_client import LLMClient
    llm = LLMClient()
    print(llm.chat([{"role": "user", "content": "你好"}]))
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------- .env 解析
# 只写 15 行就够了，不用为此装 python-dotenv（顺便理解库都做了什么）
def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        # setdefault：已存在的真实环境变量优先，不会被 .env 覆盖
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    timeout: float = 60.0

    @classmethod
    def load(cls) -> LLMConfig:
        load_env()
        return cls(
            api_key=os.getenv("LLM_API_KEY", ""),
            base_url=os.getenv("LLM_BASE_URL", cls.base_url),
            model=os.getenv("LLM_MODEL", cls.model),
        )

    @property
    def ready(self) -> bool:
        """有没有可用 Key。没有就走 mock。"""
        return bool(self.api_key.strip())

    def describe(self) -> str:
        if not self.ready:
            return "mock 模式（未配置 LLM_API_KEY，输出为模拟内容）"
        return f"真实模式 | base_url={self.base_url} model={self.model}"


class LLMClient:
    """薄封装：把 openai SDK 的调用细节收在一处，业务代码只调 chat()。"""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.load()
        self._sdk: Any = None

    # ---------------------------------------------------------- 内部
    def _client(self) -> Any:
        """延迟导入：没装 openai 也不影响 mock 模式运行。"""
        if self._sdk is None:
            from openai import OpenAI

            self._sdk = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                timeout=self.config.timeout,
            )
        return self._sdk

    def _mock(self, messages: list[dict[str, str]], **kw: Any) -> str:
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        return (
            f"[mock] 未配置 LLM_API_KEY，这是模拟回复。\n"
            f"收到 {len(messages)} 条消息，最后一条用户输入：{last_user[:40]}\n"
            f"参数：temperature={kw.get('temperature')} max_tokens={kw.get('max_tokens')}"
        )

    # ---------------------------------------------------------- 对外
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 512,
        stream: bool = False,
    ) -> str:
        """一次性返回完整回复；stream=True 时边收边打印并返回全文。"""
        if not self.config.ready:
            return self._mock(messages, temperature=temperature, max_tokens=max_tokens)

        resp = self._client().chat.completions.create(
            model=self.config.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
        )
        if not stream:
            return resp.choices[0].message.content or ""

        chunks: list[str] = []
        for event in resp:
            delta = event.choices[0].delta.content or ""
            if delta:
                print(delta, end="", flush=True)
                chunks.append(delta)
        print()
        return "".join(chunks)

    def chat_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Day 6 用：支持 Function Calling 的一轮对话。

        返回 {"content": str|None, "tool_calls": [{"id","name","arguments"}]}
        """
        if not self.config.ready:
            raise RuntimeError("chat_tools 需要真实 Key，mock 模式下请用规则决策器")

        resp = self._client().chat.completions.create(
            model=self.config.model,
            messages=messages,
            tools=tools,
            temperature=temperature,
        )
        msg = resp.choices[0].message
        calls: list[dict[str, Any]] = []
        for tc in msg.tool_calls or []:
            calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": _safe_json(tc.function.arguments),
                    "raw_arguments": tc.function.arguments,
                }
            )
        return {"content": msg.content, "tool_calls": calls}


def _safe_json(raw: str) -> dict[str, Any]:
    """模型偶尔会给出非法 JSON，这里兜住，避免整个循环崩掉。"""
    import json

    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


if __name__ == "__main__":
    # 自检：python llm_client.py
    cfg = LLMConfig.load()
    print("配置：", cfg.describe())
    print("回复：", LLMClient(cfg).chat([{"role": "user", "content": "用一句话解释什么是 Agent"}]))
