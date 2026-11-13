#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看某一类失败到底长什么样。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]

kind = sys.argv[1] if len(sys.argv) > 1 else "other"

for tag in ("nohint", "hint"):
    files = sorted((ROOT / "eval" / "runs").glob(f"sandbox_bash_{tag}_*.json"))
    if not files:
        continue
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"=== {tag} ({files[-1].name}) ===")
    n = 0
    for row in data["rows"]:
        for c in row.get("calls", []):
            if c.get("kind") != kind:
                continue
            n += 1
            print(f"  [{row['task']}] exit={c['exit_code']}  {c['command'][:100]}")
    if n == 0:
        print("  （这一类没有）")
    print()
