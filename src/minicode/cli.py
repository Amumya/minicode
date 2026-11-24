"""命令行入口。

用子命令结构（`minicode run ...`），因为后面还要加 `serve`。
现在只有 `run` 一个子命令。

设计上留了一个口子：`main()` 可以传入 provider 工厂，
这样测试能在不联网、不花钱的情况下把整条命令行路径跑一遍。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from minicode import __version__
from minicode.compact import CompactConfig, Compactor, provider_summarizer
from minicode.config import Settings
from minicode.events import Recorder
from minicode.memory import MemoryStore
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
                   help="开启上下文压缩（按预算逐级：落盘、归档、瘦身，最后模型摘要）")
    r.add_argument("--team", action="store_true",
                   help="挂上 team 工具：常驻成员名册 + 异步邮箱（send/wait/inbox，闲时冷挂起）")
    r.add_argument("--subagent", action="store_true",
                   help="挂上 task 工具，可以派子代理去做独立子任务（上下文隔离）")
    r.add_argument("--subagent-steps", type=int, default=64,
                   help="子代理的步数上限，默认 64")
    r.add_argument("--tasks", action="store_true",
                   help="挂上任务图工具（建/改/列/查/认领/完成），带依赖和环检测")
    r.add_argument("--memory", action="store_true",
                   help="挂上长期记忆工具（remember / recall / forget），存 .minicode/memory.jsonl")
    r.add_argument("--mcp", action="store_true",
                   help="连 .minicode/mcp.json 里启用的 MCP server，只暴露 allow 里列出的工具")
    r.add_argument("--compact-budget", type=int, default=100000,
                   help="上下文硬预算（字符），只有超过才触发瘦身/摘要（默认 100000）")
    r.add_argument("--compact-max-chars", type=int, default=30000,
                   help="单条工具结果超过多少字符就落盘（默认 30000）")
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
    a.add_argument("--mode", choices=("auto", "gui", "app", "window"), default="auto",
                   help="窗口模式：auto=优先 PySide6 原生窗口，失败退回浏览器（默认）；"
                        "gui=强制 PySide6；app=Edge/Chrome 独立窗口；window=普通浏览器窗口")
    a.add_argument("--no-redis", action="store_true",
                   help="不用 Redis（内存存储，重新打开后历史会丢）")

    d = sub.add_parser("dev", help="一键起开发环境（Redis + 服务，可选开窗口）")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8765)
    d.add_argument("--workspace", type=Path, default=None)
    d.add_argument("--open", action="store_true", help="顺便打开窗口")
    d.add_argument("--token", default=None, help="访问令牌（设了之后接口要求鉴权）")
    d.add_argument("--mode", choices=("auto", "gui", "app", "window"), default="auto")
    d.add_argument("--build", action="store_true", help="启动前重新构建前端")
    d.add_argument("--reload", action="store_true", help="改 Python 代码自动重载")
    d.add_argument("--no-redis", action="store_true",
                   help="不用 Redis（内存存储，进程退出后历史丢失）")

    m = sub.add_parser("mcp", help="检查 MCP 配置（连 server、列工具、看 schema 大小）")
    m.add_argument("--workspace", type=Path, default=None)
    m.add_argument("--config", type=Path, default=None,
                   help="直接指定 mcp.json 路径（默认 <workspace>/.minicode/mcp.json）")

    s = sub.add_parser("serve", help="起 HTTP 服务")
    s.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--workspace", type=Path, default=None)
    s.add_argument("--token", default=None, help="访问令牌（设了之后接口要求鉴权）")
    s.add_argument("--no-redis", action="store_true",
                   help="不用 Redis（内存存储，重启后历史会丢）")
    s.add_argument("--memory-log", action="store_true",
                   help="把服务进程内存每 10 秒写入 <workspace>/.minicode/memory.log")

    return p


def _resolve_settings(args, workspace: Path) -> Settings:
    env_file = workspace / ".env"
    base = Settings.from_env(env_file if env_file.is_file() else None)
    return Settings(
        api_key=base.api_key,
        base_url=base.base_url,
        model=args.model or base.model,
        max_steps=args.max_steps or base.max_steps,
        max_tokens=base.max_tokens,
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

    # 摘要器由 provider 包出来注入：Compactor 不认识 Provider，离线测试可以塞一个纯函数。
    compactor = (
        Compactor(
            workspace,
            CompactConfig(
                context_char_limit=args.compact_budget,
                max_tool_result_chars=args.compact_max_chars,
                keep_recent_results=args.compact_keep,
            ),
            summarize=provider_summarizer(provider),
        )
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
    if args.team:
        from minicode.team import TeamTool

        extra_tools.append(TeamTool(
            provider=provider,
            settings=settings,
            build_tools=lambda: build_registry(files=args.files),
            max_steps=64,
        ))
    task_store = TaskStore(workspace / ".minicode" / "tasks") if args.tasks else None
    memory = MemoryStore(workspace / ".minicode" / "memory.jsonl") if args.memory else None

    # MCP：显式开关 + 配置里的 enabled/allow 双重限制
    mcp_session = None
    mcp_servers: list[str] = []
    if getattr(args, "mcp", False):
        from minicode.tools.mcp import connect_mcp

        mcp_session = connect_mcp(workspace)
        extra_tools.extend(mcp_session.tools)
        mcp_servers = [item["server"] for item in mcp_session.report["servers"]]
        for warning in mcp_session.report["warnings"]:
            print(f"[mcp] {warning}", file=sys.stderr)
        if not mcp_session.tools:
            print("[mcp] 没有可用的 MCP 工具（检查 enabled 和 allow）", file=sys.stderr)
    tools = build_registry(files=args.files, todo=todo, skills=skills_loader,
                           tasks=task_store, memory=memory, extra=extra_tools,
                           compact=args.compact)

    rec = Recorder()
    started = time.time()
    # Ctrl-C 只插旗，让当前这步跑完再收尾；MCP 子进程交给 finally 关。
    stop_flag = threading.Event()
    try:
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
                compact_hint=args.compact,
                tasks_hint=task_store is not None,
                memory_hint=memory is not None,
              subagent_hint=args.subagent,
              team_hint=args.team,
                memory_snapshot=(memory.snapshot(args.task) if memory is not None else None),
                mcp_servers=mcp_servers or None,
            ),
            should_stop=stop_flag.is_set,
        )
    except KeyboardInterrupt:
        stop_flag.set()
        print("已中断（Ctrl-C）。", file=sys.stderr)
        return 130
    finally:
        if mcp_session is not None:
            mcp_session.close()
            mcp_session = None

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
            if e.kind == "thinking":
                # thinking 是多行文本：单独排版，标清第几步，保留换行
                step = e.data.get("step", "?")
                print(f"  {'thinking':<12} 第 {step} 步")
                text = str(e.data.get("text", ""))
                for line in text.splitlines() or [""]:
                    print(f"    {line}")
                print()
                continue
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



def _ensure_persistent_store(root: Path, no_redis: bool):
    """让 app/serve 默认用仓库里的便携 Redis（历史持久化）。

    返回 (自己起的 redis 进程, url)；已经有外部 MINICODE_REDIS_URL 时不启动新进程。
    """
    from minicode import dev

    if no_redis:
        os.environ.pop("MINICODE_REDIS_URL", None)
        print("存储: 内存（--no-redis，重新打开后历史会丢）")
        return None, None

    external = os.environ.get("MINICODE_REDIS_URL", "").strip()
    if external:
        print(f"存储: {external}（外部 Redis，历史会保存）")
        return None, external

    proc, url = dev.ensure_redis(root)
    if url:
        dev.apply_env(url)
        print(f"存储: {url}（仓库里的便携 Redis，历史会保存）")
    else:
        print("存储: 内存（没找到便携 Redis，重新打开后历史会丢）")
    return proc, url


def cmd_app(args) -> int:
    from minicode import dev
    from minicode.app_shell import run_app
    from minicode.service import create_app

    if args.dry_run:
        print("存储: （--dry-run 不启动 Redis）")
        redis_proc = None
    else:
        redis_proc, _ = _ensure_persistent_store(_repo_root(), args.no_redis)

    try:
        workspace = Path(args.workspace or _default_workspace()).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        server_cmd = [
            sys.executable, "-m", "minicode", "serve",
            "--host", args.host, "--port", str(args.port), "--memory-log",
            "--workspace", str(workspace),
        ]
        return run_app(
            None,
            server_cmd=server_cmd,
            host=args.host,
            port=args.port,
            workspace=workspace,
            browser=args.browser,
            mode=args.mode,
            dry_run=args.dry_run,
            wait_window=not args.no_wait,
        )
    finally:
        dev.stop(redis_proc)


def _default_workspace() -> Path:
    """桌面/服务模式的默认工作区：不再用仓库根，避免运行时产物污染源码树。"""
    raw = os.environ.get("MINICODE_WORKSPACE", "").strip()
    workspace = Path(raw).expanduser() if raw else (_repo_root() / "workspace")
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _repo_root() -> Path:
    """找仓库根目录（有 web/ 或 .tools/ 的那一层）。"""
    here = Path(__file__).resolve().parents[2]
    if (here / "web").is_dir() or (here / ".tools").is_dir():
        return here
    return Path.cwd()


def cmd_dev(args) -> int:
    """一键起开发环境。

    做了四件事：可选构建前端 → 确保 Redis → 起服务 →（可选）开窗口。
    Ctrl+C 的时候把自己起的 Redis 一起收掉，不留孤儿进程。
    """
    import uvicorn

    from minicode import dev
    from minicode.app_shell import run_app
    from minicode.service import create_app

    root = _repo_root()

    # 端口被占用时给一句人话提示，而不是让 uvicorn 抛一屏 WinError 10048
    from minicode.dev import port_open

    if port_open(args.host, args.port):
        print(f"端口 {args.port} 已经被占用了。")
        print("  可能你之前的 minicode dev 还开着 —— 关掉那个窗口再试。")
        print(f"  或者换个端口：minicode dev --port {args.port + 1}")
        return 1

    if args.build:
        dev.build_frontend(root)

    redis_proc, url = dev.ensure_redis(root, enabled=not args.no_redis)
    if args.no_redis:
        # 显式清掉：否则外部设置的 MINICODE_REDIS_URL 会让服务照样连 Redis，
        # 和用户明确要求的内存模式不符
        os.environ.pop("MINICODE_REDIS_URL", None)
    dev.apply_env(url)
    if url:
        state = "（已在运行，直接复用）" if redis_proc is None else "（刚启动）"
        print(f"Redis: {url} {state}")
    else:
        print("Redis: 没启用（用内存存储 —— 服务重启后历史会丢）")

    app = create_app(default_workspace=args.workspace, token=args.token,
                              reconcile_stale=True)
    print(f"服务: http://{args.host}:{args.port}/")
    if args.token:
        print("访问令牌已启用：界面右上角（令牌一栏）填同一个值")
    if args.open:
        print("按 Ctrl+C 结束（会一起收掉 Redis）")
    else:
        print("浏览器打开上面的地址；按 Ctrl+C 结束（会一起收掉 Redis）")

    try:
        if args.open:
            return run_app(
                app,
                host=args.host,
                port=args.port,
                workspace=args.workspace,
                mode=args.mode,
            )

        if args.reload:
            # reload 需要一个 import 字符串，uvicorn 会在子进程里重建 app。
            # 注意 factory 拿不到 --workspace，所以这里显式把它写进环境变量，
            # 让 create_app 的默认工作区跟着走（否则 --reload 会静默忽略 --workspace）。
            if args.workspace:
                os.environ["MINICODE_WORKSPACE"] = str(Path(args.workspace).resolve())
            uvicorn.run(
                "minicode.service:create_app",
                factory=True,
                host=args.host,
                port=args.port,
                reload=True,
                reload_dirs=[str(root / "src")],
            )
        else:
            if getattr(args, "memory_log", False):
                _start_memory_logger(args.workspace)
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        dev.stop(redis_proc)


def cmd_mcp(args) -> int:
    """检查 MCP 配置：把每个 server 连一遍，列出模式/版本/工具/白名单过滤结果。

    故意不跑模型：这一步是排查配置问题用的，不该花 API 费用。
    """
    from minicode.mcp import McpClient, McpError, load_servers
    from minicode.tools.mcp import schema_stats

    workspace = Path(args.workspace or Path.cwd()).resolve()
    try:
        configs = load_servers(workspace, args.config)
    except McpError as exc:
        print(f"MCP 配置有问题：{exc}", file=sys.stderr)
        return 2

    if not configs:
        print(f"没有 MCP 配置（找的是 {args.config or workspace / '.minicode' / 'mcp.json'}）")
        print("格式见 README 的「MCP」一节：servers 数组，每项要有 name/command，")
        print("并且 enabled=true + allow=[...] 才会真的挂上。")
        return 1

    print(f"MCP 配置：{len(configs)} 个 server")
    failed = 0
    connected: list[McpClient] = []
    for config in configs:
        print(f"\n[{config.name}]  {config.describe()}")
        print(f"  enabled={config.enabled}  allow={', '.join(config.allow) or '(空)'}")
        if not config.enabled:
            print("  → 跳过（默认不启用，要在配置里写 enabled: true）")
            continue
        client = McpClient(config, cwd=workspace)
        try:
            client.start()
            tools = client.list_tools()
        except McpError as exc:
            failed += 1
            print(f"  ✗ 连接失败：{exc}")
            client.close()
            continue
        connected.append(client)
        by_name = {str(t.get("name", "")): t for t in tools if isinstance(t, dict)}
        allowed = [name for name in config.allow if name in by_name]
        missing = [name for name in config.allow if name not in by_name]
        blocked = [name for name in by_name if name not in config.allow]
        stats = schema_stats([by_name[name] for name in allowed])
        print(f"  ✓ 模式={client.mode}  协议={client.protocol_version}  启动={client.startup_ms}ms")
        print(f"  工具 {len(tools)} 个，允许 {len(allowed)} 个：{', '.join(allowed) or '(空)'}")
        if blocked:
            print(f"  未允许（默认拒绝）：{', '.join(blocked)}")
        if missing:
            print(f"  ⚠ allow 里这些工具 server 没有：{', '.join(missing)}")
        print(f"  允许工具的 schema：{stats['chars']} 字符 ≈ {stats['est_tokens']} tokens/轮")
        if not allowed:
            print("  ⚠ 一个工具都没允许 —— 模型看不到任何工具")

    for client in connected:
        client.close()
    return 1 if failed else 0


def _start_memory_logger(workspace) -> None:
    """后台线程：服务端 RSS 写 memory.log，配合桌面窗口定位 OOM。"""
    import threading
    import time as time_module
    from pathlib import Path as PathClass

    from minicode.meminfo import rss_mb

    base = PathClass(workspace) if workspace else PathClass.cwd()

    def loop() -> None:
        path = base / ".minicode" / "memory.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        while True:
            try:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(f"{time_module.strftime('%H:%M:%S')} server rss={rss_mb()}MB\n")
            except OSError:
                pass
            time_module.sleep(10)

    threading.Thread(target=loop, daemon=True, name="memory-logger").start()


def cmd_serve(args) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            '没装 uvicorn。装法: pip install -e ".[serve]"',
            file=sys.stderr,
        )
        return 2

    from minicode import dev
    from minicode.service import create_app

    redis_proc, _ = _ensure_persistent_store(_repo_root(), args.no_redis)
    try:
        app = create_app(default_workspace=args.workspace, token=args.token,
                              reconcile_stale=True)
        if args.token:
            print("访问令牌已启用：浏览器界面里需要填同一个令牌")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    finally:
        dev.stop(redis_proc)


def main(argv: list[str] | None = None, provider_factory=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args, provider_factory=provider_factory)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "app":
        return cmd_app(args)
    if args.command == "dev":
        return cmd_dev(args)
    if args.command == "mcp":
        return cmd_mcp(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
