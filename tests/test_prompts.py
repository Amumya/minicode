"""测试：给模型的环境说明必须和实际用的 shell 一致。"""

import os

from minicode.prompts import base_text, default_system


def test_base_has_no_shell_claim():
    assert "PowerShell" not in base_text()
    assert "/bin/sh" not in base_text()


def test_no_hint_is_just_base():
    assert default_system(False) == base_text()


def test_hint_matches_actual_shell():
    """提示词里说的 shell，必须是 bash 工具真正会用的那一个。

    这是这个文件存在的理由：判断用 os.name，和 tools/bash.py 选 shell 用同一个判断。
    如果两边不一致，模型会按错误的语法写命令，然后无声地失败。
    """
    text = default_system(True)
    if os.name == "nt":
        assert "PowerShell" in text
        assert "Get-ChildItem" in text
    else:
        assert "/bin/sh" in text
