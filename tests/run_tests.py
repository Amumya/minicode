#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖测试运行器 —— 在没有 pytest 的环境里也能跑 CI 同一组测试。

为什么需要它：
    本机 PyPI 不可达（2026-11-13 实测），装不上 pytest。
    但"跑不了测试"和"没测过"是两件事 —— 这个运行器让本地验证不依赖网络。
    CI 里仍然用 pytest，两者跑的是同一批 `test_*` 函数。

用法：
    set PYTHONPATH=src
    python tests/run_tests.py
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    files = sorted(p for p in TESTS_DIR.glob("test_*.py"))
    if not files:
        print("没有找到测试文件")
        return 1

    passed = failed = 0
    failures: list[tuple[str, str]] = []

    for f in files:
        print(f"\n{f.name}")
        try:
            mod = load_module(f)
        except Exception:
            failed += 1
            failures.append((f.name, traceback.format_exc()))
            print("  ❌ 模块导入失败")
            continue

        names = [n for n in dir(mod) if n.startswith("test_")]
        for name in names:
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            try:
                fn()
                print(f"  ✅ {name}")
                passed += 1
            except AssertionError as e:
                print(f"  ❌ {name}: {e}")
                failed += 1
                failures.append((f"{f.name}::{name}", traceback.format_exc()))
            except Exception as e:  # noqa: BLE001 - 运行器要兜住任何异常
                print(f"  ❌ {name}: {type(e).__name__}: {e}")
                failed += 1
                failures.append((f"{f.name}::{name}", traceback.format_exc()))

    print("\n" + "=" * 60)
    print(f"通过 {passed} / 失败 {failed}")

    if failures:
        print("\n失败详情：")
        for name, tb in failures:
            print(f"\n--- {name} ---")
            print(tb)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
