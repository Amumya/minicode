"""工具执行前后的钩子。

设计原因：权限判定最初硬编码在主循环中。改为钩子机制后，审计、拦截、
参数改写与耗时统计均可作为钩子接入，主循环无需修改。

语义：
* 按注册顺序执行；首个返回 deny 的钩子立即生效，后续钩子不再执行。
* rewrite 修改参数后，后续钩子接收修改后的参数。
* 返回 None 表示不干预（审计型钩子）。
* 钩子抛出异常时：critical=True 的钩子返回 deny，critical=False 的钩子忽略异常。
  默认 critical=True：权限钩子异常时必须拒绝，不得放行。
  审计类钩子注册时传 critical=False，异常不影响任务执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Literal

# 事件名用常量，避免各处手写字符串拼错
PRE_TOOL = "pre_tool"
POST_TOOL = "post_tool"

Action = Literal["allow", "deny", "rewrite"]


@dataclass(frozen=True)
class HookContext:
    """钩子看到的上下文。故意做成不可变，改参数只能通过 rewrite 返回。"""

    tool: str
    args: dict
    workspace: Path
    step: int = 0
    result: object | None = None      # 只有 POST_TOOL 才有
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HookResult:
    action: Action
    reason: str = ""
    detail: str = ""
    args: dict | None = None          # 只有 rewrite 才有
    by: str = ""                      # 哪个钩子做的决定，便于排查

    @classmethod
    def allow(cls) -> "HookResult":
        return cls("allow")

    @classmethod
    def deny(cls, reason: str, detail: str = "", by: str = "") -> "HookResult":
        return cls("deny", reason=reason, detail=detail, by=by)

    @classmethod
    def rewrite(cls, args: dict, by: str = "") -> "HookResult":
        return cls("rewrite", args=args, by=by)


@dataclass
class HookSpec:
    event: str
    fn: Callable[[HookContext], HookResult | None]
    name: str
    critical: bool = True


class HookBus:
    def __init__(self) -> None:
        self._specs: list[HookSpec] = []
        self.errors: list[dict] = []

    def on(
        self,
        event: str,
        fn: Callable[[HookContext], HookResult | None],
        *,
        name: str | None = None,
        critical: bool = True,
    ) -> HookBus:
        self._specs.append(
            HookSpec(event=event, fn=fn, name=name or getattr(fn, "__name__", "hook"),
                     critical=critical)
        )
        return self

    def hooks_for(self, event: str) -> list[HookSpec]:
        return [s for s in self._specs if s.event == event]

    def names(self, event: str | None = None) -> list[str]:
        return [s.name for s in self._specs if event is None or s.event == event]

    def drain_errors(self) -> list[dict]:
        out, self.errors = self.errors, []
        return out

    def run(self, event: str, ctx: HookContext) -> HookResult:
        current = ctx
        for spec in self.hooks_for(event):
            try:
                result = spec.fn(current)
            except Exception as exc:  # noqa: BLE001 - 钩子的异常必须在这一层接住
                self.errors.append({
                    "hook": spec.name, "event": event, "error": f"{type(exc).__name__}: {exc}",
                    "critical": spec.critical,
                })
                if spec.critical:
                    return HookResult.deny(
                        f"hook_error:{spec.name}", f"{type(exc).__name__}: {exc}",
                        by=spec.name,
                    )
                continue

            if result is None:
                continue
            if result.action == "deny":
                return replace(result, by=result.by or spec.name) if not result.by else result
            if result.action == "rewrite":
                current = replace(current, args=result.args or current.args)
        # 一路走完：把（可能被改写过的）参数带回去。
        # 注意这里不能直接返回 allow() —— 那会把前面 rewrite 的效果丢掉。
        changed = current.args if current.args != ctx.args else None
        return HookResult("allow", args=changed)


# ------------------------------------------------------------------ 内置钩子


def policy_hook(ctx: HookContext) -> HookResult | None:
    """把权限判定包成一个钩子。critical=True：判定自己出错时必须拒绝。"""
    from minicode.policy import judge_tool_call

    verdict = judge_tool_call(ctx.tool, ctx.args, ctx.workspace)
    if verdict is None or verdict.allowed:
        return None
    return HookResult.deny(verdict.reason, verdict.detail, by="policy")


def audit_hook(sink: list[dict]) -> Callable[[HookContext], None]:
    """审计钩子：把每次调用记下来。不做决定，所以返回 None。

    挂在 pre_tool 上（执行前），这样被拒的调用也会留下记录。
    """

    def hook(ctx: HookContext) -> None:
        sink.append({
            "tool": ctx.tool,
            "args": ctx.args,
            "step": ctx.step,
        })
        return None

    hook.__name__ = "audit"
    return hook


def default_hooks() -> HookBus:
    """默认只有一个权限钩子。审计要显式加，不然每次跑都会多记一份。"""
    bus = HookBus()
    bus.on(PRE_TOOL, policy_hook, name="policy", critical=True)
    return bus
