"""文件工具的测试。"""

from pathlib import Path

from minicode.tools.files import EditFileTool, ReadFileTool, WriteFileTool


def test_write_then_read(tmp_path):
    r = WriteFileTool().run({"path": "a/b.txt", "content": "hello\nworld"}, tmp_path)
    assert r.exit_code == 0

    got = ReadFileTool().run({"path": "a/b.txt"}, tmp_path)
    assert got.exit_code == 0
    assert "hello" in got.content
    assert "1|" in got.content  # 带行号


def test_write_outside_workspace_is_refused(tmp_path):
    r = WriteFileTool().run({"path": "../escape.txt", "content": "x"}, tmp_path)
    assert r.exit_code == 1
    assert "工作目录外" in r.content
    assert not (tmp_path.parent / "escape.txt").exists()


def test_absolute_path_outside_is_refused(tmp_path):
    target = tmp_path.parent / "abs_escape.txt"
    r = WriteFileTool().run({"path": str(target), "content": "x"}, tmp_path)
    assert r.exit_code == 1
    assert not target.exists()


def test_edit_replaces_once(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    r = EditFileTool().run(
        {"path": "m.py", "old_text": "x = 1", "new_text": "x = 2"}, tmp_path)
    assert r.exit_code == 0
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "x = 2\n"


def test_edit_refuses_when_not_found(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    r = EditFileTool().run(
        {"path": "m.py", "old_text": "nope", "new_text": "y"}, tmp_path)
    assert r.exit_code == 1
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "x = 1\n"


def test_edit_refuses_when_ambiguous(tmp_path):
    (tmp_path / "m.py").write_text("a\na\n", encoding="utf-8")
    r = EditFileTool().run(
        {"path": "m.py", "old_text": "a", "new_text": "b"}, tmp_path)
    assert r.exit_code == 1
    assert "2 次" in r.content


def test_read_missing_file(tmp_path):
    r = ReadFileTool().run({"path": "nope.py"}, tmp_path)
    assert r.exit_code == 1
