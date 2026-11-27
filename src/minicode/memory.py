"""跨 run 的长期记忆。

为什么单独一个模块：它是**治理层**的一部分 —— 记忆会被注入系统提示词，
写进去的东西会影响以后每一次运行，所以要有明确的格式、上限、去重和删除入口，
而不是往一个 txt 里随便追加。

存储格式：`<workspace>/.minicode/memory.jsonl`，一行一条：

    {"id": "a1b2c3d4", "text": "测试用 .venv\\\\Scripts\\\\python.exe", "tags": ["pytest"], "created_at": 1763...}

用 JSONL 而不是 markdown：程序要按 id 删除、要去重、要排序，JSONL 解析不会歧义。
人工查看走 `GET /memory` 或界面右栏。

几个刻意的取舍：
    * id 由文本哈希得到 → 相同内容天然去重，不需要额外索引
    * 注入提示词时按"和当前任务的相关度"挑，不是把全部塞进去（否则提示词失控）
    * 单文件、无锁：这是单机自用工具，多进程同时写同一个 workspace 才会冲突，文档里写清楚
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# 单条记忆的长度上限：再长就不适合塞进提示词了
MAX_ENTRY_CHARS = 400
# 注入提示词的字符预算
DEFAULT_SNAPSHOT_CHARS = 1200
# 单条记忆的条数上限（超了丢最早的）
DEFAULT_LIMIT = 200

# 第二层近似判重：字符二元组 Jaccard 阈值
DUPLICATE_JACCARD = 0.9
JUDGE_JACCARD = 0.7

_ASCII_TOKEN = re.compile(r"[a-z0-9_]{2,}")
_CJK_CHUNK = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> set[str]:
    """粗粒度分词，够用来算"和任务的相关度"。

    中文按二字组切（没有分词库，但二字组对"pytest / 解释器 / 约定"这类词够用），
    英文按单词。故意保持简单 —— 复杂排序在这里收益很低。
    """
    low = (text or "").lower()
    tokens: set[str] = set(_ASCII_TOKEN.findall(low))
    for chunk in _CJK_CHUNK.findall(low):
        if len(chunk) == 1:
            tokens.add(chunk)
        else:
            tokens.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _make_id(text: str) -> str:
    return hashlib.sha1(_normalize(text).lower().encode("utf-8")).hexdigest()[:8]


def shingles(text: str) -> set[str]:
    """字符二元组集合；中文/英文都能用，避免英文单词级分词把短句判得过远。"""
    normalized = _normalize(text).lower()
    if len(normalized) <= 1:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def jaccard(a: str, b: str) -> float:
    left, right = shingles(a), shingles(b)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class MemoryJudge(Protocol):
    """第三层判重的模型接口。只允许 same / different / merge 三种结论。"""

    def decide(self, new_text: str, existing_text: str) -> str: ...
    def merge(self, new_text: str, existing_text: str) -> str: ...


_DECIDE_SYSTEM = """你是长期记忆去重器。比较两条记忆是否表达同一个事实。
只输出一个词：same / different / merge。
- same：同一个事实，只是措辞不同
- different：两个事实
- merge：同一个事实但有互补细节，应合并成一条
不要解释，不要标点。"""

_MERGE_SYSTEM = """你是长期记忆合并器。把两条记忆合并成一条更完整、简洁的事实。
只输出合并后的正文，不要解释，不要 Markdown。"""


class LLMMemoryJudge:
    """用主模型做第三层判重；失败时保守返回 different，不丢新记忆。"""

    def __init__(self, provider) -> None:
        self.provider = provider

    def _ask(self, system: str, user: str) -> str:
        reply = self.provider.complete(system, [{"role": "user", "content": user}], [])
        return (getattr(reply, "text", "") or "").strip()

    def decide(self, new_text: str, existing_text: str) -> str:
        try:
            answer = self._ask(
                _DECIDE_SYSTEM,
                f"已有记忆：{existing_text}\n新记忆：{new_text}",
            ).lower()
        except Exception:  # noqa: BLE001 - 判重失败不能阻断记忆写入
            return "different"
        for word in ("same", "different", "merge"):
            if re.search(rf"\b{word}\b", answer):
                return word
        return "different"

    def merge(self, new_text: str, existing_text: str) -> str:
        try:
            merged = self._ask(
                _MERGE_SYSTEM,
                f"已有记忆：{existing_text}\n新记忆：{new_text}",
            )
        except Exception:  # noqa: BLE001
            return ""
        return merged


@dataclass
class MemoryEntry:
    id: str
    text: str
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "tags": list(self.tags),
            "created_at": self.created_at,
        }

    def line(self) -> str:
        """给提示词/`recall` 输出用的一行。"""
        stamp = time.strftime("%Y-%m-%d", time.localtime(self.created_at or time.time()))
        suffix = f"（标签：{', '.join(self.tags)}）" if self.tags else ""
        return f"- [{stamp}] {self.text}{suffix}"


class MemoryStore:
    """一个 workspace 对应一份记忆。"""

    def __init__(
        self,
        path: Path | str,
        limit: int = DEFAULT_LIMIT,
        judge: MemoryJudge | None = None,
    ) -> None:
        self.path = Path(path)
        self.limit = limit
        self.judge = judge

    # ---------------------------------------------------------------- 读写

    def entries(self) -> list[MemoryEntry]:
        if not self.path.exists():
            return []
        out: list[MemoryEntry] = []
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                text = str(data.get("text", "")).strip()
                if not text:
                    continue
                out.append(MemoryEntry(
                    id=str(data.get("id") or _make_id(text)),
                    text=text,
                    tags=[str(t) for t in (data.get("tags") or [])],
                    created_at=float(data.get("created_at") or 0.0),
                ))
            except (ValueError, TypeError):
                continue       # 手改坏了某一行就跳过，不整个文件作废
        return out

    def _write(self, entries: list[MemoryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in entries)
        data = body + ("\n" if body else "")
        # 先写临时文件再原子替换：写到一半崩溃不会把整份记忆文件打烂。
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------------------------------------------------------------- 增删

    def add(self, text: str, tags: list[str] | None = None) -> tuple[MemoryEntry, bool]:
        """三层判重后写入。

        1. 严格指纹：归一化文本 sha1 相同 → 直接合并标签，返回已有条目；
        2. 字符二元组 Jaccard ≥0.9 → 判重；0.7–0.89 → 模型裁决；
        3. 模型只答 same / different / merge；merge 再让模型合并正文。
        没有注入 judge 时，第三层保守按 different 处理（正常新增）。
        """
        clean_full = _normalize(text)
        if not clean_full:
            raise ValueError("记忆内容不能为空")
        # id 按完整文本算：长文本只要尾巴不同，就不能因前 400 字相同被当成重复。
        entry_id = _make_id(clean_full)
        clean = clean_full
        if len(clean) > MAX_ENTRY_CHARS:
            clean = clean[:MAX_ENTRY_CHARS].rstrip() + "…"

        entries = self.entries()
        new_tags = sorted({t.strip() for t in (tags or []) if t.strip()})

        # 第一层：严格指纹
        for existing in entries:
            if existing.id == entry_id:
                return self._merge_tags(existing, entries, new_tags), False

        # 第二层：确定性近似
        best: MemoryEntry | None = None
        best_score = 0.0
        for existing in entries:
            score = jaccard(clean_full, existing.text)
            if score > best_score:
                best, best_score = existing, score

        if best is not None and best_score >= DUPLICATE_JACCARD:
            return self._merge_tags(best, entries, new_tags), False

        if best is not None and best_score >= JUDGE_JACCARD:
            decision = "different"
            if self.judge is not None:
                decision = self.judge.decide(clean_full, best.text).strip().lower()
            if decision == "same":
                return self._merge_tags(best, entries, new_tags), False
            if decision == "merge":
                merged = self.judge.merge(clean_full, best.text) if self.judge else ""
                if merged.strip():
                    best.text = merged.strip()[:MAX_ENTRY_CHARS].rstrip()
                    self._merge_tags(best, entries, new_tags, write=False)
                    self._write(entries)
                    return best, False

        # 第三层判为 different，或压根没有候选 → 正常新增
        entry = MemoryEntry(
            id=entry_id,
            text=clean,
            tags=new_tags,
            created_at=time.time(),
        )
        entries.append(entry)
        if len(entries) > self.limit:
            entries = entries[-self.limit :]
        self._write(entries)
        return entry, True

    def _merge_tags(
        self,
        entry: MemoryEntry,
        entries: list[MemoryEntry],
        new_tags: list[str],
        *,
        write: bool = True,
    ) -> MemoryEntry:
        merged = sorted(set(entry.tags) | set(new_tags))
        if merged != entry.tags:
            entry.tags = merged
            if write:
                self._write(entries)
        return entry

    def forget(self, entry_id: str) -> bool:
        entries = self.entries()
        kept = [e for e in entries if e.id != entry_id]
        if len(kept) == len(entries):
            return False
        self._write(kept)
        return True

    def clear(self) -> int:
        entries = self.entries()
        self._write([])
        return len(entries)

    # ---------------------------------------------------------------- 排序与注入

    def relevant(self, query: str = "", limit: int = 8) -> list[MemoryEntry]:
        """按与 query 的相关度挑记忆。query 为空就返回最近的几条。"""
        entries = self.entries()
        if not entries:
            return []
        query_tokens = tokenize(query)

        def score(entry: MemoryEntry) -> tuple[float, float]:
            if not query_tokens:
                return (0.0, entry.created_at)
            entry_tokens = tokenize(entry.text + " " + " ".join(entry.tags))
            overlap = len(query_tokens & entry_tokens)
            return (float(overlap), entry.created_at)

        ranked = sorted(entries, key=score, reverse=True)
        return ranked[: max(1, limit)]

    def snapshot(
        self,
        query: str = "",
        max_entries: int = 8,
        max_chars: int = DEFAULT_SNAPSHOT_CHARS,
    ) -> str:
        """拼成注入系统提示词的那一段。空记忆返回空字符串。"""
        picked = self.relevant(query, limit=max_entries)
        if not picked:
            return ""
        lines: list[str] = []
        used = 0
        for entry in picked:
            line = entry.line()
            if len(line) > max_chars:
                continue
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)
