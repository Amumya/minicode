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
from minicode.config import Settings
from minicode.events import Recorder
from minicode.loop import run as run_loop
from minicode.tools import default_registry

DEFAULT_SYSTEM = (
    "你是一个在命令行里干活的助手。可以用 bash 工具执行命令来完成用户的任务。"
    "命令跑完会带上退出码，退出码非 0 表示失败。任务完成后直接给出结论，不要再调工具。"
)


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

    rec = Recorder()
    started = time.time()
    result = run_loop(
        args.task,
        provider=provider,
        tools=default_registry(),
        settings=settings,
        workspace=workspace,
        recorder=rec,
        system=args.system or DEFAULT_SYSTEM,
    )
    elapsed = time.time() - started

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


def main(argv: list[str] | None = None, provider_factory=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args, provider_factory=provider_factory)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
