#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""带重试的提交并推送。

为什么需要它：
    这台机器上到 GitHub 的 HTTPS 要走本地代理，而代理的 TLS 握手是随机失败的
    （实测 fetch / push 大约三次里成功一次，报 `SSL_ERROR_SYSCALL` 或者
    `schannel: failed to receive handshake`）。
    git 本身不重试，失败一次就退出，所以每次推代码都得手动重来。

用法：
    python tools/git_push.py -m "改了什么"
    python tools/git_push.py -m "改了什么" --retries 15
    python tools/git_push.py --push-only        # 只推，不提交
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def git(*args: str) -> tuple[int, str]:
    return run(["git", *args])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", default="", help="提交信息")
    ap.add_argument("--retries", type=int, default=10)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--push-only", action="store_true", help="跳过 add/commit")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    if not args.push_only:
        code, out = git("add", "-A")
        if code != 0:
            print(out)
            return code

        code, out = git("status", "--short")
        if not out.strip():
            print("没有改动，直接推送")
        else:
            if not args.message:
                print("有改动但没给 -m 提交信息")
                return 2
            code, out = git("commit", "-m", args.message)
            print(out.strip().splitlines()[0] if out.strip() else "")
            if code != 0:
                print(out)
                return code

    for i in range(1, args.retries + 1):
        code, out = git("push", "origin", args.branch)
        if code == 0:
            tail = [ln for ln in out.strip().splitlines() if ln.strip()][-2:]
            print(f"✅ 第 {i} 次推送成功")
            for line in tail:
                print(f"   {line}")
            return 0
        last = out.strip().splitlines()[-1] if out.strip() else "(无输出)"
        print(f"✗ 第 {i} 次失败: {last[:110]}")
        if i < args.retries:
            time.sleep(args.delay)

    print(f"❌ 试了 {args.retries} 次都没成功")
    return 1


if __name__ == "__main__":
    sys.exit(main())
