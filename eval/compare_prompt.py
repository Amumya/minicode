#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比"有没有告诉模型当前是 Windows/PowerShell"的效果。

变量只有一个：系统提示词里有没有那段环境说明。
工具、任务、模型、步数上限都一样。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]


def latest(tag: str) -> dict | None:
    files = sorted((ROOT / "eval" / "runs").glob(f"sandbox_bash_{tag}_*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def stats(data: dict) -> dict:
    rows = data["rows"]
    calls = sum(r["tool_calls"] for r in rows)
    failed = sum(r["failed_tools"] for r in rows)
    kinds: dict[str, int] = {}
    for r in rows:
        for k, v in r.get("failure_kinds", {}).items():
            kinds[k] = kinds.get(k, 0) + v
    return {
        "kinds": kinds,
        "runs": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "calls": calls,
        "failed": failed,
        "fail_rate": round(100.0 * failed / max(calls, 1), 1),
        "avg_steps": round(sum(r["steps"] for r in rows) / len(rows), 2),
        "avg_out": round(
            sum(r["usage"].get("output_tokens", 0) for r in rows) / len(rows), 1),
        "denied": sum(r["denied"] for r in rows),
    }


def main() -> int:
    a, b = latest("nohint"), latest("hint")
    if not a or not b:
        print("缺结果，先把两种都跑一遍：")
        print("  eval/sandbox_run.py --condition bash --no-shell-hint --repeat 2")
        print("  eval/sandbox_run.py --condition bash --shell-hint    --repeat 2")
        return 1

    sa, sb = stats(a), stats(b)
    label_a, label_b = "不给环境说明", "给出环境说明"

    print(f"A {label_a}  {a['created_at']}")
    print(f"B {label_b}  {b['created_at']}")
    print(f"模型 {a['model']}，任务数 {sa['runs']} 次运行\n")

    rows = [
        ("通过", f"{sa['passed']}/{sa['runs']}", f"{sb['passed']}/{sb['runs']}"),
        ("工具调用", sa["calls"], sb["calls"]),
        ("其中失败", sa["failed"], sb["failed"]),
        ("失败率", f"{sa['fail_rate']}%", f"{sb['fail_rate']}%"),
        ("  └ 测试红", sa["kinds"].get("test_red", 0), sb["kinds"].get("test_red", 0)),
        ("  └ shell 不匹配", sa["kinds"].get("shell_mismatch", 0), sb["kinds"].get("shell_mismatch", 0)),
        ("  └ 其它", sa["kinds"].get("other", 0), sb["kinds"].get("other", 0)),
        ("平均步数", sa["avg_steps"], sb["avg_steps"]),
        ("平均出 token", sa["avg_out"], sb["avg_out"]),
        ("被拒次数", sa["denied"], sb["denied"]),
    ]
    print(f"{'':<14}{'A ' + label_a:>16}{'B ' + label_b:>16}")
    print("-" * 46)
    for name, va, vb in rows:
        print(f"{name:<14}{str(va):>16}{str(vb):>16}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
