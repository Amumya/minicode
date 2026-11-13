#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩阈值调优：4000 太激进，试试更宽松的档位。

上一轮 A/B 发现：压缩省了 19% 输入 token，但有一跑没完成、步数变多。
猜测原因是"单次 4000 字符就落盘"太激进 —— 稍微大点的文件读一次就被换成路径，
模型要重读一遍。这个脚本比较三档：

    A 不压缩
    B 默认档   单次 4000 字符落盘、留最近 3 个
    C 宽松档   单次 12000 字符落盘、留最近 5 个

看的是：完成率、输入 token、步数。

用法：
    python eval/ab_compact_tuning.py --repeat 3
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

ARMS = {
    "A 不压缩": [],
    "B 默认档": ["--compact", "--compact-max-chars", "4000", "--compact-keep", "3"],
    "C 宽松档": ["--compact", "--compact-max-chars", "12000", "--compact-keep", "5"],
}


def run_once(extra: list[str], max_steps: int) -> dict:
    cmd = [str(PY), "-m", "minicode.cli", "run", TASK,
           "--files", "--max-steps", str(max_steps), "--save", "--json"] + extra
    started = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    elapsed = time.time() - started
    if proc.returncode not in (0, 1):
        return {"error": (proc.stderr or proc.stdout)[-200:], "elapsed_s": elapsed}
    try:
        payload = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return {"error": (proc.stdout or proc.stderr)[-200:], "elapsed_s": elapsed}

    saved = payload.get("saved_to")
    compactions, saved_chars = 0, 0
    if saved and Path(saved).is_file():
        data = json.loads(Path(saved).read_text(encoding="utf-8"))
        for ev in data.get("events", []):
            if ev["kind"] == "compact":
                compactions += 1
                saved_chars += ev["data"].get("saved_chars", 0)

    r = payload["result"]
    return {
        "ok": r["ok"],
        "steps": r["steps"],
        "in_tokens": r["usage"].get("input_tokens", 0),
        "out_tokens": r["usage"].get("output_tokens", 0),
        "compact_events": compactions,
        "saved_chars": saved_chars,
        "elapsed_s": round(elapsed, 1),
    }


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    return {
        "n": len(rows),
        "完成数": sum(1 for r in rows if r["ok"]),
        "平均输入token": round(sum(r["in_tokens"] for r in rows) / len(rows)),
        "平均步数": round(sum(r["steps"] for r in rows) / len(rows), 1),
        "压缩次数": sum(r["compact_events"] for r in rows),
        "省下字符": sum(r["saved_chars"] for r in rows),
        "平均耗时s": round(sum(r["elapsed_s"] for r in rows) / len(rows), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--max-steps", type=int, default=4)
    args = ap.parse_args()

    out: dict[str, dict] = {}
    for name, extra in ARMS.items():
        rows = []
        print(f"\n=== {name} ===")
        for i in range(1, args.repeat + 1):
            row = run_once(extra, args.max_steps)
            rows.append(row)
            if "error" in row:
                print(f"  #{i} 出错: {row['error'][:120]}")
                continue
            print(f"  #{i} ok={row['ok']} 步数={row['steps']} 入token={row['in_tokens']} "
                  f"压缩={row['compact_events']}次 {row['elapsed_s']}s")
        out[name] = summarize([r for r in rows if "error" not in r])

    print("\n" + "=" * 62)
    keys = ["n", "完成数", "平均输入token", "平均步数", "压缩次数", "省下字符", "平均耗时s"]
    heads = list(ARMS)
    print(f"{'指标':<14}" + "".join(f"{h:>14}" for h in heads))
    print("-" * (14 + 14 * len(heads)))
    for k in keys:
        print(f"{k:<14}" + "".join(f"{str(out[h].get(k)):>14}" for h in heads))

    dest = ROOT / "eval" / "runs" / f"ab_compact_tuning_{int(time.time())}.json"
    dest.write_text(json.dumps({"task": TASK, "arms": out}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n结果写入 {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
