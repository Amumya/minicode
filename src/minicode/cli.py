"""命令行入口。

用子命令结构（`minicode run ...`），因为后面还要加 `serve`。
现在只有 `run` 一个子命令。

设计上留了一个口子：`main()` 可以传入 provider 工厂，
这样测试能在不联网、不花钱的情况下把整条命令行路径跑一遍。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from minicode import __version__
from minicode.compact import CompactConfig, Compactor
from minicode.config import Settings
from minicode.events import Recorder
from minicode.loop import run as run_loop
from minicode.prompts import default_system
from minicode.skills import SkillLoader
from minicode.tasks import TaskStore
from minicode.tools import TodoList, build_registry


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="minicode", description="一个 coding agent")
    p.add_argument("--version", action="version", version=f"minicode {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="跑一个任务")
    r.add_argument("task", help="任务内容")
    r.add_argument("--model", default=None, help="覆盖 DEEPSEEK_MODEL")
    r.add_argument("--max-steps", type=int, default=None, help="最多几轮")
    r.add_argument("--workspace", type=Path, default=None, help="工作目录，默认当前目录")
    r.add_argument("--system", default=None, help="覆盖系统提示词")
    r.add_argument("--save", action="store_true", help="把轨迹写进 eval/runs/")
    r.add_argument("--json", action="store_true", help="只输出 JSON，便于脚本调用")
    r.add_argument("--quiet", action="store_true", help="不打印事件")
    r.add_argument("--files", action="store_true", help="额外挂上文件读写工具")
    r.add_argument("--plan", action="store_true", help="挂上 todo_write，让它可以写任务清单")
    r.add_argument("--skills", nargs="?", const="skills", default=None,
                   help="挂上技能加载，并把技能目录放进系统提示词（默认 ./skills）")
    r.add_argument("--no-python-hint", action="store_true",
                   help="不告诉 agent 该用哪个 Python 解释器（对照实验用）")
    r.add_argument("--compact", action="store_true",
                   help="开启上下文压缩（大工具输出落盘、旧结果截断、剪掉最早整对）")
    r.add_argument("--subagent", action="store_true",
                   help="挂上 task 工具，可以派子代理去做独立子任务（上下文隔离）")
    r.add_argument("--subagent-steps", type=int, default=6,
                   help="子代理的步数上限，默认 6")
    r.add_argument("--tasks", action="store_true",
                   help="挂上任务图工具（建/改/列/查/认领/完成），带依赖和环检测")
    r.add_argument("--compact-max-chars", type=int, default=4000,
                   help="单个工具结果超过多少字符就落盘（默认 4000）")
    r.add_argument("--compact-keep", type=int, default=3,
                   help="最近几个工具结果保留全文（默认 3）")

    a = sub.add_parser("app", help="起服务并打开独立窗口（桌面壳）")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8765)
    a.add_argument("--workspace", type=Path, default=None)
    a.add_argument("--browser", default=None, help="指定浏览器可执行文件")
    a.add_argument("--dry-run", action="store_true", help="只打印会启动什么，不真起")
    a.add_argument("--no-wait", action="store_true",
                   help="不等窗口关闭（服务在后台继续跑）")

    s = sub.add_parser("serve", help="起 HTTP 服务")
    s.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--workspace", type=Path, default=None)

    return p


def _resolve_settings(args, workspace: Path) -> Settings:
    env_file = workspace / ".env"
    base = Settings.from_env(env_file if env_file.is_file() else None)
    return Settings(
        api_key=base.api_key,
        base_url=base.base_url,
        model=args.model or base.model,
        max_steps=args.max_steps or base.max_steps,
    )


def _default_provider_factory(settings: Settings):
    from minicode.provider import AnthropicProvider

    return AnthropicProvider(settings)


def cmd_run(args, provider_factory=None) -> int:
    workspace = Path(args.workspace or Path.cwd()).resolve()
    settings = _resolve_settings(args, workspace)

    # 只有真要用模型时才要求 key。测试里注入了假 provider，不该被这条挡住。
    if provider_factory is None and not settings.api_key:
        print(
            "没有 API key。把 .env.example 复制成 .env，填上 DEEPSEEK_API_KEY。",
            file=sys.stderr,
        )
        return 2

    factory = provider_factory or _default_provider_factory
    try:
        provider = factory(settings)
    except Exception as exc:  # 网络或认证问题都在这里
        print(f"初始化模型客户端失败: {exc}", file=sys.stderr)
        return 2

    compactor = (
        Compactor(workspace, CompactConfig(
            max_tool_result_chars=args.compact_max_chars,
            keep_recent_tool_results=args.compact_keep,
        ))
        if args.compact else None
    )
    skills_loader = SkillLoader(workspace / args.skills) if args.skills else None
    todo = TodoList() if args.plan else None

    extra_tools = []
    if args.subagent:
        from minicode.subagent import TaskTool

        extra_tools.append(TaskTool(
            provider=provider,
            settings=settings,
            build_tools=lambda: build_registry(files=args.files),
            max_steps=args.subagent_steps,
        ))
    task_store = TaskStore(workspace / ".minicode" / "tasks") if args.tasks else None
    tools = build_registry(files=args.files, todo=todo, skills=skills_loader,
                           tasks=task_store, extra=extra_tools)

    rec = Recorder()
    started = time.time()
    result = run_loop(
        args.task,
        provider=provider,
        tools=tools,
        settings=settings,
        workspace=workspace,
        recorder=rec,
        compactor=compactor,
        system=args.system or default_system(
            skills=skills_loader,
            python_hint=None if args.no_python_hint else sys.executable,
            plan_hint=args.plan,
        ),
    )
    elapsed = time.time() - started

    if todo is not None and todo.items and not args.json:
        print("--- 任务清单（结束时）---")
        print(todo.render())
        print()

    payload = {
        "task": args.task,
        "model": settings.model,
        "elapsed_s": round(elapsed, 2),
        "result": result.to_dict(),
    }

    if args.save:
        stamp = int(time.time())
        out = Path.cwd() / "eval" / "runs" / f"task_{stamp}.json"
        rec.dump(out, extra={**payload, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        payload["saved_to"] = str(out)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    print(f"任务: {args.task}")
    print(f"模型: {settings.model}")
    print(
        f"结果: {'完成' if result.ok else '未完成'}  步数 {result.steps}  "
        f"stop_reason={result.stop_reason}"
    )
    print(f"工具调用 {result.tool_calls} 次，被拒 {result.denied} 次")
    print(
        f"token: 入 {result.usage.get('input_tokens')} 出 {result.usage.get('output_tokens')}   "
        f"耗时 {elapsed:.1f}s"
    )

    if not args.quiet:
        print("\n--- 事件 ---")
        for e in rec.events:
            brief = {
                k: v
                for k, v in e.data.items()
                if k in ("step", "tool", "exit_code", "failed", "reason", "stop_reason")
            }
            print(f"  {e.kind:<12} {brief}")

    print(f"\n--- 最终回答 ---\n{result.final_text}")
    if args.save:
        print(f"\n轨迹已写入 {payload['saved_to']}")

    return 0 if result.ok else 1


def cmd_app(args) -> int:
    from minicode.app_shell import run_app
    from minicode.service import create_app

    app = create_app(default_workspace=args.workspace)
    return run_app(
        app,
        host=args.host,
        port=args.port,
        workspace=args.workspace,
        browser=args.browser,
        dry_run=args.dry_run,
        wait_window=not args.no_wait,
    )


def cmd_serve(args) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            '没装 uvicorn。装法: pip install -e ".[serve]"',
            file=sys.stderr,
        )
        return 2

    from minicode.service import create_app

    app = create_app(default_workspace=args.workspace)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None, provider_factory=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args, provider_factory=provider_factory)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "app":
        return cmd_app(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
