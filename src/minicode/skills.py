"""技能：把"怎么做某类事"的指令写成文件，按需加载。

为什么要这样而不是全塞进系统提示词：
    系统提示词每轮都要发一次。塞十份说明书进去，每轮都付一遍 token，
    而且大部分用不上。所以只放一份**目录**（每个技能一行名字和一句描述），
    真要用了再让 agent 自己调 `load_skill` 取全文。

这就是"渐进式披露"，参考实现（learn-claude-code s07）也是这个思路。
我这版把 frontmatter 解析写成不依赖 yaml 的小函数 —— 为了不为一件事引入一个依赖。

目录约定：

    skills/
      <技能名>/
        SKILL.md      ← 开头是 --- 包起来的元数据，然后是正文
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """切出 `---` 包起来的元数据。解析失败就当没有元数据，正文照用。

    故意不引 yaml：这里只需要 `key: value` 这种形式，
    为它加一个依赖不划算（而且 yaml 还会把 `on` 之类解析成布尔）。
    """
    if not text.startswith("---"):
        return {}, text.strip()

    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}, text.strip()

    meta_block = rest[:end]
    body = rest[end + 4:].strip()

    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


class SkillLoader:
    def __init__(self, skills_dir: Path | None = None) -> None:
        self.dir = Path(skills_dir) if skills_dir else Path.cwd() / "skills"
        self.skills: dict[str, Skill] = {}
        self.scan()

    def scan(self) -> None:
        self.skills.clear()
        if not self.dir.is_dir():
            return
        root = self.dir.resolve()
        for manifest in sorted(self.dir.glob("*/SKILL.md")):
            try:
                if not manifest.is_file() or not manifest.resolve().is_relative_to(root):
                    continue
                text = manifest.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = parse_frontmatter(text)
            name = (meta.get("name") or manifest.parent.name).strip()
            description = (meta.get("description") or "").strip()
            if not description:
                first = body.splitlines()[0] if body else ""
                description = first.lstrip("# ").strip()
            self.skills[name] = Skill(name=name, description=description,
                                      body=body, path=manifest)

    def names(self) -> list[str]:
        return sorted(self.skills)

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def catalog(self) -> str:
        """只列名字和一句描述 —— 这段会进系统提示词，必须短。"""
        if not self.skills:
            return ""
        lines = [f"- {s.name}: {s.description}" for s in
                 (self.skills[n] for n in self.names())]
        return "\n".join(lines)

    def catalog_chars(self) -> int:
        return len(self.catalog())

    def total_chars(self) -> int:
        return sum(len(s.body) for s in self.skills.values())
