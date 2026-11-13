#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""权限判定的对照实验。

比两组：
    A 子串黑名单    —— 最朴素的写法：命令里出现 "rm -rf" 就算危险
    B 结构化判定    —— 先分词拿到程序名和参数，再按参数语义判

比两个数：
    拦截率       危险命令里被拦下的比例
    误报率       正常命令里被误拦的比例

不需要 API key，纯逻辑，跑一次几毫秒。

用法：
    python eval/check_permission.py
    python eval/check_permission.py --json     # 只输出 JSON
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minicode.policy import SubstringBlacklistJudge, judge  # noqa: E402


def load_cases() -> list[dict]:
    data = json.loads((ROOT / "eval" / "permission_cases.json").read_text(encoding="utf-8"))
    return data["cases"]


def evaluate(cases: list[dict], judge_fn) -> dict:
    danger = [c for c in cases if c["expect"] == "deny"]
    safe = [c for c in cases if c["expect"] == "allow"]

    missed, wrong_allow = [], []
    for c in danger:
        v = judge_fn(c["command"])
        if v.allowed:
            missed.append(c)
    for c in safe:
        v = judge_fn(c["command"])
        if v.denied:
            wrong_allow.append(c)

    return {
        "拦截率": len(danger) - len(missed),
        "危险总数": len(danger),
        "放过的危险命令": missed,
        "正确放行": len(safe) - len(wrong_allow),
        "正常总数": len(safe),
        "误拦的正常命令": wrong_allow,
    }


def rate(part: int, total: int) -> float:
    return round(100.0 * part / total, 1) if total else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true", help="把结果写进 eval/runs/")
    args = ap.parse_args()

    cases = load_cases()
    started = time.time()

    baseline = evaluate(cases, lambda c: SubstringBlacklistJudge().judge(c, ROOT))
    structured = evaluate(cases, lambda c: judge(c, ROOT))

    summary = {
        "样本": {"危险": baseline["危险总数"], "正常": baseline["正常总数"]},
        "子串黑名单": {
            "拦截率": rate(baseline["拦截率"], baseline["危险总数"]),
            "误报率": rate(len(baseline["误拦的正常命令"]), baseline["正常总数"]),
        },
        "结构化判定": {
            "拦截率": rate(structured["拦截率"], structured["危险总数"]),
            "误报率": rate(len(structured["误拦的正常命令"]), structured["正常总数"]),
        },
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(f"用例：{baseline['危险总数']} 条危险 + {baseline['正常总数']} 条正常\n")
    print(f"{'':<14}{'拦截率':>10}{'误报率':>10}")
    print(f"{'子串黑名单':<14}{summary['子串黑名单']['拦截率']:>9}%"
          f"{summary['子串黑名单']['误报率']:>9}%")
    print(f"{'结构化判定':<14}{summary['结构化判定']['拦截率']:>9}%"
          f"{summary['结构化判定']['误报率']:>9}%")

    print("\n子串黑名单放过的危险命令：")
    for c in baseline["放过的危险命令"]:
        print(f"  {c['id']}  {c['command']}    （{c['why']}）")
    print("\n子串黑名单误拦的正常命令：")
    for c in baseline["误拦的正常命令"]:
        print(f"  {c['id']}  {c['command']}    （{c['why']}）")

    if structured["放过的危险命令"]:
        print("\n结构化判定放过的危险命令（说明规则还有洞）：")
        for c in structured["放过的危险命令"]:
            print(f"  {c['id']}  {c['command']}")
    if structured["误拦的正常命令"]:
        print("\n结构化判定误拦的正常命令（说明规则太严）：")
        for c in structured["误拦的正常命令"]:
            print(f"  {c['id']}  {c['command']}")

    if args.save:
        out = ROOT / "eval" / "runs" / f"perm_{int(time.time())}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed_ms": round((time.time() - started) * 1000, 1),
            "summary": summary,
            "baseline_missed": [c["id"] for c in baseline["放过的危险命令"]],
            "baseline_false_alarm": [c["id"] for c in baseline["误拦的正常命令"]],
            "structured_missed": [c["id"] for c in structured["放过的危险命令"]],
            "structured_false_alarm": [c["id"] for c in structured["误拦的正常命令"]],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {out.relative_to(ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
