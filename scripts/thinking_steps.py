"""探针（单会话多步版）：在一个会话里连续跑 N 步，每步都要求模型思考，
统计每步返回的 thinking 块和正文字数。

用法（项目根目录）：
    .venv\\Scripts\\python.exe scripts\\thinking_steps.py --steps 40 --out thinking_steps.jsonl

和 thinking_probe.py 的区别：那个是 N 次互相独立的单轮调用，这个是同一个
messages 历史往前滚 N 步——所以它还顺带验证了「assistant 的 thinking 块原样
回传后，下一轮不会 400」这条链路（回传格式直接复用 loop._assistant_message）。

每步写一行 JSONL，最后打印汇总：有 thinking 的步数 / 总步数、thinking 块总数、
正文字符数（每步 + 合计 + 最小/中位/最大）、signature 是否齐全、失败步。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from minicode.config import Settings  # noqa: E402
from minicode.loop import _assistant_message  # noqa: E402
from minicode.provider import AnthropicProvider  # noqa: E402

SYSTEM = "你是一个算术助手。每次都要先在心里算清楚，再给出简洁答案。"

TEMPLATES = [
    "第{step}步：{a} 加 {b} 再乘以 {c} 等于多少？只回数字。",
    "第{step}步：{a} 除以 {b} 的余数是多少？只回数字。",
    "第{step}步：{a} 和 {b} 的最大公约数是多少？只回数字。",
    "第{step}步：{a} 以内有多少个质数？只回数字。",
    "第{step}步：{a} 的平方减去 {b} 的平方等于多少？只回数字。",
    "第{step}步：{a} 是不是 {b} 的倍数？只回「是」或「否」。",
]
SIZES = [7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]


def make_prompt(step: int) -> str:
    t = TEMPLATES[step % len(TEMPLATES)]
    a = SIZES[step % len(SIZES)] * (step + 1)
    b = SIZES[(step + 3) % len(SIZES)]
    c = SIZES[(step + 5) % len(SIZES)]
    return t.format(step=step, a=a, b=b, c=c)


def summarize(rows: list[dict], steps: int) -> dict:
    ok = [r for r in rows if r.get("ok")]
    with_thinking = [r for r in ok if r["n_blocks"] > 0]
    chars = [r["thinking_chars"] for r in with_thinking] or [0]
    sig_ok = [r for r in with_thinking if r["signature_chars"] > 0]
    return {
        "steps": steps,
        "ok_steps": len(ok),
        "failed_steps": steps - len(ok),
        "steps_with_thinking": len(with_thinking),
        "thinking_rate": round(len(with_thinking) / max(len(ok), 1), 4),
        "thinking_blocks": sum(r["n_blocks"] for r in with_thinking),
        "empty_thinking_steps": len([r for r in with_thinking if r["thinking_chars"] == 0]),
        "redacted_blocks": sum(r["redacted_blocks"] for r in with_thinking),
        "signature_ok_steps": len(sig_ok),
        "thinking_chars_total": sum(chars),
        "thinking_chars_min": min(chars),
        "thinking_chars_median": statistics.median(chars),
        "thinking_chars_max": max(chars),
        "thinking_chars_mean": round(statistics.mean(chars), 1),
        "out_tokens_total": sum(r.get("out_tokens", 0) for r in ok),
        "in_tokens_last": rows[-1].get("in_tokens", 0) if rows else 0,
        "seconds_total": round(sum(r.get("seconds", 0) for r in ok), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--out", default=str(ROOT / "thinking_steps.jsonl"))
    args = ap.parse_args()

    settings = Settings.from_env(ROOT / ".env")
    provider = AnthropicProvider(settings, max_tokens=args.max_tokens)
    print(f"model={provider.model} base_url={settings.base_url} steps={args.steps}")

    messages: list[dict] = []
    rows: list[dict] = []
    out_path = Path(args.out)

    for step in range(args.steps):
        prompt = make_prompt(step)
        messages.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
        t0 = time.time()
        try:
            reply = provider.stream(SYSTEM, messages, [], lambda _c: None)
        except Exception as exc:  # 400 之类会在这里露出来
            rows.append({
                "step": step, "ok": False, "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"#{step:02d} FAILED {type(exc).__name__}: {exc}")
            break

        secs = time.time() - t0
        thinking = reply.thinking or []
        chars = sum(len(b.get("thinking", "")) for b in thinking if b["type"] == "thinking")
        sig = sum(len(b.get("signature", "")) for b in thinking)
        row = {
            "step": step,
            "ok": True,
            "history_msgs": len(messages),
            "n_blocks": len(thinking),
            "thinking_chars": chars,
            "signature_chars": sig,
            "redacted_blocks": len([b for b in thinking if b["type"] == "redacted_thinking"]),
            "text_chars": len(reply.text),
            "stop_reason": reply.stop_reason,
            "in_tokens": reply.usage.get("input_tokens", 0),
            "out_tokens": reply.usage.get("output_tokens", 0),
            "seconds": round(secs, 2),
            "answer": reply.text.strip()[:40],
        }
        rows.append(row)
        print(
            f"#{step:02d} blocks={row['n_blocks']} chars={row['thinking_chars']:>5} "
            f"sig={row['signature_chars']:>4} in={row['in_tokens']:>6} "
            f"{secs:>4.1f}s answer={row['answer']!r}"
        )

        # 关键：assistant 消息把 thinking 块原样放在最前面再回灌（和主循环一致）
        messages.append(_assistant_message(reply))
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    s = summarize(rows, args.steps)
    print("\n=== 汇总 ===")
    for k, v in s.items():
        print(f"{k:<24} {v}")
    if s["ok_steps"] == s["steps"] and s["steps_with_thinking"] == s["steps"]:
        print(f"\n结论：{s['steps']} 步全部带 thinking（100%），合计 "
              f"{s['thinking_chars_total']} 字符，thinking 块 {s['thinking_blocks']} 个。")
    else:
        print("\n注意：有步骤缺 thinking 或失败，见上面明细 / JSONL。")
    return 0 if s["ok_steps"] == s["steps"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
