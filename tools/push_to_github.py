#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当前工作区上传到 GitHub —— 走 REST API，不依赖 git 协议。

为什么需要这个脚本：
    本机 `git` 的 HTTPS 协议到 github.com 会被 reset（2026-11-13 实测），
    但 `api.github.com` 可用。所以改用 Git Data API：
        文件 → blob → tree → commit → 更新 ref
    一次调用产生**一个**干净提交，而不是每个文件一个提交。

用法：
    python tools/push_to_github.py --dry-run          # 只看会上传哪些文件
    python tools/push_to_github.py                    # 创建/更新仓库并上传
    python tools/push_to_github.py --private          # 建私有仓库
    python tools/push_to_github.py -m "阶段 B: 抽出包"  # 指定提交信息

Token 来源（按顺序）：
    1) 环境变量 GITHUB_TOKEN
    2) 仓库根目录的 .github_token.txt（已被 .gitignore 忽略）
    3) 仓库上一级目录的 .github_token.txt
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
DEFAULT_REPO = "minicode"
ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 基础


def find_token() -> str:
    import os

    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    for cand in (ROOT / ".github_token.txt", ROOT.parent / ".github_token.txt"):
        if cand.is_file():
            return cand.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "找不到 token：请设置环境变量 GITHUB_TOKEN，"
        "或把 token 放到仓库根目录的 .github_token.txt"
    )


class Client:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, path: str, payload: dict | None = None,
             allow_404: bool = False, allow_409: bool = False):
        url = path if path.startswith("http") else API + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "minicode-push")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            # 409 = 仓库为空（没有 ref / 对象库），语义上等价于"没有"。
            # 实测：GitHub 对空仓库返回 409 而不是 404，两者都要能容忍。
            if (e.code == 404 and allow_404) or (e.code == 409 and allow_409):
                return None
            raise SystemExit(f"{method} {path} -> HTTP {e.code}\n{body[:500]}") from None


# ------------------------------------------------------------------ 收集文件


def collect_files() -> list[tuple[str, bytes]]:
    """用 git 自己的 ignore 规则决定哪些文件进仓库。

    做法：git init（如果还没有）+ git add -A，然后用 git ls-files 列出被跟踪的路径。
    这样 .gitignore 的行为与真实提交完全一致，不用重写一套匹配规则。
    """
    if not (ROOT / ".git").exists():
        subprocess.run(["git", "init", "-b", "main"], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL)
    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)

    out = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True,
                         capture_output=True, text=True, encoding="utf-8").stdout
    files: list[tuple[str, bytes]] = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        p = ROOT / rel
        if p.is_file():
            files.append((rel, p.read_bytes()))
    return sorted(files, key=lambda x: x[0])


def as_blob_payload(raw: bytes) -> dict:
    """文本走 utf-8，二进制走 base64。"""
    try:
        return {"content": raw.decode("utf-8"), "encoding": "utf-8"}
    except UnicodeDecodeError:
        return {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}


# ------------------------------------------------------------------ 主流程


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO, help="仓库名")
    ap.add_argument("-m", "--message", default="", help="提交信息")
    ap.add_argument("--private", action="store_true", help="建私有仓库")
    ap.add_argument("--dry-run", action="store_true", help="只列出文件，不调用 API")
    ap.add_argument("--description", default=None)
    args = ap.parse_args()

    files = collect_files()
    total = sum(len(b) for _, b in files)
    print(f"待上传 {len(files)} 个文件，共 {total/1024:.1f} KB")
    for rel, raw in files:
        print(f"  {len(raw):>8} B  {rel}")

    if args.dry_run:
        print("\n--dry-run：未调用 GitHub API")
        return 0

    cli = Client(find_token())
    me = cli.call("GET", "/user")
    owner = me["login"]
    print(f"\n账号: {owner}")

    # 1) 仓库：存在就复用，不存在就创建
    repo_path = f"/repos/{owner}/{args.repo}"
    repo = cli.call("GET", repo_path, allow_404=True)
    if repo is None:
        desc = args.description or "A coding agent harness focused on governance and verifiable evaluation."
        repo = cli.call("POST", "/user/repos", {
            "name": args.repo,
            "private": bool(args.private),
            "description": desc,
            "auto_init": False,
            "has_issues": True,
            "has_wiki": False,
        })
        print(f"✅ 已创建仓库: {repo['html_url']}")
        empty = True
    else:
        print(f"✅ 仓库已存在: {repo['html_url']}")
        empty = repo["size"] == 0

    # 2) 完全空的仓库要先落地第一个提交。
    #    实测：对没有任何提交的仓库，POST /git/blobs 会返回 409 "Git Repository is empty"。
    #    所以先用 Contents API 写一个 README 把对象库建起来，再走正常的 tree/commit 流程。
    ref = cli.call("GET", f"{repo_path}/git/ref/heads/main",
                   allow_404=True, allow_409=True)
    if ref is None:
        seed = ROOT / "README.md"
        seed_bytes = seed.read_bytes() if seed.is_file() else b"# minicode\n"
        cli.call("PUT", f"{repo_path}/contents/README.md", {
            "message": "chore: initialize repository",
            "content": base64.b64encode(seed_bytes).decode("ascii"),
        })
        print("✅ 已创建初始提交（空仓库 bootstrap）")
        ref = cli.call("GET", f"{repo_path}/git/ref/heads/main")

    # 3) 上传 blob
    tree_items = []
    for rel, raw in files:
        blob = cli.call("POST", f"{repo_path}/git/blobs", as_blob_payload(raw))
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    print(f"✅ 已上传 {len(tree_items)} 个 blob")

    # 4) 建 tree
    tree_payload: dict = {"tree": tree_items}
    parent_sha = None
    if ref:
        parent_sha = ref["object"]["sha"]
        # 已有提交：以现有 tree 为基底覆盖本次文件
        parent_commit = cli.call("GET", f"{repo_path}/git/commits/{parent_sha}")
        tree_payload["base_tree"] = parent_commit["tree"]["sha"]
    tree = cli.call("POST", f"{repo_path}/git/trees", tree_payload)
    print(f"✅ 已建 tree {tree['sha'][:10]}")

    # 5) 建 commit
    msg = args.message or "chore: sync from local workspace"
    commit_payload = {"message": msg, "tree": tree["sha"]}
    if parent_sha:
        commit_payload["parents"] = [parent_sha]
    commit = cli.call("POST", f"{repo_path}/git/commits", commit_payload)
    print(f"✅ 已建 commit {commit['sha'][:10]}  “{msg}”")

    # 6) 移动 ref
    if parent_sha:
        cli.call("PATCH", f"{repo_path}/git/refs/heads/main",
                 {"sha": commit["sha"], "force": True})
    else:
        cli.call("POST", f"{repo_path}/git/refs",
                 {"ref": "refs/heads/main", "sha": commit["sha"]})
    print("✅ 已更新 refs/heads/main")

    print(f"\n仓库地址: {repo['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
