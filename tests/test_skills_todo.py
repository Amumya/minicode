"""技能加载和任务清单的测试。

技能这块要验的是"渐进式披露真的省了东西"：
系统提示词里只有目录，全文只在 load_skill 时才进上下文。
"""

from pathlib import Path

import pytest

from minicode.skills import SkillLoader, parse_frontmatter
from minicode.tools.skills import LoadSkillTool
from minicode.tools.todo import STATUSES, TodoList, TodoWriteTool

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ frontmatter


def test_parse_frontmatter_basic():
    meta, body = parse_frontmatter("---\nname: a\ndescription: b\n---\n\n正文\n")
    assert meta == {"name": "a", "description": "b"}
    assert body == "正文"


def test_parse_frontmatter_absent():
    meta, body = parse_frontmatter("没有元数据\n")
    assert meta == {}
    assert body == "没有元数据"


def test_parse_frontmatter_unclosed():
    """只有开头没有结尾：不能把整份文件当成元数据吃掉。"""
    text = "---\nname: a\n正文在这里\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert "正文在这里" in body


def test_parse_frontmatter_ignores_comments_and_blank():
    meta, _ = parse_frontmatter("---\n# 注释\n\nname: a\n---\nx")
    assert meta == {"name": "a"}


# ------------------------------------------------------------------ 扫描


def test_scan_repository_skills():
    loader = SkillLoader(ROOT / "skills")
    names = loader.names()
    assert "fix-failing-test" in names
    assert "add-feature" in names


def test_missing_dir_is_fine(tmp_path):
    loader = SkillLoader(tmp_path / "nope")
    assert loader.names() == []
    assert loader.catalog() == ""


def test_catalog_is_much_smaller_than_bodies():
    """核心断言：目录要远远小于全部正文，否则渐进式披露没意义。"""
    loader = SkillLoader(ROOT / "skills")
    assert loader.skills
    assert loader.catalog_chars() * 2 < loader.total_chars(), (
        f"目录 {loader.catalog_chars()} 字符 vs 正文 {loader.total_chars()} 字符"
    )


def test_description_falls_back_to_first_line(tmp_path):
    d = tmp_path / "skills" / "no-desc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# 这是个标题\n\n正文\n", encoding="utf-8")
    loader = SkillLoader(tmp_path / "skills")
    assert loader.get("no-desc").description == "这是个标题"


# ------------------------------------------------------------------ load_skill 工具


def test_load_skill_lists_catalog(tmp_path):
    loader = SkillLoader(ROOT / "skills")
    tool = LoadSkillTool(loader)
    r = tool.run({}, tmp_path)
    assert r.exit_code == 0
    assert "fix-failing-test" in r.content


def test_load_skill_returns_body(tmp_path):
    loader = SkillLoader(ROOT / "skills")
    tool = LoadSkillTool(loader)
    r = tool.run({"name": "fix-failing-test"}, tmp_path)
    assert r.exit_code == 0
    assert "测试文件不能改" in r.content
    assert r.meta["chars"] > 100


def test_load_skill_unknown_name(tmp_path):
    loader = SkillLoader(ROOT / "skills")
    r = LoadSkillTool(loader).run({"name": "nope"}, tmp_path)
    assert r.exit_code == 1
    assert "fix-failing-test" in r.content  # 把可用的列出来


# ------------------------------------------------------------------ todo


def test_todo_update_and_render(tmp_path):
    todo = TodoList()
    tool = TodoWriteTool(todo)
    ok, text = todo.update([
        {"content": "读代码", "status": "completed"},
        {"content": "改函数", "status": "in_progress"},
        {"content": "跑测试"},
    ])
    assert ok
    assert "1. [x] 读代码" in text
    assert "2. [~] 改函数" in text
    assert "3. [ ] 跑测试" in text
    assert "1/3 完成" in text
    assert tool.run({"todos": []}, tmp_path).exit_code == 0


def test_todo_rejects_two_in_progress():
    ok, text = TodoList().update([
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
    ])
    assert not ok
    assert "最多只能一条" in text


def test_todo_rejects_bad_status():
    ok, text = TodoList().update([{"content": "a", "status": "doing"}])
    assert not ok
    assert "状态非法" in text


def test_todo_rejects_empty_content():
    ok, text = TodoList().update([{"content": "  "}])
    assert not ok
    assert "没有 content" in text


def test_todo_rejects_too_many():
    ok, text = TodoList().update([{"content": f"t{i}"} for i in range(40)])
    assert not ok
    assert "太多" in text


def test_todo_rejects_non_list():
    ok, _ = TodoList().update("不是数组")
    assert not ok


def test_todo_tool_marks_failure(tmp_path):
    todo = TodoList()
    r = TodoWriteTool(todo).run({"todos": [{"content": "a", "status": "wrong"}]}, tmp_path)
    assert r.exit_code == 1
    assert r.failed


def test_todo_keeps_previous_on_invalid_update():
    """更新失败时不能把原来的清单弄丢。"""
    todo = TodoList()
    todo.update([{"content": "原有"}])
    ok, _ = todo.update([{"content": ""}])
    assert not ok
    assert todo.snapshot() == [{"content": "原有", "status": "pending", "blockedBy": []}]


def test_status_marks_are_all_defined():
    assert set(STATUSES) == {"pending", "in_progress", "completed"}


# ------------------------------------------------------------------ 依赖与环检测


def test_todo_cycle_is_rejected():
    """环不会报错，只会让计划永远开不了工 —— 必须在提交那一刻拦下。"""
    ok, text = TodoList().update([
        {"content": "A", "blockedBy": [2]},
        {"content": "B", "blockedBy": [1]},
    ])
    assert not ok
    assert "环" in text
    assert "1 → 2 → 1" in text


def test_todo_three_node_cycle_is_rejected():
    ok, text = TodoList().update([
        {"content": "A", "blockedBy": [3]},
        {"content": "B", "blockedBy": [1]},
        {"content": "C", "blockedBy": [2]},
    ])
    assert not ok
    assert "环" in text


def test_todo_self_dependency_rejected():
    ok, text = TodoList().update([{"content": "A", "blockedBy": [1]}])
    assert not ok
    assert "不能依赖自己" in text


def test_todo_dependency_out_of_range():
    ok, text = TodoList().update([
        {"content": "A"},
        {"content": "B", "blockedBy": [5]},
    ])
    assert not ok
    assert "不存在的编号" in text


def test_todo_blocked_item_cannot_start():
    ok, text = TodoList().update([
        {"content": "A"},
        {"content": "B", "status": "in_progress", "blockedBy": [1]},
    ])
    assert not ok
    assert "未完成的前置" in text


def test_todo_blocked_item_cannot_complete():
    ok, text = TodoList().update([
        {"content": "A"},
        {"content": "B", "status": "completed", "blockedBy": [1]},
    ])
    assert not ok
    assert "未完成的前置" in text


def test_todo_pending_may_wait_on_others():
    """pending 的条目可以等着别人 —— 这是计划的常态，不该被拒。"""
    ok, _ = TodoList().update([
        {"content": "A", "status": "in_progress"},
        {"content": "B", "blockedBy": [1]},
    ])
    assert ok


def test_todo_dependency_unlocks_after_complete():
    todo = TodoList()
    ok, _ = todo.update([
        {"content": "A", "status": "completed"},
        {"content": "B", "status": "in_progress", "blockedBy": [1]},
    ])
    assert ok


def test_todo_render_shows_waiting():
    todo = TodoList()
    ok, text = todo.update([
        {"content": "A", "status": "in_progress"},
        {"content": "B", "blockedBy": [1]},
        {"content": "C", "blockedBy": [1, 2]},
    ])
    assert ok
    assert "← 等 [1]" in text
    assert "← 等 [1, 2]" in text


def test_todo_render_hides_waited_marker_when_done():
    todo = TodoList()
    ok, text = todo.update([
        {"content": "A", "status": "completed"},
        {"content": "B", "blockedBy": [1]},
    ])
    assert ok
    assert "← 等" not in text


def test_todo_snapshot_includes_blockers():
    todo = TodoList()
    todo.update([{"content": "A"}, {"content": "B", "blockedBy": [1]}])
    assert todo.snapshot()[1]["blockedBy"] == [1]


def test_todo_blocker_must_be_int():
    ok, text = TodoList().update([
        {"content": "A"},
        {"content": "B", "blockedBy": ["1"]},
    ])
    assert not ok
    assert "不是整数" in text
