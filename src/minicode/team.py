"""Agent Teams（MVP）：Lead 把总任务拆给多个带角色的 teammate 并行跑。

和子代理的区别：
    * `task` 工具：一个子任务，串行执行，只回结论；
    * `team` 工具：一个总任务 + 2–6 个成员（名字/角色/可选的个人子任务），
      成员在各自线程里并行跑，结果按成员汇总回 Lead。

MVP 不做 s13 的常驻邮箱/任务认领/审议：成员都是一次性线程，跑完即退出；
每个成员的结论、步数、token 都写进 tool_call 事件的 meta，前端「编队」视图从这里渲染。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from pathlib import Path
from typing import Callable

from minicode.config import Settings
from minicode.subagent import _default_child_system, report_stub, run_subagent
from minicode.tools.base import Registry, ToolResult

MAX_MEMBERS = 6
TEAM_TIMEOUT_SECONDS = 300
MAX_WORKERS = 3


def _timeout_report():
    from minicode.subagent import SubagentReport

    return SubagentReport(ok=False, steps=0, stop_reason="timeout",
                          error="团队成员执行超时（编队整体超时，先返回已有结果）")


class TeamTool:
    """把「一个 Lead + 多个 teammate」包装成 `team` 工具。"""

    name = "team"
    description = (
        "把总任务拆给多个带角色的成员并行执行，只拿回各自的结论。"
        "适合多角度审查、并行调查、独立子任务；成员之间不共享上下文。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "团队的总任务"},
            "members": {
                "type": "array",
                "description": "2–6 个成员；每个成员有 name、role，可选 task",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "role": {"type": "string"},
                        "task": {"type": "string", "description": "该成员负责的子任务（可选）"},
                    },
                    "required": ["name", "role"],
                },
            },
            "max_steps": {"type": "integer", "description": "每个成员最多几步，默认 32"},
        },
        "required": ["task", "members"],
    }

    def __init__(
        self,
        *,
        provider,
        settings: Settings,
        build_tools: Callable[[], Registry],
        max_steps: int = 32,
        on_progress: Callable[[dict], None] | None = None,
        timeout_seconds: float = TEAM_TIMEOUT_SECONDS,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.build_tools = build_tools
        self.max_steps = max_steps
        self.on_progress = on_progress
        self.timeout_seconds = timeout_seconds

    def run(self, args: dict, cwd: Path) -> ToolResult:
        task = str((args or {}).get("task", "")).strip()
        raw_members = (args or {}).get("members")
        if not task:
            return ToolResult("缺少 task", exit_code=2)
        if not isinstance(raw_members, list) or not 2 <= len(raw_members) <= MAX_MEMBERS:
            return ToolResult(f"members 必须是 2–{MAX_MEMBERS} 个成员的数组", exit_code=2)

        members: list[dict] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_members, start=1):
            if not isinstance(raw, dict):
                return ToolResult(f"第 {index} 个成员不是对象", exit_code=2)
            name = str(raw.get("name", "")).strip() or f"member{index}"
            role = str(raw.get("role", "")).strip() or "通用成员"
            if name in seen:
                return ToolResult(f"成员名重复: {name}", exit_code=2)
            seen.add(name)
            members.append({
                "name": name,
                "role": role,
                "task": str(raw.get("task", "")).strip(),
            })

        steps = (args or {}).get("max_steps")
        max_steps = int(steps) if isinstance(steps, int) and steps > 0 else self.max_steps

        def progress(payload: dict) -> None:
            if not self.on_progress:
                return
            try:
                self.on_progress(payload)
            except Exception:  # noqa: BLE001 - 进度写失败不能拖垮成员线程
                pass

        def work(member: dict) -> tuple[dict, object]:
            progress({"agent": member["name"], "role": member["role"], "phase": "start"})
            prompt = member["task"] or f"团队总任务：{task}\n\n你的角色：{member['role']}\n完成你负责的部分并给出结论。"
            system = (
                _default_child_system()
                + f"\n\n你在一个团队里，角色是：{member['role']}。"
                "只负责你的部分，给出可以直接合并进最终报告的结论。"
            )
            report = run_subagent(
                prompt,
                provider=self.provider,
                settings=self.settings,
                workspace=cwd,
                build_tools=self.build_tools,
                max_steps=max_steps,
                system=system,
            )
            progress({
                "agent": member["name"], "role": member["role"],
                "phase": "done", "ok": report.ok,
                "steps": report.steps,
                "tokens": report.input_tokens + report.output_tokens,
            })
            return member, report

        reports: list[tuple[dict, object]] = []
        executor = ThreadPoolExecutor(
            max_workers=min(MAX_WORKERS, len(members)),
            thread_name_prefix="team",
        )
        futures = {executor.submit(work, member): member for member in members}
        try:
            for future in as_completed(futures, timeout=self.timeout_seconds):
                reports.append(future.result())
        except TimeoutError:
            # 个别成员卡死不能把整次编队挂住：标记超时、先返回；后台线程继续跑完
            # 也只会写进度事件，不再阻塞父 run。
            for member in members:
                if not any(item[0] is member for item in reports):
                    reports.append((member, _timeout_report()))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        blocks: list[str] = []
        meta_members: list[dict] = []
        any_ok = False
        for member, report in reports:
            any_ok = any_ok or report.ok
            output = (report.final_text or "").strip() or report_stub(report)
            blocks.append(f"## {member['name']}（{member['role']}）\n{output}")
            meta_members.append({
                "name": member["name"],
                "role": member["role"],
                "ok": report.ok,
                "steps": report.steps,
                "tokens": report.input_tokens + report.output_tokens,
                "tool_calls": report.tool_calls,
                "output": output,
                "error": report.error,
            })

        return ToolResult(
            "\n\n".join(blocks),
            exit_code=0 if any_ok else 1,
            meta={"team": {"task": task, "members": meta_members}},
        )
