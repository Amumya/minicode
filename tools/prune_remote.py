#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把远端仓库裁成"和本地完全一致"。

为什么需要这个：`tools/push_to_github.py` 走 `base_tree` 增量更新，
**只能覆盖和新增，不能删除**。所以每次本地删了文件（比如换了前端构建产物），
远端都会留下旧的那份。代理正常情况下用 `git push` 就没这个问题。

做法：读远端最新的 tree，只保留本地 `git ls-files` 里也有的路径，
拼新 tree 提交上去。**不重新上传任何文件**（复用已有 blob）——
本质上是"删掉远端多出来的路径"。

用法：
    python tools/prune_remote.py            # 删掉远端多余的文件
    python tools/prune_remote.py --dry-run  # 只看会删什么
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from push_to_github import ROOT, Client, find_token  # noqa: E402

OWNER = "Amumya"
REPO = "minicode"
MESSAGE = "清理远端残留文件（API 推送不会删文件，这里补上）"


def local_paths() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True,
                         capture_output=True, text=True, encoding="utf-8").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cli = Client(find_token())
    repo = f"/repos/{OWNER}/{REPO}"

    ref = cli.call("GET", f"{repo}/git/ref/heads/main")
    parent = ref["object"]["sha"]
    commit = cli.call("GET", f"{repo}/git/commits/{parent}")
    tree = cli.call("GET", f"{repo}/git/trees/{commit['tree']['sha']}?recursive=1")

    keep = local_paths()
    kept, dropped = [], []
    for item in tree["tree"]:
        if item["type"] != "blob":
            continue
        if item["path"] in keep:
            kept.append({"path": item["path"], "mode": item["mode"],
                         "type": "blob", "sha": item["sha"]})
        else:
            dropped.append(item["path"])

    if not dropped:
        print("远端和本地一致，不用动")
        return 0

    print(f"  保留 {len(kept)} 个，删除 {len(dropped)} 个：")
    for path in dropped:
        print(f"    - {path}")
    if args.dry_run:
        print("\n（--dry-run：没有真的提交）")
        return 0

    new_tree = cli.call("POST", f"{repo}/git/trees", {"tree": kept})
    new_commit = cli.call("POST", f"{repo}/git/commits", {
        "message": MESSAGE,
        "tree": new_tree["sha"],
        "parents": [parent],
    })
    cli.call("PATCH", f"{repo}/git/refs/heads/main",
             {"sha": new_commit["sha"], "force": True})
    print(f"\n✅ 已提交 {new_commit['sha'][:10]}：{MESSAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
