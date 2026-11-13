#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一下技能走"渐进式披露"到底省了多少。

不需要 API、不花钱：直接把系统提示词拼出来数长度。
算的是每轮都要重发的那部分，所以省下来的量会乘以轮数。

用法：
    python eval/measure_prompts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minicode.prompts import default_system  # noqa: E402
from minicode.skills import SkillLoader  # noqa: E402


def main() -> int:
    loader = SkillLoader(ROOT / "skills")
    base = default_system(shell_hint=True)
    with_catalog = default_system(shell_hint=True, skills=loader)

    catalog_chars = len(with_catalog) - len(base)
    total_bodies = loader.total_chars()
    # 粗估 token：中英混排，按 3 字符 1 token 算，够用来比量级
    per_round_saved = total_bodies - catalog_chars

    print(f"技能数：{len(loader.names())}  {loader.names()}")
    print()
    print(f"{'项目':<28}{'字符':>8}{'约 token':>10}")
    print("-" * 46)
    print(f"{'系统提示词（不含技能）':<28}{len(base):>8}{len(base)//3:>10}")
    print(f"{'技能目录（进提示词）':<28}{catalog_chars:>8}{catalog_chars//3:>10}")
    print(f"{'全部技能正文（不进提示词）':<28}{total_bodies:>8}{total_bodies//3:>10}")
    print("-" * 46)
    print(f"{'每轮省下':<28}{per_round_saved:>8}{per_round_saved//3:>10}")
    if total_bodies:
        print(f"目录 / 正文 = {catalog_chars/total_bodies:.1%}")
    print()
    print("注意：省的是**每轮**的量。一个 8 步的任务，这部分会被重发 8 次左右。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
