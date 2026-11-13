#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查沙箱任务本身是否自洽 —— 不跑模型，几秒钟。

判据只有一条硬性的：
    **起始代码下，隐藏测试必须至少挂一条** —— 否则没有要修的东西，白送分。

可见测试的状态只做记录，不做要求：
    t1_counter 这种"修失败的测试"的任务，起始代码就是红的，那是对的。
    只看可见测试过没过会误判。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from sandbox_run import SANDBOX, prepare, run_hidden  # noqa: E402

PY = ROOT / ".venv" / "Scripts" / "python.exe" if (ROOT / ".venv").exists() else Path(sys.executable)


def run_visible(ws: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [str(PY), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests"],
        cwd=str(ws), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = ((proc.stdout or "").strip().splitlines() or [""])[-1]
    return proc.returncode == 0, tail[-90:]


def main() -> int:
    rows = []
    for task_dir in sorted(p for p in SANDBOX.iterdir() if p.is_dir()):
        name = task_dir.name
        ws = prepare(name)
        try:
            vis_ok, vis_note = run_visible(ws)
            hid_ok, hid_note = run_hidden(ws, name)
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        good = not hid_ok
        rows.append((name, vis_ok, vis_note, hid_ok, hid_note, good))

    print(f"{'任务':<14}{'可见测试':<10}{'隐藏测试':<10}{'结论'}")
    print("-" * 66)
    for name, vis_ok, vis_note, hid_ok, hid_note, good in rows:
        print(f"{name:<14}{'过' if vis_ok else '挂':<10}{'过' if hid_ok else '挂':<10}"
              f"{'✅ 自洽' if good else '❌ 不自洽'}")
        if not vis_ok:
            print(f"    （起始代码可见测试是红的：{vis_note}）")
        if hid_ok:
            print(f"    隐藏测试一开始就过：{hid_note}")

    bad = [r[0] for r in rows if not r[5]]
    print()
    if bad:
        print(f"❌ 有 {len(bad)} 个任务不自洽：{', '.join(bad)}")
        return 1
    print(f"✅ {len(rows)} 个任务全部自洽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
