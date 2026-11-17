"""探针：连打 N 次模型调用，统计每次返回的 thinking 块。

用法（项目根目录）：
    .venv\\Scripts\\python.exe scripts\\thinking_probe.py --calls 40 --concurrency 4

走的是项目自己的 AnthropicProvider.stream，也就是主循环实际用的那条路，
所以它同时验证了「端点上确实会回 thinking」和「provider 没把 thinking 丢掉」。

每行结果写进 JSONL，最后打印汇总：
- 有 thinking 的调用数 / 总调用数
- thinking 块总数、redacted_thinking 块数
- thinking 正文字符数（每条 + 合计 + 最小/中位/最大）
- signature 是否齐全（带了 signature 才能原样回传，否则下一轮 400）
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minicode.config import Settings  # noqa: E402
from minicode.provider import AnthropicProvider  # noqa: E402

SYSTEM = "你是一个严谨的助手。先用 thinking 认真推理，再用一句话给出结论。"

# 每步算式都不一样，避免服务端把重复请求当缓存命中，也顺便覆盖不同长度的思考
TEMPLATES = [
    "计算 {a} + {b} * {c} 的结果，只回一个数字。",
    "计算 ({a} - {b}) * {c} 的结果，只回一个数字。",
    "计算 {a} / {b} 的整数商和余数，用「商,余数」格式回答。",
    "把 {a} 换算成十六进制，只回结果。",
    "判断 {a} 是不是质数，只回 是/否。",
]
SIZES = [7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]


def make_prompt(i: int) -> str:
    t = TEMPLATES[i % len(TEMPLATES)]
    a = SIZES[i % len(SIZES)] * (i + 1)
    b = SIZES[(i + 3) % len(SIZES)]
    c = SIZES[(i + 5) % len(SIZES)]
    return t.format(a=a, b=b, c=c)


def one_call(provider: AnthropicProvider, i: int) -> dict:
    prompt = make_prompt(i)
    deltas: list[str] = []
    started = time.perf_counter()
    try:
        reply = provider.stream(
            SYSTEM,
            [{"role": "user", "content": prompt}],
            [],
            deltas.append,
        )
    except Exception as exc:  # noqa: BLE001 - 单次失败要记下来，不能让整轮挂掉
        return {
            "i": i,
            "ok": False,
            "prompt": prompt,
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "latency_s": round(time.perf_counter() - started, 2),
        }

    latency = time.perf_counter() - started
    blocks = list(reply.thinking or [])
    redacted = [b for b in blocks if b.get("type") == "redacted_thinking"]
    plain = [b for b in blocks if b.get("type") == "thinking"]
    text = "".join(b.get("thinking") or "" for b in plain)
    sig_chars = sum(len(b.get("signature") or "") for b in plain)

    return {
        "i": i,
        "ok": True,
        "prompt": prompt,
        "n_blocks": len(blocks),
        "n_plain": len(plain),
        "n_redacted": len(redacted),
        "thinking_chars": len(text),
        "signature_chars": sig_chars,
        "all_signed": all(bool(b.get("signature")) for b in plain) if plain else False,
        "text_chars": len(reply.text or ""),
        "answer": (reply.text or "").strip()[:60],
        "usage": reply.usage,
        "latency_s": round(latency, 2),
    }


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("ok")]
    failed = [r for r in rows if not r.get("ok")]
    with_think = [r for r in ok if r["n_blocks"] > 0]
    chars = [r["thinking_chars"] for r in ok]

    def pct(n: int, d: int) -> str:
        return f"{(100.0 * n / d):.1f}%" if d else "-"

    summary = {
        "calls": len(rows),
        "ok": len(ok),
        "failed": len(failed),
        "calls_with_thinking": len(with_think),
        "call_thinking_rate": pct(len(with_think), len(ok)),
        "total_blocks": sum(r["n_blocks"] for r in ok),
        "blocks_per_call_avg": round(statistics.mean([r["n_blocks"] for r in ok]), 2) if ok else 0,
        "redacted_blocks": sum(r["n_redacted"] for r in ok),
        "total_thinking_chars": sum(chars),
        "thinking_chars_min": min(chars) if chars else 0,
        "thinking_chars_median": int(statistics.median(chars)) if chars else 0,
        "thinking_chars_max": max(chars) if chars else 0,
        "thinking_chars_avg": round(statistics.mean(chars), 1) if chars else 0,
        "calls_all_signed": sum(1 for r in ok if r["all_signed"]),
        "calls_with_empty_thinking": sum(1 for r in with_think if r["thinking_chars"] == 0),
        "total_output_tokens": sum((r.get("usage") or {}).get("output_tokens") or 0 for r in ok),
        "latency_avg_s": round(statistics.mean([r["latency_s"] for r in ok]), 2) if ok else 0,
        "errors": [r["error"] for r in failed][:5],
    }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=40)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--out", default=str(ROOT / "thinking_probe.jsonl"))
    args = ap.parse_args()

    settings = Settings.from_env(ROOT / ".env")
    provider = AnthropicProvider(settings, max_tokens=args.max_tokens)
    print(f"model={provider.model} base_url={settings.base_url} calls={args.calls}", flush=True)

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(one_call, provider, i) for i in range(args.calls)]
        for fut in futures:
            row = fut.result()
            rows.append(row)
            if row["ok"]:
                print(
                    f"#{row['i']:02d} blocks={row['n_blocks']} "
                    f"chars={row['thinking_chars']:5d} sig={row['signature_chars']:5d} "
                    f"{row['latency_s']:5.1f}s answer={row['answer']!r}",
                    flush=True,
                )
            else:
                print(f"#{row['i']:02d} FAIL {row['error']}", flush=True)

    rows.sort(key=lambda r: r["i"])
    out = Path(args.out)
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )

    summary = summarize(rows)
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nper-call jsonl -> {out}")
    return 0 if summary["failed"] == 0 and summary["calls_with_thinking"] == summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
