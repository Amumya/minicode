#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照实验：把"该用哪个 Python 解释器"告诉 agent，值不值。

背景：实测发现，不说的话 agent 会用系统 python 跑 pytest，失败后再花好几步
去找装了 pytest 的那个解释器（轨迹里能看到它试错相对路径）。

两组：
    A 不说     --no-python-hint
    B 说明     （默认）

同一任务、同一模型、同样步数上限，只改"系统提示词里有没有解释器路径"。

用法：
    python eval/ab_python_hint.py --repeat 3
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"

# 这个任务专门测"环境发现"：它必须跑一次测试，而跑测试需要装了 pytest 的解释器。
# 不告诉它解释器在哪，它就得自己试错找。
TASK = (
    "跑一下 eval/sandbox/t1_counter 目录里的测试，告诉我通过还是失败。"
    "不要改任何文件。"
)


def run_once(extra: list[str], max_steps: int) -> dict:
    cmd = [str(PY), "-m", "minicode.cli", "run", TASK,
           "--max-steps", str(max_steps), "--save", "--json"]
    cmd += extra
    started = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    elapsed = time.time() - started
    if proc.returncode not in (0, 1):
        return {"error": (proc.stderr or proc.stdout)[-300:], "elapsed_s": elapsed}

    try:
        payload = json.loads(proc.stdout.strip())
    except (json.JSONDecodeError, IndexError):
        return {"error": (proc.stdout or proc.stderr)[-300:], "elapsed_s": elapsed}

    saved = payload.get("saved_to")
    kinds: list[str] = []
    tools: list[str] = []
    failed_tools = 0
    if saved and Path(saved).is_file():
        data = json.loads(Path(saved).read_text(encoding="utf-8"))
        for ev in data.get("events", []):
            kinds.append(ev["kind"])
            if ev["kind"] == "tool_call":
                tools.append(ev["data"].get("tool", ""))
                if ev["data"].get("failed"):
                    failed_tools += 1

    result = payload["result"]
    return {
        "ok": result["ok"],
        "steps": result["steps"],
        "stop": result["stop_reason"],
        "out_tokens": result["usage"].get("output_tokens", 0),
        "in_tokens": result["usage"].get("input_tokens", 0),
        "tool_calls": len(tools),
        "failed_tools": failed_tools,
        "used_load_skill": "load_skill" in tools,
        "used_todo": "todo_write" in tools,
        "elapsed_s": round(elapsed, 1),
    }


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("ok")]
    return {
        "n": len(rows),
        "完成数": len(ok),
        "平均步数": round(sum(r["steps"] for r in rows) / len(rows), 1),
        "平均出token": round(sum(r["out_tokens"] for r in rows) / len(rows)),
        "工具失败总数": sum(r["failed_tools"] for r in rows),
        "用了 load_skill": sum(1 for r in rows if r.get("used_load_skill")),
        "用了 todo_write": sum(1 for r in rows if r.get("used_todo")),
        "平均耗时s": round(sum(r["elapsed_s"] for r in rows) / len(rows), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--max-steps", type=int, default=6)
    args = ap.parse_args()

    arms = {
        "A 不说解释器": ["--no-python-hint"],
        "B 说明解释器": [],
    }
    out: dict[str, dict] = {}
    for name, extra in arms.items():
        rows = []
        print(f"\n=== {name} ===")
        for i in range(1, args.repeat + 1):
            row = run_once(extra, args.max_steps)
            rows.append(row)
            if "error" in row:
                print(f"  #{i} 出错: {row['error'][:120]}")
                continue
            print(f"  #{i} ok={row['ok']} 步数={row['steps']} 出token={row['out_tokens']} "
                  f"工具失败={row['failed_tools']} load_skill={row['used_load_skill']} "
                  f"todo={row['used_todo']} {row['elapsed_s']}s")
        out[name] = summarize([r for r in rows if "error" not in r])

    print("\n" + "=" * 60)
    keys = ["n", "完成数", "平均步数", "平均出token", "工具失败总数", "用了 load_skill",
            "用了 todo_write", "平均耗时s"]
    print(f"{'指标':<16}{'A 不说':>12}{'B 说明':>12}")
    print("-" * 40)
    for k in keys:
        print(f"{k:<16}{str(out['A 不说解释器'][k]):>12}{str(out['B 说明解释器'][k]):>12}")

    stamp = int(time.time())
    dest = ROOT / "eval" / "runs" / f"ab_python_hint_{stamp}.json"
    dest.write_text(json.dumps({"task": TASK, "arms": out}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n结果写入 {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
