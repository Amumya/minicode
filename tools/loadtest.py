#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发 / 多会话压测工具。

用的是**假 provider**：不联网、不花钱、不依赖 API key，测的是服务层
（worker 线程、store 写入、SSE 分发、会话分组、followup）能不能扛住并发。

用法：

    .venv\\Scripts\\python.exe tools\\loadtest.py
    .venv\\Scripts\\python.exe tools\\loadtest.py --runs 80 --concurrency 40
    .venv\\Scripts\\python.exe tools\\loadtest.py --no-sse --no-followups
    .venv\\Scripts\\python.exe tools\\loadtest.py --json docs/loadtest.json

指标：
- 并发建 run 的延迟（p50 / p95 / max）
- 全部 run 跑完的墙钟时间、失败数
- 并发 SSE：每条流是否收到 start/end、总事件数
- 并发 followup：多会话是否各自独立
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from minicode.config import Settings
from minicode.provider import ModelReply, ToolCall
from minicode.service import create_app
from minicode.service.store import InMemoryStore, RedisStore


class LoadProvider:
    """每个 run 一个实例：前 tool_rounds 步调工具，之后收尾。"""

    def __init__(self, tool_rounds: int, delay: float, tool: str) -> None:
        self.calls = 0
        self.tool_rounds = tool_rounds
        self.delay = delay
        self.tool = tool

    def complete(self, system, messages, tools):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.calls <= self.tool_rounds:
            if self.tool == "todo_write":
                args = {"todos": [{"content": f"压测第 {self.calls} 步", "status": "in_progress"}]}
            else:
                args = {"command": "echo loadtest"}
            return ModelReply(
                tool_calls=[ToolCall(
                    id=f"call-{self.calls}",
                    name=self.tool,
                    arguments=args,
                )],
                stop_reason="tool_use",
                usage={"input_tokens": 5, "output_tokens": 2},
            )
        return ModelReply(
            text=f"完成，共 {self.calls} 次模型调用",
            stop_reason="end_turn",
            usage={"input_tokens": 3, "output_tokens": 4},
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * p)))
    return ordered[idx]


def _wait_health(client: httpx.Client, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("/healthz").json().get("ok"):
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise RuntimeError("服务没在 15s 内起来")


def _wait_runs(client: httpx.Client, run_ids: list[str], timeout: float = 120.0) -> list[dict[str, Any]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        bodies = [client.get(f"/runs/{rid}").json() for rid in run_ids]
        if all(b["status"] in ("done", "error") for b in bodies):
            return bodies
        time.sleep(0.05)
    raise RuntimeError(f"{len(run_ids)} 个 run 没在 {timeout}s 内结束")


def _read_sse(client: httpx.Client, run_id: str) -> dict[str, Any]:
    kinds: list[str] = []
    try:
        with client.stream("GET", f"/runs/{run_id}/events", timeout=httpx.Timeout(60.0)) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    kinds.append(line[7:].strip())
    except Exception as exc:  # noqa: BLE001 - 压测里把失败记下来，不要中断全部
        return {"run_id": run_id, "ok": False, "error": f"{type(exc).__name__}: {exc}", "events": len(kinds)}
    return {
        "run_id": run_id,
        "ok": "start" in kinds and "end" in kinds,
        "events": len(kinds),
        "kinds": kinds,
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print("\n=== 并发压测结果 ===")
    print(f"  runs/并发     : {summary['runs']} / {summary['concurrency']}")
    print(f"  建 run        : 成功 {summary['create_ok']}/{summary['runs']}  "
          f"p50={summary['create_p50_ms']:.1f}ms  p95={summary['create_p95_ms']:.1f}ms  "
          f"max={summary['create_max_ms']:.1f}ms")
    print(f"  完成          : ok={summary['done_ok']}  error={summary['done_error']}  "
          f"总墙钟={summary['wall_ms']:.0f}ms")
    print(f"  事件          : 每条 run 事件数 min/max = {summary['events_min']}/{summary['events_max']}")
    if summary.get("sse"):
        print(f"  SSE           : 流 {summary['sse_ok']}/{summary['sse_streams']} 正常  "
              f"事件总数={summary['sse_events_total']}  min/max/流={summary['sse_events_min']}/{summary['sse_events_max']}")
    if summary.get("followups"):
        print(f"  followup      : 成功 {summary['follow_ok']}/{summary['followups']}  "
              f"墙钟={summary['follow_wall_ms']:.0f}ms  会话数={summary['conversations']}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="minicode 并发/多会话压测（假 provider，不联网）")
    ap.add_argument("--runs", type=int, default=40, help="并发创建多少个 run")
    ap.add_argument("--concurrency", type=int, default=20, help="建 run 的线程数")
    ap.add_argument("--tool-rounds", type=int, default=2, help="每个 run 调几次工具")
    ap.add_argument("--tool", choices=("bash", "todo_write"), default="bash",
                    help="压测时用哪个工具（todo_write 没有子进程开销，更适合量服务层）")
    ap.add_argument("--delay", type=float, default=0.01, help="假 provider 每次调用耗时（秒）")
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--workspace", default=str(Path.cwd()), help="workspace 目录")
    ap.add_argument("--no-sse", action="store_true", help="不测并发 SSE")
    ap.add_argument("--no-followups", action="store_true", help="不测并发 followup")
    ap.add_argument("--redis-url", default=None,
                    help="用这个 Redis 当 store（默认 InMemory；别指向正在用的库）")
    ap.add_argument("--json", default=None, help="把结果额外写入 JSON 文件")
    args = ap.parse_args()

    port = _free_port()
    store = (RedisStore(url=args.redis_url) if args.redis_url
             else InMemoryStore(limit=max(500, args.runs * 2 + 10)))
    app = create_app(
        provider_factory=lambda settings: LoadProvider(args.tool_rounds, args.delay, args.tool),
        settings=Settings(api_key="fake", model="fake-model", max_steps=args.max_steps),
        store=store,
        default_workspace=Path(args.workspace),
    )
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False,
    ))
    server.install_signal_handlers = lambda: None  # 在子线程里跑，别碰信号
    thread = threading.Thread(target=server.run, daemon=True, name="loadtest-uvicorn")
    thread.start()

    base = f"http://127.0.0.1:{port}"
    limits = httpx.Limits(max_connections=max(64, args.concurrency * 3),
                          max_keepalive_connections=max(32, args.concurrency))
    client = httpx.Client(base_url=base, timeout=httpx.Timeout(60.0), limits=limits)

    summary: dict[str, Any] = {
        "runs": args.runs,
        "concurrency": args.concurrency,
        "store": "RedisStore" if args.redis_url else "InMemoryStore",
        "tool": args.tool,
    }
    try:
        _wait_health(client)
        t0 = time.perf_counter()

        def create_one(i: int):
            started = time.perf_counter()
            resp = client.post("/runs", json={"task": f"压测任务 {i}", "max_steps": args.max_steps})
            return resp, (time.perf_counter() - started) * 1000

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            created = list(pool.map(create_one, range(args.runs)))
        responses = [r for r, _ in created]
        latencies = [ms for _, ms in created]
        good = [r for r in responses if r.status_code in (200, 202)]
        if not good:
            print("create 全部失败:", [(r.status_code, r.text[:200]) for r in responses[:3]])
            return 2
        run_ids = [r.json()["id"] for r in good]
        summary.update({
            "create_ok": len(good),
            "create_fail": len(responses) - len(good),
            "create_p50_ms": _pct(latencies, 0.50),
            "create_p95_ms": _pct(latencies, 0.95),
            "create_max_ms": max(latencies),
            "run_ids": run_ids,
        })

        sse_results: list[dict[str, Any]] = []
        sse_thread = None
        if not args.no_sse:
            def read_all():
                with ThreadPoolExecutor(max_workers=max(8, len(run_ids))) as pool:
                    return list(pool.map(lambda rid: _read_sse(client, rid), run_ids))
            sse_thread = threading.Thread(target=lambda: sse_results.extend(read_all()), daemon=True)
            sse_thread.start()

        bodies = _wait_runs(client, run_ids)
        wall_ms = (time.perf_counter() - t0) * 1000
        event_counts = [int(b.get("event_count", 0)) for b in bodies]
        summary.update({
            "done_ok": sum(1 for b in bodies if b.get("result", {}).get("ok")),
            "done_error": sum(1 for b in bodies if b["status"] == "error"),
            "wall_ms": wall_ms,
            "events_min": min(event_counts) if event_counts else 0,
            "events_max": max(event_counts) if event_counts else 0,
        })

        if sse_thread is not None:
            sse_thread.join(timeout=120)
            summary.update({
                "sse_streams": len(sse_results),
                "sse_ok": sum(1 for r in sse_results if r.get("ok")),
                "sse_events_total": sum(int(r.get("events", 0)) for r in sse_results),
                "sse_events_min": min((int(r.get("events", 0)) for r in sse_results), default=0),
                "sse_events_max": max((int(r.get("events", 0)) for r in sse_results), default=0),
                "sse_errors": [r for r in sse_results if not r.get("ok")][:5],
                "sse": True,
            })

        if not args.no_followups:
            follow_started = time.perf_counter()

            def followup(i: int):
                return client.post(f"/runs/{run_ids[i]}/followup",
                                   json={"text": f"继续压测 {i}", "max_steps": args.max_steps})

            with ThreadPoolExecutor(max_workers=min(args.concurrency, len(run_ids))) as pool:
                follow_resp = list(pool.map(followup, range(len(run_ids))))
            follow_ids = [r.json()["id"] for r in follow_resp if r.status_code == 202]
            follow_bodies = _wait_runs(client, follow_ids)
            follow_wall_ms = (time.perf_counter() - follow_started) * 1000
            convs = client.get(f"/conversations?limit={max(100, len(run_ids) * 2)}").json()["conversations"]
            summary.update({
                "followups": len(run_ids),
                "follow_ok": sum(1 for b in follow_bodies if b.get("result", {}).get("ok")),
                "follow_wall_ms": follow_wall_ms,
                "conversations": len(convs),
                "followups_flag": True,
            })

        _print_summary(summary)
        if args.json:
            Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"结果已写入 {args.json}")
        return 0
    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
