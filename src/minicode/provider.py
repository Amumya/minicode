"""模型通信层。

把不同协议返回的东西归一成一个 ModelReply，主循环只认这个结构。
现在只实现 Anthropic 协议（DeepSeek 的 /anthropic 端点走的就是它），
以后要加 OpenAI 兼容协议，只需要再写一个 Provider，主循环不用动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from minicode.config import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)


class Provider(Protocol):
    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelReply: ...


class AnthropicProvider:
    def __init__(self, settings: Settings, max_tokens: int = 2048) -> None:
        import anthropic  # 延迟导入：跑不联网的测试时不需要装

        self._client = anthropic.Anthropic(
            api_key=settings.require_key(), base_url=settings.base_url
        )
        self.model = settings.model
        self.max_tokens = max_tokens

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelReply:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        resp = self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                calls.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        arguments=dict(getattr(block, "input", {}) or {}),
                    )
                )

        usage = getattr(resp, "usage", None)
        return ModelReply(
            text="".join(text_parts),
            tool_calls=calls,
            stop_reason=str(getattr(resp, "stop_reason", "")),
            usage={
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            },
        )
