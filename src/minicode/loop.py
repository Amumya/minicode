"""主循环。

形状很普通：拿回复 → 有工具调用就执行 → 结果回灌 → 再来一轮。
真正值得看的是三件事：

    1. 工具调用先过权限判定，被拒的不执行，但仍然回给模型一条结构化结果，
       让它自己知道"这条路走不通"，而不是静默跳过。
    2. 每一次模型调用和工具调用都记事件，指标从事件算，不从日志猜。
    3. 步数有上限，烧钱的路径必须有闸。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from minicode.config import Settings
from minicode.events import Recorder
from minicode.policy import judge_tool_call
from minicode.provider import ModelReply, Provider
from minicode.tools.base import Registry

DEFAULT_SYSTEM = (
    "你是一个在命令行里干活的助手。可以用 bash 工具执行命令来完成用户的任务。"
    "命令跑完会带上退出码，退出码非 0 表示失败。任务完成后直接给出结论，不要再调工具。"
)


@dataclass
class RunResult:
    ok: bool
    final_text: str
    steps: int
    stop_reason: str
    usage: dict = field(default_factory=dict)
    tool_calls: int = 0
    denied: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "final_text": self.final_text,
            "steps": self.steps,
            "stop_reason": self.stop_reason,
            "usage": self.usage,
            "tool_calls": self.tool_calls,
            "denied": self.denied,
        }


def _assistant_message(reply: ModelReply) -> dict:
    blocks: list[dict] = []
    if reply.text:
        blocks.append({"type": "text", "text": reply.text})
    for tc in reply.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.arguments,
            }
        )
    return {"role": "assistant", "content": blocks}


def run(
    task: str,
    *,
    provider: Provider,
    tools: Registry,
    settings: Settings,
    workspace: Path,
    recorder: Recorder | None = None,
    system: str = DEFAULT_SYSTEM,
) -> RunResult:
    workspace = Path(workspace).resolve()
    rec = recorder or Recorder()
    rec.add("start", task=task, model=settings.model, tools=tools.names())

    messages: list[dict] = [{"role": "user", "content": task}]
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    n_calls = 0
    n_denied = 0

    for step in range(1, settings.max_steps + 1):
        reply = provider.complete(system, messages, tools.schemas())
        n_calls += 1
        for k in usage_total:
            usage_total[k] += reply.usage.get(k, 0)
        rec.add(
            "model_call",
            step=step,
            stop_reason=reply.stop_reason,
            usage=reply.usage,
            tool_calls=len(reply.tool_calls),
            text_len=len(reply.text),
        )

        messages.append(_assistant_message(reply))

        if not reply.tool_calls:
            rec.add("finish", step=step, ok=True, reason=reply.stop_reason)
            return RunResult(
                ok=True,
                final_text=reply.text,
                steps=step,
                stop_reason=reply.stop_reason,
                usage=usage_total,
                tool_calls=n_calls - 1,
                denied=n_denied,
            )

        results: list[dict] = []
        for tc in reply.tool_calls:
            verdict = judge_tool_call(tc.name, tc.arguments, workspace)
            if verdict is not None and verdict.denied:
                n_denied += 1
                rec.add(
                    "denied",
                    step=step,
                    tool=tc.name,
                    reason=verdict.reason,
                    detail=verdict.detail,
                    args=tc.arguments,
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "is_error": True,
                        "content": f"[denied: {verdict.reason}] {verdict.detail}",
                    }
                )
                continue

            tool = tools.get(tc.name)
            if tool is None:
                rec.add("unknown_tool", step=step, tool=tc.name)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "is_error": True,
                        "content": f"没有这个工具: {tc.name}",
                    }
                )
                continue

            tool_result = tool.run(tc.arguments, workspace)
            rec.add(
                "tool_call",
                step=step,
                tool=tc.name,
                args=tc.arguments,
                exit_code=tool_result.exit_code,
                failed=tool_result.failed,
                meta=tool_result.meta,
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "is_error": tool_result.failed,
                    "content": tool_result.to_model_content(),
                }
            )

        messages.append({"role": "user", "content": results})

    rec.add("finish", step=settings.max_steps, ok=False, reason="max_steps")
    return RunResult(
        ok=False,
        final_text="",
        steps=settings.max_steps,
        stop_reason="max_steps",
        usage=usage_total,
        tool_calls=n_calls - 1,
        denied=n_denied,
    )
