"""Agent Teams：常驻成员 + 异步邮箱 + 冷挂起。

核心模型：
    * Lead（当前用户会话）只与用户对话，通过 team 工具管理成员；
    * 成员名册持久化在 <workspace>/.minicode/team/roster.json；
    * team_send 只是把消息放进成员邮箱并起一个临时线程，立即返回；
    * 成员线程跑完后把 messages 落盘、线程退出（冷挂起，不占内存）；
    * team_wait / team_inbox 收成员结果；下次 team_send 从磁盘恢复上下文继续对话。

工具动作：
    define  定义/更新名册
    send    给某个成员发消息（异步）
    wait    阻塞等待成员结果（可带 timeout 秒）
    inbox   非阻塞取已完成结果
    list    查看成员状态
    history 查看某个成员最近对话
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

from minicode.config import Settings
from minicode.loop import run as run_loop
from minicode.subagent import _default_child_system, report_stub
from minicode.tools.base import Registry, ToolResult

MAX_MEMBERS = 6
DEFAULT_WAIT_SECONDS = 120
MAX_WAIT_SECONDS = 600


def _safe_name(name: str) -> str:
    return quote(name, safe="") or "member"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _last_assistant_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = " ".join(
                str(block.get("text", "")) for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if text:
                return text
    return ""


def _read_json(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


class TeamManager:
    """一个 workspace 的团队运行时；不持有成员完整上下文，成员状态只在磁盘。"""

    def __init__(
        self,
        workspace: Path,
        *,
        provider,
        settings: Settings,
        build_tools,
        max_steps: int = 64,
        on_event=None,
        should_stop=None,
    ) -> None:
        self.workspace = Path(workspace)
        self.provider = provider
        self.settings = settings
        self.build_tools = build_tools
        self.max_steps = max_steps
        self.on_event = on_event
        self.should_stop = should_stop
        self.root = self.workspace / ".minicode" / "team"
        self.roster_path = self.root / "roster.json"
        self.member_dir = self.root / "members"
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._threads: dict[str, threading.Thread] = {}
        self._status: dict[str, str] = {}
        self._results: list[dict] = []
        self._stop = threading.Event()

    # ---------------------------------------------------------------- 基础
    def emit(self, kind: str, **data) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:  # noqa: BLE001 - 观测事件不能影响成员执行
            pass

    def roster(self) -> dict[str, dict]:
        data = _read_json(self.roster_path, {"members": []})
        members = data.get("members") if isinstance(data.get("members"), list) else []
        return {
            str(item.get("name")): item
            for item in members
            if isinstance(item, dict) and item.get("name")
        }

    def _member_state_path(self, name: str) -> Path:
        return self.member_dir / f"{_safe_name(name)}.json"

    def member_state(self, name: str) -> dict:
        return _read_json(self._member_state_path(name), {"messages": [], "usage": {}})

    def status(self, name: str) -> str:
        with self._lock:
            if name in self._threads:
                return "working"
            if name in self._status:
                return self._status[name]
        state = self.member_state(name)
        return str(state.get("status") or "suspended")

    # ---------------------------------------------------------------- 定义
    def define(self, raw_members) -> ToolResult:
        if not isinstance(raw_members, list) or not 1 <= len(raw_members) <= MAX_MEMBERS:
            return ToolResult(f"members 必须是 1–{MAX_MEMBERS} 个成员的数组", exit_code=2)
        members: list[dict] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_members, start=1):
            if not isinstance(raw, dict):
                return ToolResult(f"第 {index} 个成员不是对象", exit_code=2)
            name = str(raw.get("name", "")).strip()
            role = str(raw.get("role", "")).strip() or "通用成员"
            if not name:
                return ToolResult(f"第 {index} 个成员缺少 name", exit_code=2)
            if name in seen:
                return ToolResult(f"成员名重复: {name}", exit_code=2)
            seen.add(name)
            members.append({
                "name": name,
                "role": role,
                "system": str(raw.get("system", "")).strip(),
                "max_steps": int(raw.get("max_steps") or 0) or None,
            })
        _atomic_write(self.roster_path, {"members": members})
        self.emit("team_roster", members=[
            {"name": m["name"], "role": m["role"]} for m in members
        ])
        return ToolResult(
            "已定义团队名册：\n" + "\n".join(f"- {m['name']}（{m['role']}）" for m in members),
            exit_code=0,
            meta={"member_count": len(members)},
        )

    # ---------------------------------------------------------------- 成员执行
    def _worker(self, name: str, definition: dict, message: str) -> None:
        state_path = self._member_state_path(name)
        state = self.member_state(name)
        history = state.get("messages") if isinstance(state.get("messages"), list) else []
        prior_usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
        user_message = {"role": "user", "content": message}
        captured: dict = {}

        def on_step(payload: dict) -> None:
            captured["messages"] = payload.get("messages")
            captured["usage"] = payload.get("usage")

        role = definition.get("role") or "通用成员"
        system = definition.get("system") or (
            _default_child_system()
            + f"\n\n你在一个团队里，角色是：{role}。"
            "只负责你的部分，给出可以直接合并进最终报告的结论；"
            "如果这是连续对话，保持与之前结论一致。"
        )
        max_steps = int(definition.get("max_steps") or self.max_steps)
        child_settings = Settings(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            model=definition.get("model") or self.settings.model,
            max_steps=max_steps,
            max_tokens=self.settings.max_tokens,
        )
        self.emit("team_status", member=name, role=role, status="working")
        try:
            result = run_loop(
                message,
                provider=self.provider,
                tools=self.build_tools(),
                settings=child_settings,
                workspace=self.workspace,
                system=system,
                resume={
                    "messages": [*history, user_message],
                    "step": 0,
                    "usage": prior_usage,
                },
                on_step=on_step,
                should_stop=lambda: self._stop.is_set() or bool(
                    self.should_stop and self.should_stop()
                ),
            )
            messages = captured.get("messages") or [
                *history, user_message,
                {"role": "assistant", "content": result.final_text},
            ]
            usage = captured.get("usage") or prior_usage
            text = (result.final_text or "").strip()
            if not text:
                text = _last_assistant_text(messages)
            if not result.ok:
                note = f"（未完成：{result.stop_reason}，{result.steps} 步）"
                text = f"{note}\n{text}" if text else report_stub(
                    type("R", (), {
                        "ok": result.ok, "stop_reason": result.stop_reason,
                        "steps": result.steps, "error": result.error,
                    })()
                )
            payload = {
                "ok": result.ok,
                "steps": result.steps,
                "tokens": int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
                "text": text,
                "error": result.error or "",
            }
        except Exception as exc:  # noqa: BLE001 - 单个成员失败不能拖垮团队
            messages = [*history, user_message]
            payload = {
                "ok": False, "steps": 0, "tokens": 0,
                "text": f"成员执行出错：{type(exc).__name__}: {exc}",
                "error": f"{type(exc).__name__}: {exc}",
            }
        _atomic_write(state_path, {
            "name": name,
            "messages": messages,
            "usage": captured.get("usage") or prior_usage,
            "status": "suspended",
            "last_result": payload,
            "last_active": time.time(),
        })
        with self._cv:
            self._threads.pop(name, None)
            self._status[name] = "suspended"
            self._results.append({"member": name, "role": role, **payload})
            self._cv.notify_all()
        self.emit("team_status", member=name, role=role, status="suspended")
        self.emit("team_message", member=name, role=role, **payload)

    # ---------------------------------------------------------------- 动作
    def send(self, name: str, message: str) -> ToolResult:
        if not name or not message.strip():
            return ToolResult("send 需要 member 和 message", exit_code=2)
        members = self.roster()
        definition = members.get(name)
        if definition is None:
            return ToolResult(f"没有这个成员：{name}（先用 action=define 定义名册）", exit_code=2)
        with self._lock:
            if name in self._threads:
                return ToolResult(
                    f"{name} 正在执行上一条消息，用 team_wait 等它结束再发。",
                    exit_code=1,
                )
            thread = threading.Thread(
                target=self._worker,
                args=(name, definition, message),
                name=f"team-{name}",
                daemon=True,
            )
            self._threads[name] = thread
            self._status[name] = "working"
            thread.start()
        return ToolResult(
            f"已派发给 {name}（后台执行）。用 team_wait 收结果，或继续做别的事。",
            exit_code=0,
            meta={"member": name, "status": "working"},
        )

    def wait(self, timeout: float = DEFAULT_WAIT_SECONDS) -> ToolResult:
        timeout = max(1.0, min(float(timeout), MAX_WAIT_SECONDS))
        deadline = time.monotonic() + timeout
        with self._cv:
            while not self._results:
                if self.should_stop and self.should_stop():
                    return ToolResult("已取消等待。", exit_code=1)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    active = sorted(self._threads)
                    if active:
                        return ToolResult(
                            f"等待超时；仍在执行：{', '.join(active)}", exit_code=1
                        )
                    return ToolResult("没有正在执行的成员，也没有新结果。", exit_code=1)
                if not self._threads:
                    return ToolResult("没有正在执行的成员，也没有新结果。", exit_code=1)
                self._cv.wait(timeout=remaining)
            results = self._results[:]
            self._results.clear()
        blocks = []
        any_ok = False
        for item in results:
            any_ok = any_ok or bool(item.get("ok"))
            blocks.append(
                f"## {item['member']}（{item.get('role', '成员')}）\n"
                f"{item.get('text') or '（无输出）'}"
            )
        return ToolResult(
            "\n\n".join(blocks),
            exit_code=0 if any_ok else 1,
            meta={"results": results},
        )

    def inbox(self) -> ToolResult:
        with self._cv:
            results = self._results[:]
            self._results.clear()
        if not results:
            return ToolResult("暂无新消息。", exit_code=1)
        lines = [
            f"- {item['member']}：{item.get('text') or item.get('error') or '（无输出）'}"
            for item in results
        ]
        return ToolResult("\n".join(lines), exit_code=0, meta={"results": results})

    def list_members(self) -> ToolResult:
        members = self.roster()
        if not members:
            return ToolResult("还没有定义成员；先用 action=define。", exit_code=1)
        lines = []
        for name, definition in members.items():
            status = self.status(name)
            lines.append(f"- {name}（{definition.get('role', '成员')}）：{status}")
        return ToolResult("\n".join(lines), exit_code=0)

    def history(self, name: str, limit: int = 10) -> ToolResult:
        if name not in self.roster():
            return ToolResult(f"没有这个成员：{name}", exit_code=2)
        state = self.member_state(name)
        messages = state.get("messages") if isinstance(state.get("messages"), list) else []
        lines: list[str] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role not in ("user", "assistant"):
                continue
            if isinstance(content, list):
                text = " ".join(
                    str(block.get("text", "")) for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                text = str(content or "")
            if text.strip():
                lines.append(f"{role}: {text.strip()[:400]}")
        tail = lines[-max(1, min(limit, 50)):]
        return ToolResult("\n".join(tail) or "（没有历史）", exit_code=0)

    def shutdown(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()


class TeamTool:
    """把 TeamManager 包成 `team` 工具。"""

    name = "team"
    description = (
        "管理常驻 Agent 团队（Lead 唯一与用户对话）。先用 action=define 定义成员；"
        "action=send 异步派发消息并立即返回；action=wait 收结果；action=inbox 非阻塞收消息；"
        "action=list 查看状态；action=history 查看成员对话。成员闲时冷挂起，不占内存。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["define", "send", "wait", "inbox", "list", "history"],
                "description": "要执行的动作",
            },
            "members": {
                "type": "array",
                "description": "define 用：成员定义（name / role / system / max_steps）",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "role": {"type": "string"},
                        "system": {"type": "string"},
                        "max_steps": {"type": "integer"},
                    },
                    "required": ["name"],
                },
            },
            "member": {"type": "string", "description": "send/wait/history 的成员名"},
            "message": {"type": "string", "description": "send 的消息"},
            "timeout": {"type": "integer", "description": "wait 超时秒数，默认 120"},
            "limit": {"type": "integer", "description": "history 返回的最大消息数"},
        },
        "required": ["action"],
    }

    def __init__(
        self,
        *,
        provider,
        settings: Settings,
        build_tools,
        max_steps: int = 64,
        on_event=None,
        should_stop=None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.build_tools = build_tools
        self.max_steps = max_steps
        self.on_event = on_event
        self.should_stop = should_stop
        self._manager: TeamManager | None = None
        self._manager_workspace: Path | None = None

    def shutdown(self) -> None:
        if self._manager is not None:
            self._manager.shutdown()

    def _get_manager(self, cwd: Path) -> TeamManager:
        cwd = Path(cwd).resolve()
        if self._manager is None or self._manager_workspace != cwd:
            self._manager = TeamManager(
                cwd,
                provider=self.provider,
                settings=self.settings,
                build_tools=self.build_tools,
                max_steps=self.max_steps,
                on_event=self.on_event,
                should_stop=self.should_stop,
            )
            self._manager_workspace = cwd
        return self._manager

    def run(self, args: dict, cwd: Path) -> ToolResult:
        args = args or {}
        action = str(args.get("action") or "").strip()
        manager = self._get_manager(cwd)
        if action == "define":
            return manager.define(args.get("members"))
        if action == "send":
            return manager.send(str(args.get("member") or "").strip(),
                                 str(args.get("message") or ""))
        if action == "wait":
            timeout = args.get("timeout")
            return manager.wait(float(timeout) if isinstance(timeout, (int, float)) else DEFAULT_WAIT_SECONDS)
        if action == "inbox":
            return manager.inbox()
        if action == "list":
            return manager.list_members()
        if action == "history":
            limit = args.get("limit")
            return manager.history(
                str(args.get("member") or "").strip(),
                int(limit) if isinstance(limit, int) else 10,
            )
        return ToolResult("action 必须是 define/send/wait/inbox/list/history", exit_code=2)
