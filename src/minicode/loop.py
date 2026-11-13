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
from typing import Callable

from minicode.config import Settings
from minicode.events import Recorder
from minicode.compact import Compactor
from minicode.hooks import POST_TOOL, PRE_TOOL, HookBus, HookContext, default_hooks
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
    resume: dict | None = None,
    on_step: "Callable[[dict], None] | None" = None,
    hooks: HookBus | None = None,
    compactor: Compactor | None = None,
) -> RunResult:
    """跑一个任务。

    resume 用来接着跑：传上一次存下来的 {"messages": [...], "step": n, "usage": {...}}，
    主循环就从第 n+1 步继续，而不是从头开始。

    on_step 每轮结束调一次，把当前 messages 和计数器交出去存盘。
    注意它拿到的是**已经补齐了工具结果**的 messages —— 存的时候必须是这个状态，
    否则恢复回来会缺 tool_result，下一轮请求直接 400。
    """
    workspace = Path(workspace).resolve()
    rec = recorder or Recorder()

    usage_total = {"input_tokens": 0, "output_tokens": 0}
    if resume:
        messages: list[dict] = list(resume.get("messages") or [])
        start_step = int(resume.get("step", 0)) + 1
        usage_total.update(resume.get("usage") or {})
        rec.add("resume", from_step=start_step - 1, max_steps=settings.max_steps)
    else:
        messages = [{"role": "user", "content": task}]
        start_step = 1

    # 不传就用默认的（只挂一个权限钩子）。传了就完全用调用方的，便于测试和扩展。
    bus = hooks if hooks is not None else default_hooks()

    rec.add("start", task=task, model=settings.model, tools=tools.names(),
            hooks=bus.names())
    n_calls = 0
    n_denied = 0

    for step in range(start_step, settings.max_steps + 1):
        # 每次调模型之前压一次。压的是"要发出去的那份"，
        # 所以检查点里存的也是压缩后的内容 —— 恢复回来发送的东西才一致。
        if compactor is not None:
            messages, cstats = compactor.compact(messages)
            if cstats.changed:
                rec.add("compact", step=step, **cstats.as_dict())

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
            ctx = HookContext(
                tool=tc.name,
                args=dict(tc.arguments or {}),
                workspace=workspace,
                step=step,
            )
            decision = bus.run(PRE_TOOL, ctx)
            for err in bus.drain_errors():
                rec.add("hook_error", step=step, **err)

            if decision.action == "deny":
                n_denied += 1
                rec.add(
                    "denied",
                    step=step,
                    tool=tc.name,
                    reason=decision.reason,
                    detail=decision.detail,
                    args=tc.arguments,
                    by=decision.by,
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "is_error": True,
                        "content": f"[denied: {decision.reason}] {decision.detail}",
                    }
                )
                continue

            # 钩子可能改写过参数，后面一律用生效后的那份
            call_args = decision.args or ctx.args

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

            tool_result = tool.run(call_args, workspace)
            rec.add(
                "tool_call",
                step=step,
                tool=tc.name,
                args=call_args,
                exit_code=tool_result.exit_code,
                failed=tool_result.failed,
                meta=tool_result.meta,
            )

            post_ctx = HookContext(
                tool=tc.name,
                args=call_args,
                workspace=workspace,
                step=step,
                result=tool_result,
            )
            bus.run(POST_TOOL, post_ctx)
            for err in bus.drain_errors():
                rec.add("hook_error", step=step, **err)

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "is_error": tool_result.failed,
                    "content": tool_result.to_model_content(),
                }
            )

        messages.append({"role": "user", "content": results})

        # 工具结果补齐之后才存检查点，见上面 on_step 的说明
        if on_step is not None:
            on_step({"messages": messages, "step": step, "usage": usage_total})

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
