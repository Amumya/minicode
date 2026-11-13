"""文件读写工具。

三个工具都只能在工作目录里动文件。越界的路径直接报错，
校验放在工具层和权限层各做一次 —— 权限层负责"拒"，工具层负责"就算绕过也写不出去"。
"""

from __future__ import annotations

from pathlib import Path

from minicode.tools.base import ToolResult


class PathOutsideWorkspace(ValueError):
    """路径跑出工作目录了。"""


def safe_path(rel: str, workspace: Path) -> Path:
    """把相对路径解析到工作目录内，越界就抛异常。

    用 resolve() 而不是字符串前缀比较，是为了挡住 `src/../../../etc` 这种。
    """
    raw = Path(rel)
    target = raw if raw.is_absolute() else workspace / raw
    resolved = target.resolve()
    root = workspace.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathOutsideWorkspace(f"路径在工作目录外: {rel}") from exc
    return resolved


def _err(msg: str) -> ToolResult:
    return ToolResult(msg, exit_code=1)


class ReadFileTool:
    name = "read_file"
    description = "读取工作目录里的一个文本文件，返回内容和行号。"
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "相对工作目录的路径"}},
        "required": ["path"],
    }

    def run(self, args: dict, cwd: Path) -> ToolResult:
        rel = (args or {}).get("path", "")
        if not rel:
            return _err("缺少 path 参数")
        try:
            path = safe_path(rel, cwd)
        except PathOutsideWorkspace as exc:
            return _err(str(exc))
        if not path.is_file():
            return _err(f"文件不存在: {rel}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _err(f"不是 UTF-8 文本: {rel}")

        numbered = "\n".join(
            f"{i:>4}| {line}" for i, line in enumerate(text.splitlines(), 1)
        )
        return ToolResult(numbered, exit_code=0, meta={"path": rel, "lines": text.count("\n") + 1})


class WriteFileTool:
    name = "write_file"
    description = "把内容整个写入一个文件，父目录不存在会自动创建。"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    def run(self, args: dict, cwd: Path) -> ToolResult:
        rel = (args or {}).get("path", "")
        content = (args or {}).get("content", "")
        if not rel:
            return _err("缺少 path 参数")
        try:
            path = safe_path(rel, cwd)
        except PathOutsideWorkspace as exc:
            return _err(str(exc))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult(f"已写入 {rel}（{len(content)} 字符）", exit_code=0,
                          meta={"path": rel, "bytes": len(content.encode('utf-8'))})


class EditFileTool:
    name = "edit_file"
    description = (
        "把文件里的 old_text 替换成 new_text。old_text 必须在文件里恰好出现一次，"
        "否则不修改，避免改错地方。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def run(self, args: dict, cwd: Path) -> ToolResult:
        rel = (args or {}).get("path", "")
        old = (args or {}).get("old_text", "")
        new = (args or {}).get("new_text", "")
        if not rel or not old:
            return _err("缺少 path 或 old_text 参数")
        try:
            path = safe_path(rel, cwd)
        except PathOutsideWorkspace as exc:
            return _err(str(exc))
        if not path.is_file():
            return _err(f"文件不存在: {rel}")

        text = path.read_text(encoding="utf-8")
        hits = text.count(old)
        if hits == 0:
            return _err(f"没找到要替换的内容: {old[:40]!r}")
        if hits > 1:
            return _err(f"要替换的内容出现了 {hits} 次，不唯一，没有修改")

        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return ToolResult(f"已修改 {rel}", exit_code=0, meta={"path": rel})
