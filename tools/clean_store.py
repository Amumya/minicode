#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 store 里的测试脏数据。

为什么需要这个脚本：
    测试曾经在**没有隔离环境变量**的情况下跑，`create_app()` 回落到真实 Redis，
    把 model=fake-model、workspace 指向 pytest 临时目录的假 run 写了进去。
    界面上这些记录会混在真实历史里点开就报错（模型名不存在）。

用法：
    python tools/clean_store.py --dry-run     # 先看会删什么
    python tools/clean_store.py               # 真删
    python tools/clean_store.py --all         # 全删（重置 store）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minicode.service.store import InMemoryStore, RedisStore, SessionStore  # noqa: E402

# 测试里用的假模型名
JUNK_MODELS = {"fake-model", "m", "fake", "stub"}

# 临时目录特征
TEMP_MARKERS = ("pytest-of-", "/tmp/", "appdata/local/temp", "/temp/", "td-", "pytest-")


def is_junk(run) -> bool:
    """这条 run 是不是测试写进来的。"""
    model = (run.model or "").strip().lower()
    if model in JUNK_MODELS:
        return True
    ws = (run.workspace or "").lower().replace("\\", "/")
    return any(marker in ws for marker in TEMP_MARKERS)


def main() -> int:
    ap = argparse.ArgumentParser(description="清理测试脏数据")
    ap.add_argument("--dry-run", action="store_true", help="只列出来，不删")
    ap.add_argument("--all", action="store_true", help="把所有 run 都删掉")
    args = ap.parse_args()

    url = os.environ.get("MINICODE_REDIS_URL", "redis://127.0.0.1:6380/0").strip()
    store: SessionStore
    if url:
        store = RedisStore(url)
        print(f"store: RedisStore({url})")
    else:
        store = InMemoryStore()
        print("store: InMemoryStore（进程内，删完就没了）")

    runs = store.all()
    targets = runs if args.all else [r for r in runs if is_junk(r)]
    print(f"共 {len(runs)} 个 run，本次要删 {len(targets)} 个" + ("（--all）" if args.all else "（测试脏数据）"))
    for run in targets:
        print(f"  - {run.id}  model={run.model!r}  {run.task[:40]!r}")
    if not targets:
        return 0
    if args.dry_run:
        print("\n（--dry-run：没有真的删）")
        return 0

    deleted = 0
    for run in targets:
        if store.delete(run.id):
            deleted += 1
    print(f"\n✅ 已删除 {deleted} 个 run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
