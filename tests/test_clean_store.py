"""清理脚本的判定逻辑。

`tools/clean_store.py` 要能准确认出"测试写进来的脏数据"，不能误删真实记录。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from clean_store import is_junk  # noqa: E402

from minicode.service.store import RunState  # noqa: E402


def _run(model: str, workspace: str) -> RunState:
    return RunState(id="r1", task="t", model=model, workspace=workspace)


def test_fake_models_are_junk():
    assert is_junk(_run("fake-model", "D:/work/demo")) is True
    assert is_junk(_run("m", "D:/work/demo")) is True
    assert is_junk(_run("FAKE-MODEL", "D:/work/demo")) is True


def test_temp_workspaces_are_junk():
    assert is_junk(_run("deepseek-flash", "D:/repo/pytest-of-1/pytest-2/test_x0")) is True
    assert is_junk(_run("deepseek-flash", "/tmp/pytest-abc/x")) is True
    assert is_junk(_run("deepseek-flash", "C:/Users/me/AppData/Local/Temp/xyz")) is True


def test_real_records_are_not_junk():
    assert is_junk(_run("deepseek-flash", "D:/work/minicode")) is False
    assert is_junk(_run("deepseek-v4-pro", "D:/projects/demo")) is False
