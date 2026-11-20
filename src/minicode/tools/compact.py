"""模型主动要求压缩上下文的入口。

为什么要拆成「发信号 + 循环里执行」：
    `Compactor.compact_history()` 需要完整的 messages 和当前请求，
    而 `Tool.run()` 的签名只有 `(args, cwd)`。
    要让工具自己压，要么改工具协议，要么让工具持有可变状态 —— 都不划算。
    发一个信号出来，由主循环在「这批工具结果补齐之后」统一处理，是最小改动。

为什么要有这个工具：
    自动压缩只在超过预算时才触发。但有些时候模型自己知道该整理了 ——
    比如一个大阶段刚做完、接下来要从头开始查另一件事。
    这时主动压一次，比等到上下文塞满再被动压缩更划算。
"""

from __future__ import annotations

from pathlib import Path

from minicode.tools.base import ToolResult

COMPACT_SIGNAL = "compact_requested"

class CompactTool:
    name = "compact"
    description = (
        "把此前较长的对话整理成一段摘要，腾出上下文空间。"
        "在上下文快满、或一个阶段做完要开始新阶段时调用。"
        "调用后本批工具执行完会立即整理，不需要重复调用，也不要指望它返回历史原文。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "为什么要压缩，一句话（会记进轨迹，便于回看）",
            }
        },
        "required": [],
    }

    def run(self, args: dict, cwd: Path) -> ToolResult:
        reason = str((args or {}).get("reason", "") or "").strip()
        if reason:
            note = f"已记录压缩请求（{reason}）。本批工具执行完会立刻整理上下文。"
        else:
            note = "已记录压缩请求。本批工具执行完会立刻整理上下文。"
        return ToolResult(note, exit_code=0, meta={COMPACT_SIGNAL: True})
