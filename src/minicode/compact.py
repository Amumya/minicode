"""上下文压缩。

为什么需要：工具输出会随着步数累积。一次 `Get-Content` 读进 3 万字，
八步之后上下文就爆了 —— 而且**每一轮都要把历史重发一遍**，成本是平方级的。

三种手段，都是确定性的（不调模型）：
    1. 大输出落盘：单个工具结果超过阈值就写到文件，上下文里只留头尾和路径
    2. 旧结果截断：最近 N 个保留全文，更早的只留开头一小段
    3. 剪掉最早的整轮对话：超过消息上限时，从最早的"assistant + 它对应的 user"
       整对开始删

最重要的约束（上一版踩过的坑）：
    **绝对不能把 assistant 的 tool_use 和它对应的 tool_result 拆开。**
    拆开之后下一次请求的对话结构非法，服务端直接 400。
    所以第 3 条只删"整对"，而且永远保留第 1 条（任务本身）和最后 2 条。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CompactConfig:
    max_tool_result_chars: int = 4000      # 超过这个就落盘
    keep_recent_tool_results: int = 3      # 最近几个保留全文
    old_tool_result_chars: int = 400       # 更早的截断到多少
    max_messages: int = 40                 # 消息条数上限
    head_chars: int = 600                  # 落盘后保留的开头
    tail_chars: int = 400                  # 落盘后保留的结尾
    enabled: bool = True


@dataclass
class CompactStats:
    chars_before: int = 0
    chars_after: int = 0
    persisted: int = 0
    trimmed: int = 0
    snipped_messages: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        saved = self.chars_before - self.chars_after
        return {
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "saved_chars": saved,
            "saved_ratio": round(saved / self.chars_before, 3) if self.chars_before else 0.0,
            "persisted": self.persisted,
            "trimmed": self.trimmed,
            "snipped_messages": self.snipped_messages,
        }

    @property
    def changed(self) -> bool:
        return self.persisted or self.trimmed or self.snipped_messages


def _text_len(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_len(v) for v in value)
    if isinstance(value, dict):
        return sum(_text_len(v) for v in value.values())
    return 0


def messages_chars(messages: list[dict]) -> int:
    return sum(_text_len(m.get("content")) for m in messages)


def _result_blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]


def _block_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(c.get("text", "")) for c in content if isinstance(c, dict)
        )
    return str(content or "")


class Compactor:
    def __init__(self, workspace: Path, config: CompactConfig | None = None) -> None:
        self.workspace = Path(workspace)
        self.cfg = config or CompactConfig()
        self.dir = self.workspace / ".minicode" / "tool_results"

    # ---------------------------------------------------------------- 落盘

    def _persist(self, tool_use_id: str, text: str) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_use_id)[:40]
        path = self.dir / f"{safe}_{int(time.time() * 1000)}.txt"
        path.write_text(text, encoding="utf-8")
        label = self.cfg.head_chars
        tail = self.cfg.tail_chars
        head = text[:label]
        end = text[-tail:] if tail > 0 else ""
        try:
            rel = path.relative_to(self.workspace)
        except ValueError:
            rel = path
        return (
            f"{head}\n\n…（中间省略 {len(text) - label - len(end)} 字符，"
            f"完整内容在 {rel}，需要时用 read_file 或 Get-Content 读）…\n\n{end}"
        )

    # ---------------------------------------------------------------- 主流程

    def compact(self, messages: list[dict]) -> tuple[list[dict], CompactStats]:
        stats = CompactStats(chars_before=messages_chars(messages))
        if not self.cfg.enabled or not messages:
            stats.chars_after = stats.chars_before
            return messages, stats

        out = [dict(m) for m in messages]  # 浅拷贝，别改调用方的列表
        out = self._persist_large(out, stats)
        out = self._trim_old_results(out, stats)
        out = self._snip(out, stats)

        stats.chars_after = messages_chars(out)
        return out, stats

    def _persist_large(self, messages: list[dict], stats: CompactStats) -> list[dict]:
        for message in messages:
            for block in _result_blocks(message):
                text = _block_text(block)
                if len(text) <= self.cfg.max_tool_result_chars:
                    continue
                block["content"] = self._persist(
                    str(block.get("tool_use_id", "unknown")), text
                )
                stats.persisted += 1
        return messages

    def _trim_old_results(self, messages: list[dict], stats: CompactStats) -> list[dict]:
        """最近 N 个工具结果保留全文，更早的截断。

        截断的仍是内容字符串 —— tool_use / tool_result 的配对关系没动，
        所以对话结构始终合法。
        """
        blocks: list[tuple[dict, str]] = []
        for message in messages:
            for block in _result_blocks(message):
                blocks.append((block, _block_text(block)))

        keep = self.cfg.keep_recent_tool_results
        old = blocks[:-keep] if keep > 0 else blocks
        for block, text in old:
            if len(text) <= self.cfg.old_tool_result_chars:
                continue
            block["content"] = (
                text[: self.cfg.old_tool_result_chars]
                + f"\n…（更早的工具输出已截断，原长 {len(text)}）"
            )
            stats.trimmed += 1
        return messages

    def _snip(self, messages: list[dict], stats: CompactStats) -> list[dict]:
        """超过上限时，从最早的整对开始删。

        安全的切点：assistant 后面紧跟 user 的地方。删的时候把这两个一起删，
        不会留下孤立的 tool_use 或 tool_result。
        """
        limit = self.cfg.max_messages
        if limit <= 0 or len(messages) <= limit:
            return messages

        # 永远保留：第 1 条（任务）、最后 2 条
        keep_head, keep_tail = 1, 2
        budget = limit - keep_head - keep_tail
        if budget <= 0:
            return messages

        body = messages[keep_head : len(messages) - keep_tail]
        cut = 0
        # 找尽量靠后的安全切点，使剩余条数不超过 budget。
        # 切点必须落在"user（工具结果）之后"，也就是 i+2：
        # 只删到 i+1 会把 assistant 的 tool_use 删掉却留下它的 tool_result，
        # 留下孤立的 tool_result，下一次请求就是 400。
        target = len(body) - budget
        for i in range(target, len(body) - 1):
            if body[i].get("role") == "assistant" and body[i + 1].get("role") == "user":
                cut = i + 2
                break
        else:
            return messages  # 找不到安全切点，就别动

        removed = keep_head + cut
        stats.snipped_messages = cut
        stats.notes.append(f"从第 {keep_head + 1} 条起删了 {cut} 条整对历史")
        return messages[:keep_head] + body[cut:] + messages[len(messages) - keep_tail:]

    # ---------------------------------------------------------------- 便捷函数

    def summary_line(self, stats: CompactStats) -> str:
        d = stats.as_dict()
        if not stats.changed:
            return f"上下文未压缩（{d['chars_before']} 字符）"
        return (
            f"上下文 {d['chars_before']} → {d['chars_after']} 字符"
            f"（省 {d['saved_ratio']:.0%}；落盘 {d['persisted']}、"
            f"截断 {d['trimmed']}、删整对 {d['snipped_messages']}）"
        )
