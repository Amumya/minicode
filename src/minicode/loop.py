"""主循环。

形状很普通：拿回复 → 有工具调用就执行 → 结果回灌 → 再来一轮。
真正值得看的是三件事：

    1. 工具调用先过权限判定，被拒的不执行，但仍然回给模型一条结构化结果，
       让它自己知道"这条路走不通"，而不是静默跳过。
    2. 每一次模型调用和工具调用都记事件，指标从事件算，不从日志猜。
    3. 步数有上限，烧钱的路径必须有闸。
"""

from __future__ import annotations

import time

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from minicode.config import Settings
from minicode.events import Recorder
from minicode.compact import Compactor
from minicode.hooks import POST_TOOL, PRE_TOOL, HookBus, HookContext, default_hooks
from minicode.provider import ModelReply, Provider
from minicode.tools.base import Registry, ToolResult

def _default_system() -> str:
    """默认系统提示词统一从 prompts 生成，避免这里、prompts、子代理三处各写一份而发散。"""
    from minicode.prompts import default_system

    return default_system()


DEFAULT_SYSTEM = _default_system()


@dataclass
class RunResult:
    ok: bool
    final_text: str
    steps: int
    stop_reason: str
    usage: dict = field(default_factory=dict)
    tool_calls: int = 0
    denied: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "final_text": self.final_text,
            "steps": self.steps,
            "stop_reason": self.stop_reason,
            "usage": self.usage,
            "tool_calls": self.tool_calls,
            "denied": self.denied,
            "error": self.error,
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
    should_stop: "Callable[[], bool] | None" = None,
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
        # max_steps 是"**再给几步**"，不是"跑到第几步"。
        # 之前按绝对步数算：检查点停在 max_steps 时，续跑循环一次都不进，
        # 直接返回 ok=False —— 用户点「继续跑」看到的是"没反应"。
        end_step = start_step + max(1, settings.max_steps) - 1
        usage_total.update(resume.get("usage") or {})
        rec.add("resume", from_step=start_step - 1,
                max_steps=settings.max_steps, until_step=end_step)
    else:
        messages = [{"role": "user", "content": task}]
        start_step = 1
        end_step = settings.max_steps

    # 不传就用默认的（只挂一个权限钩子）。传了就完全用调用方的，便于测试和扩展。
    bus = hooks if hooks is not None else default_hooks()

    rec.add("start", task=task, model=settings.model, tools=tools.names(),
            hooks=bus.names())
    n_calls = 0      # 实际发起的工具调用次数（并行算多次）
    n_denied = 0
    n_models = 0     # 模型调用次数

    for step in range(start_step, end_step + 1):
        # 取消：每步开头检查一次。正在飞的模型调用打断不了，
        # 但它一返回就会被这里拦下 —— 不会继续执行工具、也不会再发下一次请求。
        if should_stop is not None and should_stop():
            rec.add("finish", step=step - 1, ok=False, reason="cancelled")
            if on_step is not None:
                on_step({"messages": messages, "step": step - 1, "usage": usage_total})
            return RunResult(
                ok=False,
                final_text="",
                steps=step - 1,
                stop_reason="cancelled",
                usage=usage_total,
                tool_calls=n_calls,
                denied=n_denied,
            )

        # 每次调模型之前压一次。压的是"要发出去的那份"，
        # 所以检查点里存的也是压缩后的内容 —— 恢复回来发送的东西才一致。
        if compactor is not None:
            messages, cstats = compactor.compact(messages)
            if cstats.changed:
                rec.add("compact", step=step, **cstats.as_dict())

        # 流式：把模型吐字的过程变成事件，界面就能逐字显示。
        # 不是每个 provider 都支持（测试里的假 provider 就没有），所以用 getattr 探测。
        delta_buffer: list[str] = []
        last_flush = [0.0]

        def _flush_delta(force: bool = False) -> None:
            if not delta_buffer:
                return
            now = time.time()
            if not force and now - last_flush[0] < 0.08 and sum(len(x) for x in delta_buffer) < 120:
                return
            rec.add("assistant_delta", step=step, text="".join(delta_buffer))
            delta_buffer.clear()
            last_flush[0] = now

        def _on_text(chunk: str) -> None:
            delta_buffer.append(chunk)
            _flush_delta()

        try:
            stream_fn = getattr(provider, "stream", None)
            if callable(stream_fn):
                reply = stream_fn(system, messages, tools.schemas(), _on_text)
                _flush_delta(force=True)
            else:
                reply = provider.complete(system, messages, tools.schemas())
        except Exception as exc:  # noqa: BLE001 - 网络/API 异常不能让整个进程挂掉
            rec.add("finish", step=step, ok=False, reason="provider_error")
            if on_step is not None:
                # 消息没变，但保留进度，至少能续跑
                on_step({"messages": messages, "step": step - 1, "usage": usage_total})
            return RunResult(
                ok=False,
                final_text="",
                steps=step,
                stop_reason="provider_error",
                usage=usage_total,
                tool_calls=n_calls,
                denied=n_denied,
                error=f"{type(exc).__name__}: {exc}",
            )
        n_models += 1
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

        # 最后一步还要求调工具：这些工具不执行。
        # 执行了结果也永远不会再发给模型（循环到此结束），只会白花钱，
        # 还可能有不可逆副作用。给一组"未执行"的结果让对话结构保持合法。
        if step == end_step and reply.tool_calls:
            skipped = [
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "is_error": True,
                    "content": "[skipped] 步数已用尽，这条工具调用没有执行",
                }
                for tc in reply.tool_calls
            ]
            messages.append({"role": "user", "content": skipped})
            if on_step is not None:
                on_step({"messages": messages, "step": step, "usage": usage_total})
            rec.add("finish", step=step, ok=False, reason="max_steps")
            return RunResult(
                ok=False,
                final_text=reply.text,
                steps=step,
                stop_reason="max_steps",
                usage=usage_total,
                tool_calls=n_calls,
                denied=n_denied,
            )

        if not reply.tool_calls:
            # 只有"模型自己说完了"才算成功。max_tokens = 回答被截断，
            # refusal = 拒绝回答 —— 都不算完成，否则半截答案会被当成"任务做完了"。
            stopped_ok = reply.stop_reason not in ("max_tokens", "refusal")
            rec.add("finish", step=step, ok=stopped_ok, reason=reply.stop_reason)
            # 正常收尾也要存一次检查点：多轮对话要靠它拿到"这一轮结束时完整的消息列表"。
            # 不存的话，只有被中断的 run 才有检查点，完成后就没法接着聊了。
            if on_step is not None:
                on_step({"messages": messages, "step": step, "usage": usage_total})
            return RunResult(
                ok=stopped_ok,
                final_text=reply.text,
                steps=step,
                stop_reason=reply.stop_reason,
                usage=usage_total,
                tool_calls=n_calls,
                denied=n_denied,
            )

        results: list[dict] = []
        for tc in reply.tool_calls:
            n_calls += 1
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

            try:
                tool_result = tool.run(call_args, workspace)
            except Exception as exc:  # noqa: BLE001 - 工具内部异常不该中断整个 run
                tool_result = ToolResult(
                    f"工具内部错误（{type(exc).__name__}）：{exc}",
                    exit_code=1,
                )
            rec.add(
                "tool_call",
                step=step,
                tool=tc.name,
                args=call_args,
                exit_code=tool_result.exit_code,
                failed=tool_result.failed,
                meta=tool_result.meta,
                # 给界面看的输出预览：不带 [exit=] 前缀，太长就截断。
                # 有了它用户不用点开事件流就能看到命令的结果。
                output=str(tool_result.content)[:400],
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

    rec.add("finish", step=end_step, ok=False, reason="max_steps")
    return RunResult(
        ok=False,
        final_text="",
        steps=end_step,
        stop_reason="max_steps",
        usage=usage_total,
        tool_calls=n_calls,
        denied=n_denied,
    )
