"""骨架自检：不需要 API key、不联网。

这些测试的作用不是"证明功能正确"（阶段 A 还没有功能），
而是**防止骨架腐坏**：包能被导入、版本号存在、src layout 没被写乱、
必需的仓库文件还在。CI 每次都跑这一组。
"""

from pathlib import Path

import minicode

ROOT = Path(__file__).resolve().parents[1]


def test_package_imports():
    assert minicode.__version__ == "0.1.0"


def test_src_layout():
    """包必须位于 src/ 下 —— 否则 editable install 与 CI 都会走歪。"""
    assert (ROOT / "src" / "minicode" / "__init__.py").is_file()
    assert not (ROOT / "minicode").exists(), "包不应该出现在仓库根目录下"


def test_repo_skeleton_files():
    for rel in ("README.md", "LICENSE", ".gitignore", "pyproject.toml", ".env.example"):
        assert (ROOT / rel).is_file(), f"缺少仓库文件: {rel}"


def test_env_example_has_no_secret():
    """.env.example 只能有空值，防止有人把真 key 填进去提交。"""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DEEPSEEK_API_KEY":
            assert value.strip() == "", ".env.example 里的 DEEPSEEK_API_KEY 必须留空"
