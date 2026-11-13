#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照实验：上下文压缩到底省了多少。

任务故意选"读一个很大的文件"：一次工具调用就能把几万字符塞进上下文，
如果每轮都重发一遍，成本是步数 × 文件大小。

两组：
    A 不压缩
    B --compact

看的是**输入 token**（这是压缩直接作用的量）和工具输出落盘次数。

用法：
    python eval/ab_compact.py --repeat 2
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"

TASK = (
    "读一下 docs/取舍与证据.md，用一句话告诉我它一共记了多少条决策，"
    "再读一下 docs/环境实测.md，用一句话说它主要讲什么。不要改任何文件。"
)


def run_once(extra: list[str], max_steps: int) -> dict:
    cmd = [str(PY), "-m", "minicode.cli", "run", TASK,
           "--files", "--max-steps", str(max_steps), "--save", "--json"] + extra
    started = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    elapsed = time.time() - started
    if proc.returncode not in (0, 1):
        return {"error": (proc.stderr or proc.stdout)[-300:], "elapsed_s": elapsed}
    try:
        payload = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return {"error": (proc.stdout or proc.stderr)[-300:], "elapsed_s": elapsed}

    saved = payload.get("saved_to")
    compactions = []
    tool_chars = 0
    if saved and Path(saved).is_file():
        data = json.loads(Path(saved).read_text(encoding="utf-8"))
        for ev in data.get("events", []):
            if ev["kind"] == "compact":
                compactions.append(ev["data"])
            if ev["kind"] == "tool_call":
                tool_chars += len(str(ev["data"].get("args", {})))

    result = payload["result"]
    return {
        "ok": result["ok"],
        "steps": result["steps"],
        "in_tokens": result["usage"].get("input_tokens", 0),
        "out_tokens": result["usage"].get("output_tokens", 0),
        "compact_events": len(compactions),
        "saved_chars": sum(c.get("saved_chars", 0) for c in compactions),
        "elapsed_s": round(elapsed, 1),
    }


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    return {
        "n": len(rows),
        "完成数": sum(1 for r in rows if r["ok"]),
        "平均输入token": round(sum(r["in_tokens"] for r in rows) / len(rows)),
        "平均输出token": round(sum(r["out_tokens"] for r in rows) / len(rows)),
        "平均步数": round(sum(r["steps"] for r in rows) / len(rows), 1),
        "压缩触发次数": sum(r["compact_events"] for r in rows),
        "压缩省下字符": sum(r["saved_chars"] for r in rows),
        "平均耗时s": round(sum(r["elapsed_s"] for r in rows) / len(rows), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--max-steps", type=int, default=4)
    args = ap.parse_args()

    arms = {"A 不压缩": [], "B 压缩": ["--compact"]}
    out: dict[str, dict] = {}
    for name, extra in arms.items():
        rows = []
        print(f"\n=== {name} ===")
        for i in range(1, args.repeat + 1):
            row = run_once(extra, args.max_steps)
            rows.append(row)
            if "error" in row:
                print(f"  #{i} 出错: {row['error'][:160]}")
                continue
            print(f"  #{i} ok={row['ok']} 步数={row['steps']} 入token={row['in_tokens']} "
                  f"压缩={row['compact_events']}次 省{row['saved_chars']}字符 "
                  f"{row['elapsed_s']}s")
        out[name] = summarize([r for r in rows if "error" not in r])

    print("\n" + "=" * 56)
    keys = ["n", "完成数", "平均输入token", "平均输出token", "平均步数",
            "压缩触发次数", "压缩省下字符", "平均耗时s"]
    print(f"{'指标':<16}{'A 不压缩':>12}{'B 压缩':>12}")
    print("-" * 40)
    for k in keys:
        print(f"{k:<16}{str(out['A 不压缩'].get(k)):>12}{str(out['B 压缩'].get(k)):>12}")

    dest = ROOT / "eval" / "runs" / f"ab_compact_{int(time.time())}.json"
    dest.write_text(json.dumps({"task": TASK, "arms": out}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n结果写入 {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
