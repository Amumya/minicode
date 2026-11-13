"""子代理：把一件小事整个丢出去，只拿回结论。

核心是**上下文隔离**：子代理有自己的一份 messages，跑完之后父代理只收到
一段最终的文本结论，看不到子代理中间调了多少工具、输出了多少字。
这样做的好处是父代理的上下文不会被细节撑爆。

几条硬约束（都是为了别把系统搞失控）：

    * 子代理的工具集里**没有 `task`** —— 不许递归，深度固定为 1
    * 子代理有自己的步数上限，跟父代理的互不影响
    * 子代理跑挂了不许把父代理带崩：转成一条 is_error 的工具结果返回
    * 子代理的花费（步数 / token）回填到工具结果的 meta 里，方便统计
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from minicode.config import Settings
from minicode.events import Recorder
from minicode.loop import run as run_loop
from minicode.tools.base import Registry, ToolResult


@dataclass
class SubagentReport:
    """子代理这一趟的花费，回填给父代理做统计（不进它的上下文）。"""

    ok: bool
    steps: int
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    final_text: str = ""
    error: str = ""
    events: list[dict] = field(default_factory=list)


def run_subagent(
    prompt: str,
    *,
    provider,
    settings: Settings,
    workspace: Path,
    build_tools: Callable[[], Registry],
    max_steps: int = 6,
    system: str = "",
) -> SubagentReport:
    """跑一个独立的子任务。返回结论 + 花费，不返回中间过程。"""
    child_settings = Settings(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.model,
        max_steps=max_steps,
    )
    recorder = Recorder()

    try:
        result = run_loop(
            prompt,
            provider=provider,
            tools=build_tools(),
            settings=child_settings,
            workspace=workspace,
            recorder=recorder,
            system=system or "你是子代理，只做被交代的这件小事，做完直接给结论。",
        )
    except Exception as exc:  # noqa: BLE001 - 子代理异常不能让父代理崩
        return SubagentReport(ok=False, steps=0, stop_reason="error",
                              error=f"{type(exc).__name__}: {exc}")

    return SubagentReport(
        ok=result.ok,
        steps=result.steps,
        stop_reason=result.stop_reason,
        input_tokens=result.usage.get("input_tokens", 0),
        output_tokens=result.usage.get("output_tokens", 0),
        tool_calls=result.tool_calls,
        final_text=result.final_text,
        events=[{"kind": e.kind} for e in recorder.events],
    )


class TaskTool:
    """把子代理包装成工具。名字就叫 `task`，跟参考实现一致。"""

    name = "task"
    description = (
        "把一件独立的子任务交给子代理去跑，只拿回它的结论。"
        "适合「需要翻很多文件但只想要结论」的情况 —— 中间过程不会占用你的上下文。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "交给子代理的完整交代"},
            "max_steps": {"type": "integer", "description": "子代理最多几步，默认 6"},
        },
        "required": ["prompt"],
    }

    def __init__(
        self,
        *,
        provider,
        settings: Settings,
        build_tools: Callable[[], Registry],
        max_steps: int = 6,
        system: str = "",
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.build_tools = build_tools
        self.max_steps = max_steps
        self.system = system
        self.reports: list[SubagentReport] = []

    def run(self, args: dict, cwd: Path) -> ToolResult:
        prompt = str((args or {}).get("prompt", "")).strip()
        if not prompt:
            return ToolResult("缺少 prompt", exit_code=1)

        steps = args.get("max_steps")
        max_steps = int(steps) if isinstance(steps, int) and steps > 0 else self.max_steps

        report = run_subagent(
            prompt,
            provider=self.provider,
            settings=self.settings,
            workspace=cwd,
            build_tools=self.build_tools,
            max_steps=max_steps,
            system=self.system,
        )
        self.reports.append(report)

        if report.error:
            return ToolResult(f"子代理出错：{report.error}", exit_code=1,
                              meta={"subagent": report.__dict__})

        # 只把结论给父代理；花费放 meta，不占它的上下文
        content = report.final_text.strip() or report_stub(report)
        return ToolResult(
            content,
            exit_code=0 if report.ok else 1,
            meta={
                "subagent_steps": report.steps,
                "subagent_tokens": report.input_tokens + report.output_tokens,
                "subagent_tool_calls": report.tool_calls,
                "subagent_ok": report.ok,
            },
        )


def report_stub(report: SubagentReport) -> str:
    return (
        f"子代理没有给出结论（ok={report.ok}, stop={report.stop_reason}, "
        f"steps={report.steps}）"
    )
