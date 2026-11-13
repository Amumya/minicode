#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看一眼消融实验里 agent 到底做了什么。"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]

condition = sys.argv[1] if len(sys.argv) > 1 else "bash"
files = sorted((ROOT / "eval" / "runs").glob(f"sandbox_{condition}_*.json"))
if not files:
    print(f"没有 {condition} 的结果")
    raise SystemExit(1)

data = json.loads(files[-1].read_text(encoding="utf-8"))
print(f"文件: {files[-1].name}")
print(f"汇总: {data['summary']}\n")

for row in data["rows"]:
    mark = "✅" if row["passed"] else "❌"
    print(f"{mark} {row['task']} #{row['repeat']}  passed={row['passed']}  "
          f"steps={row['steps']}  ok={row['ok']}  拒绝={row['denied']}")
    for i, c in enumerate(row["commands"], 1):
        cmd = c.get("command") or c.get("path") or ""
        print(f"     {i}. {cmd[:120]}")
    for flag in row["cheat_flags"]:
        print(f"     ⚠️ {flag}")
    print()
