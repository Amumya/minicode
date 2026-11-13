#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑一个真实任务，把轨迹存下来。

这是阶段 B 的验收用脚本：证明主循环能跑通、事件能落盘、指标能算出来。
正式的命令行入口在阶段 C 做。

用法：
    python tools/run_task.py "数一下仓库里有几个 py 文件"
    python tools/run_task.py --task-file task.txt --model deepseek-v4-pro
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minicode.config import Settings  # noqa: E402
from minicode.events import Recorder  # noqa: E402
from minicode.loop import run  # noqa: E402
from minicode.provider import AnthropicProvider  # noqa: E402
from minicode.tools import default_registry  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", default="")
    ap.add_argument("--task-file", type=Path, default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--workspace", type=Path, default=ROOT)
    ap.add_argument("--save", action="store_true", help="写进 eval/runs/")
    args = ap.parse_args()

    task = args.task
    if args.task_file:
        task = args.task_file.read_text(encoding="utf-8").strip()
    if not task:
        print("没有任务。用法: python tools/run_task.py \"任务内容\"")
        return 2

    settings = Settings.from_env(ROOT / ".env")
    if args.model:
        settings = Settings(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=args.model,
            max_steps=settings.max_steps,
        )
    if args.max_steps:
        settings = Settings(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            max_steps=args.max_steps,
        )

    provider = AnthropicProvider(settings)
    rec = Recorder()
    started = time.time()
    result = run(
        task,
        provider=provider,
        tools=default_registry(),
        settings=settings,
        workspace=args.workspace,
        recorder=rec,
    )
    elapsed = time.time() - started

    print(f"任务: {task}")
    print(f"模型: {settings.model}")
    print(f"结果: {'完成' if result.ok else '未完成'}  步数 {result.steps}  "
          f"stop_reason={result.stop_reason}")
    print(f"工具调用 {result.tool_calls} 次，被拒 {result.denied} 次")
    print(f"token: 输入 {result.usage.get('input_tokens')}  "
          f"输出 {result.usage.get('output_tokens')}   耗时 {elapsed:.1f}s")
    print("\n--- 事件 ---")
    for e in rec.events:
        brief = {k: v for k, v in e.data.items() if k in
                 ("step", "tool", "exit_code", "failed", "reason", "stop_reason")}
        print(f"  {e.kind:<12} {brief}")
    print(f"\n--- 最终回答 ---\n{result.final_text}")

    if args.save:
        stamp = int(time.time())
        out = ROOT / "eval" / "runs" / f"task_{stamp}.json"
        rec.dump(out, extra={
            "task": task,
            "model": settings.model,
            "elapsed_s": round(elapsed, 2),
            "result": result.to_dict(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        print(f"\n轨迹已写入 {out.relative_to(ROOT)}")
        print(json.dumps(result.to_dict(), ensure_ascii=False))

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
