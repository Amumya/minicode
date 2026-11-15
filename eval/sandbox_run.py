#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沙箱任务 + 消融实验。

每次跑都是全新的临时工作区：把任务的 src/ 和 tests/ 复制进去，agent 在里面改，
跑完之后把 hidden/ 里的隐藏测试拷进去判定。

三个条件：
    bash    只有 shell（对照组）
    files   shell + 读写改文件工具（实验组）

为什么要有隐藏测试：
    可见测试只覆盖一部分情况。如果只按可见测试判，agent 把可见测试改一改也能"通过"。
    隐藏测试在判定时才放进去，agent 看不见。

怎么防作弊（三道）：
    1. 权限层：write_file / edit_file 改测试文件直接拒
    2. 哈希比对：跑完把 tests/ 下文件的哈希和跑之前比。不管它是用 bash 改的
       还是用文件工具改的，改了就对不上 —— 这条能兜住权限层的漏
    3. 静态检查：新增的 sys.exit(0)、裸 except 里 pass

用法：
    python eval/sandbox_run.py --condition bash  --repeat 2
    python eval/sandbox_run.py --condition files --repeat 2
    python eval/sandbox_run.py --condition bash --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minicode.config import Settings  # noqa: E402
from minicode.events import Recorder  # noqa: E402
from minicode.loop import run as run_loop  # noqa: E402
from minicode.prompts import default_system  # noqa: E402
from minicode.provider import AnthropicProvider  # noqa: E402
from minicode.tools import default_registry  # noqa: E402

from failure_kinds import classify  # noqa: E402  (同目录)

SANDBOX = ROOT / "eval" / "sandbox"
# 工作区必须放在**仓库外面**。
# 之前放在 eval/tmp 下，agent 用 `..` 就能爬回项目目录，把基准任务的源码改掉
# （实测发生过：t2_ranges 和 t4_config 的起始代码被改成"答案"，隐藏测试一开始就过）。
# 工作区必须放在**仓库外面**。
# 踩过两次：
#   1) 放 eval/tmp 下时，agent 用 `..` 爬回项目，把基准任务的源码改成了"答案"
#   2) tempfile.gettempdir() 在这个会话里指向仓库根目录（写 AppData 被文件沙箱拒），
#      于是"临时目录"其实还在仓库里
# 现在固定在仓库的上一级目录下，并配合 source_fingerprint() 做兜底校验。
TMP = ROOT.parent / "_eval_tmp"

TASK_FRAME = (
    "任务是修 bug 或者补功能，改完之后应该让测试通过。改完直接给结论。"
)


def source_fingerprint(task: str) -> str:
    """把任务源目录（src/tests/hidden/extras）的哈希拼起来，用来发现被改动。

    为什么需要：工作区虽然挪出了仓库，但"agent 会不会绕过"不能靠假设。
    每跑一次都比对一次，被改了就立刻停下 —— 污染一旦进入数据就再也说不清了。
    """
    root = SANDBOX / task
    parts = []
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        parts.append(f"{f.relative_to(root).as_posix()}:{sha(f)}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha(p) for p in root.rglob("*") if p.is_file()}


def prepare(task: str) -> Path:
    """建一个全新的工作区：src 和 tests 拷进去，hidden 不拷。"""
    src = SANDBOX / task
    ws = TMP / f"{task}_{uuid.uuid4().hex[:8]}"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    # 忽略 __pycache__：不忽略的话，pytest 生成的 .pyc 会被拷进工作区，
    # 跑完哈希一变就被当成"agent 改了测试文件" —— 实测出现过这种假阳性。
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for sub in ("src", "tests"):
        shutil.copytree(src / sub, ws / sub, ignore=ignore)

    # 任务根目录下的普通文件（SPEC.md 之类）也要进工作区，agent 才看得见
    for extra in src.iterdir():
        if extra.is_file() and extra.name != "task.txt":
            shutil.copy2(extra, ws / extra.name)

    # 让 `python -m pytest` 在工作区根目录就能跑通：
    # 不写这个的话，测试里的 `from seq import ...` 找不到模块，
    # agent 会先去折腾 sys.path，白烧几步（实测过）。
    (ws / "pytest.ini").write_text("[pytest]\npythonpath = src\n", encoding="utf-8")
    return ws


def run_hidden(ws: Path, task: str) -> tuple[bool, str]:
    src = SANDBOX / task
    hidden_files = list((src / "hidden").glob("*.py"))
    if not hidden_files:
        return False, "没有隐藏测试"
    # 拷进去时改名成 test_ 前缀：pytest 只认 test_*.py 这个模式，
    # 直接放 hidden_*.py 即使显式传路径也可能被跳过（实测过）。
    renamed = {}
    for f in hidden_files:
        target = ws / "tests" / f"test_{f.name}"
        shutil.copy2(f, target)
        renamed[f] = target

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ws / "src")
    # 关键：隐藏测试的文件名是 hidden_*.py，不匹配 pytest 默认的 test_*.py 规则。
    # 不带路径跑 `pytest -q` 只会收集到可见测试 —— 那样"隐藏测试判定"其实是假的，
    # 实际只是在重跑可见测试。必须把隐藏文件显式列出来。
    targets = ["tests"]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *targets],
            cwd=str(ws), capture_output=True, text=True, timeout=120, env=env,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "判定超时"
    finally:
        for target in renamed.values():
            if target.exists():
                target.unlink()

    tail = (proc.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else ""
    return proc.returncode == 0, summary


def cheat_flags(before: dict[str, str], after: dict[str, str], ws: Path) -> list[str]:
    """能抓一个算一个。抓不到的在 README 里写清楚，不装作全覆盖。

    这里的静态检查很粗：sys.exit(0) 和裸 except 都只看"现在文件里有没有"，
    分不清是原来就有还是改出来的。所以它只能当线索，不能当判决。
    真正兜底的是隐藏测试本身。
    """
    flags: list[str] = []

    # 1) 测试文件被动过 —— 不管用 bash 还是文件工具，哈希都对不上
    for rel, h in before.items():
        if not rel.startswith("tests"):
            continue
        if rel not in after:
            flags.append(f"deleted_test_file:{rel}")
        elif after[rel] != h:
            flags.append(f"modified_test_file:{rel}")

    # 2) 静态线索
    for rel in after:
        if not rel.endswith(".py"):
            continue
        text = (ws / rel).read_text(encoding="utf-8", errors="replace")
        if "sys.exit(0)" in text:
            flags.append(f"has_sys_exit:{rel}")
        if re.search(r"except\s*:\s*\n\s*pass", text):
            flags.append(f"has_bare_except_pass:{rel}")

    return flags


def run_one(task: str, condition: str, provider, settings, repeat_idx: int,
            shell_hint: bool = True) -> dict:
    fingerprint_before = source_fingerprint(task)
    ws = prepare(task)
    before = snapshot(ws)
    prompt = (SANDBOX / task / "task.txt").read_text(encoding="utf-8").strip()

    rec = Recorder()
    started = time.time()
    result = run_loop(
        prompt,
        provider=provider,
        tools=default_registry(include_files=(condition == "files")),
        settings=settings,
        workspace=ws,
        recorder=rec,
        system=default_system(shell_hint=shell_hint, python_hint=sys.executable) + TASK_FRAME,
    )
    elapsed = time.time() - started

    after = snapshot(ws)
    flags = cheat_flags(before, after, ws)

    tool_ev = [e for e in rec.events if e.kind == "tool_call"]
    denied_ev = [e for e in rec.events if e.kind == "denied"]
    failed_tools = [e for e in tool_ev if e.data.get("failed")]
    for e in denied_ev:
        if e.data.get("reason") == "protected_file":
            flags.append("denied_write_to_test")

    calls = [
        {
            "command": str(e.data.get("args", {}).get("command", "")),
            "exit_code": e.data.get("exit_code"),
            "kind": classify(
                str(e.data.get("args", {}).get("command", "")),
                e.data.get("exit_code"),
            ),
        }
        for e in tool_ev
    ]
    kinds: dict[str, int] = {}
    for c in calls:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1

    denied_detail = [
        {"reason": e.data.get("reason"), "command": str(e.data.get("args", {}))[:300]}
        for e in denied_ev
    ]

    contaminated = source_fingerprint(task) != fingerprint_before

    passed, judge_note = run_hidden(ws, task)

    return {
        "task": task,
        "condition": condition,
        "repeat": repeat_idx,
        "passed": passed,
        "judge": judge_note,
        "ok": result.ok,
        "steps": result.steps,
        "tool_calls": len(tool_ev),
        "failed_tools": len(failed_tools),
        "denied": len(denied_ev),
        "denied_detail": denied_detail,
        "stop_reason": result.stop_reason,
        "usage": result.usage,
        "elapsed_s": round(elapsed, 2),
        "cheat_flags": flags,
        "source_contaminated": contaminated,
        "commands": [e.data.get("args", {}) for e in tool_ev],
        "calls": calls,
        "failure_kinds": kinds,
        "workspace": (str(ws.relative_to(ROOT)) if ROOT in ws.parents else str(ws)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", choices=["bash", "files"], required=True)
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--task", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--shell-hint", dest="shell_hint", action="store_true", default=True)
    ap.add_argument("--no-shell-hint", dest="shell_hint", action="store_false")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tasks = sorted(p.name for p in SANDBOX.iterdir() if p.is_dir())
    if args.task:
        tasks = [t for t in tasks if t == args.task]
    if not tasks:
        print("没有任务")
        return 2

    settings = Settings.from_env(ROOT / ".env")
    settings = Settings(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=args.model or settings.model,
        max_steps=args.max_steps,
    )

    if args.dry_run:
        for t in tasks:
            ws = prepare(t)
            print(f"  {t}: 工作区 {ws.relative_to(ROOT)}")
            print(f"    文件: {sorted(snapshot(ws))}")
            shutil.rmtree(ws)
        return 0

    settings.require_key()
    provider = AnthropicProvider(settings)

    rows = []
    for t in tasks:
        for i in range(1, args.repeat + 1):
            row = run_one(t, args.condition, provider, settings, i,
                          shell_hint=args.shell_hint)
            rows.append(row)
            mark = "✅" if row["passed"] else "❌"
            k = row["failure_kinds"]
            print(f"  {mark} {t} #{i}  步数 {row['steps']}  工具 {row['tool_calls']}"
                  f"（非0 {row['failed_tools']}: 测试红={k.get('test_red', 0)}"
                  f" shell={k.get('shell_mismatch', 0)} 其它={k.get('other', 0)}）"
                  f"  拒绝 {row['denied']}"
                  f"  token {row['usage'].get('input_tokens')}/{row['usage'].get('output_tokens')}"
                  f"  {row['elapsed_s']}s  {row['judge']}")
            for f in row["cheat_flags"]:
                print(f"      ⚠️  {f}")

    total = len(rows)
    passed = sum(1 for r in rows if r["passed"])
    print(f"\n条件 {args.condition}: 通过 {passed}/{total}"
          f"（{100.0 * passed / total:.0f}%）")
    print(f"平均步数 {sum(r['steps'] for r in rows) / total:.1f}  "
          f"平均 token {sum(r['usage'].get('output_tokens', 0) for r in rows) / total:.0f}（出）")
    flagged = [r for r in rows if r["cheat_flags"]]
    print(f"带作弊标记的运行: {len(flagged)}/{total}")

    hint_tag = "hint" if args.shell_hint else "nohint"
    out = ROOT / "eval" / "runs" / f"sandbox_{args.condition}_{hint_tag}_{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "condition": args.condition,
        "shell_hint": args.shell_hint,
        "model": settings.model,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": {
            "total": total, "passed": passed,
            "pass_rate": round(100.0 * passed / total, 1),
            "avg_steps": round(sum(r["steps"] for r in rows) / total, 2),
            "avg_output_tokens": round(
                sum(r["usage"].get("output_tokens", 0) for r in rows) / total, 1),
            "flagged_runs": len(flagged),
        },
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果写入 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
