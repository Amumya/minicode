#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比两个条件的消融结果，按任务拆开看。

只看汇总会漏掉一件事：文件工具在有的任务上省事，在有的任务上反而更绕。
这道分任务看。

用法：
    python eval/compare_conditions.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]


def latest(condition: str) -> dict | None:
    files = [f for f in sorted((ROOT / "eval" / "runs").glob(f"sandbox_{condition}_*.json"))
             if "hint" not in f.name]
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def by_task(data: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for row in data["rows"]:
        out[row["task"]].append(row)
    return dict(out)


def avg(rows, key):
    vals = [r[key] if key in ("steps",) else r["usage"].get("output_tokens", 0) for r in rows]
    return sum(vals) / len(vals) if vals else 0.0


def main() -> int:
    a, b = latest("bash"), latest("files")
    if not a or not b:
        print("缺结果文件，先把两个条件都跑一遍")
        return 1

    print(f"A 条件（只有 bash）  {a['created_at']}  模型 {a['model']}")
    print(f"B 条件（bash+文件）  {b['created_at']}  模型 {b['model']}")
    print()

    ba, bb = by_task(a), by_task(b)
    tasks = sorted(set(ba) | set(bb))

    print(f"{'任务':<14}{'A通过':>7}{'B通过':>7}{'A步数':>8}{'B步数':>8}"
          f"{'A出token':>10}{'B出token':>10}")
    print("-" * 66)

    tot = defaultdict(float)
    for t in tasks:
        ra, rb = ba.get(t, []), bb.get(t, [])
        pa = f"{sum(1 for r in ra if r['passed'])}/{len(ra)}"
        pb = f"{sum(1 for r in rb if r['passed'])}/{len(rb)}"
        sa, sb = avg(ra, "steps"), avg(rb, "steps")
        ta, tb = avg(ra, "out"), avg(rb, "out")
        print(f"{t:<14}{pa:>7}{pb:>7}{sa:>8.1f}{sb:>8.1f}{ta:>10.0f}{tb:>10.0f}")
        tot["sa"] += sa
        tot["sb"] += sb
        tot["ta"] += ta
        tot["tb"] += tb
        tot["n"] += 1

    n = max(tot["n"], 1)
    print("-" * 66)
    print(f"{'平均':<14}{'':>7}{'':>7}{tot['sa']/n:>8.1f}{tot['sb']/n:>8.1f}"
          f"{tot['ta']/n:>10.0f}{tot['tb']/n:>10.0f}")

    print()
    for label, data in (("A 只有 bash", a), ("B 有文件工具", b)):
        rows = data["rows"]
        fl = [r for r in rows if r["cheat_flags"]]
        fail = sum(r["failed_tools"] for r in rows)
        calls = sum(r["tool_calls"] for r in rows)
        print(f"{label}: 工具调用 {calls} 次，其中失败 {fail} 次"
              f"（{100.0*fail/max(calls,1):.0f}%），带作弊标记 {len(fl)}/{len(rows)}")
        for r in fl:
            print(f"    {r['task']} #{r['repeat']}: {r['cheat_flags']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
