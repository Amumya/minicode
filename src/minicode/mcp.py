"""MCP（Model Context Protocol）客户端。

协议背景（2026-11 时点的官方规范）：
    * 最新版 `2026-07-28` 是"**现代**"协议：**没有 initialize 握手**，每个请求在
      `_meta` 里自带协议版本/客户端信息/能力；响应是 `{result: {resultType: "complete", ...}}`。
    * `2025-11-25` 及更早是"**旧版**"：先 `initialize` 握手 → `notifications/initialized`，
      然后才能 `tools/list` / `tools/call`；响应没有 `resultType`。
    * 两代都是 JSON-RPC 2.0，stdio 传输按行分隔。

本项目的客户端策略：**先按现代版发，失败再回退旧版**（从新到旧把版本试一遍）。
这样既能对接最新 server，也能对接大量还在用 2025-06-18 的现成 server。

安全边界（重要）：
    * 配置里写了 `command` 就等于允许它在本机执行任意代码 —— 这是 MCP 的固有风险。
    * 因此：server 必须显式 `"enabled": true`，工具必须显式列进 `"allow"`，
      两个条件缺一不可；默认一个都不放行。
    * 本模块只负责"连上、列出来、调一下"，权限判断在 `tools/mcp.py` 的适配器里做。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from minicode import __version__

# 现代协议版本（无握手）
MODERN_VERSION = "2026-07-28"
# 旧版协议版本，从新到旧尝试
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")

# 单个工具结果的字符上限：外部 server 的输出不可控，和 bash 那次是同一类坑
MAX_RESULT_CHARS = 20_000
# 启动/握手超时、单次请求超时
START_TIMEOUT = 15.0
REQUEST_TIMEOUT = 30.0
# stderr 最多留多少行用于报错
STDERR_LINES = 200

CONFIG_RELATIVE = Path(".minicode") / "mcp.json"


class McpError(RuntimeError):
    """连接/协议/调用层面的错误。message 要能直接给用户看。"""


# ------------------------------------------------------------------ 配置


@dataclass
class ServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = False          # 默认关：配置写错/忘记开不应该默默执行外部代码
    allow: list[str] = field(default_factory=list)   # 枚举白名单，默认拒绝未知工具
    timeout: float = REQUEST_TIMEOUT

    def argv(self) -> list[str]:
        return [self.command, *self.args]

    def describe(self) -> str:
        return " ".join(self.argv())


def config_path(workspace: Path) -> Path:
    return Path(workspace) / CONFIG_RELATIVE


def load_servers(workspace: Path, path: Path | None = None) -> list[ServerConfig]:
    """读 `<workspace>/.minicode/mcp.json`（或显式指定的 path）。不存在返回空列表。"""
    path = Path(path) if path is not None else config_path(workspace)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise McpError(f"读不了 MCP 配置 {path}：{exc}") from exc

    items = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise McpError(f"MCP 配置格式不对：{path} 里应有 servers 数组")

    servers: list[ServerConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise McpError(f"servers[{index}] 不是对象")
        name = str(item.get("name", "")).strip()
        if not name:
            raise McpError(f"servers[{index}] 缺少 name")
        if name in seen:
            raise McpError(f"server 名字重复：{name}")
        seen.add(name)
        command = str(item.get("command", "")).strip()
        if not command:
            raise McpError(f"server {name} 缺少 command")
        args = item.get("args") or []
        if not isinstance(args, list):
            raise McpError(f"server {name} 的 args 应该是数组")
        env = item.get("env") or {}
        if not isinstance(env, dict):
            raise McpError(f"server {name} 的 env 应该是对象")
        allow = item.get("allow") or []
        if not isinstance(allow, list):
            raise McpError(f"server {name} 的 allow 应该是数组")
        try:
            timeout = float(item.get("timeout", REQUEST_TIMEOUT))
        except (TypeError, ValueError):
            raise McpError(f"server {name} 的 timeout 不是数字") from None
        servers.append(ServerConfig(
            name=name,
            command=command,
            args=[str(a) for a in args],
            env={str(k): str(v) for k, v in env.items()},
            cwd=(str(item["cwd"]) if item.get("cwd") else None),
            enabled=bool(item.get("enabled", False)),
            allow=[str(t) for t in allow],
            timeout=timeout,
        ))
    return servers


# ------------------------------------------------------------------ 结果渲染


def render_tool_result(result: dict) -> tuple[str, bool]:
    """把 tools/call 的 result 变成 (给模型看的文本, 是否错误)。"""
    is_error = result.get("isError") is True
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "image":
            parts.append(f"[图片 {item.get('mimeType', '?')}，已省略]")
        elif kind == "audio":
            parts.append(f"[音频 {item.get('mimeType', '?')}，已省略]")
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(f"[资源 {res.get('uri', '?')}]")
        else:
            parts.append(f"[{kind or '未知内容'}]")
    structured = result.get("structuredContent")
    if structured is not None and not parts:
        parts.append(json.dumps(structured, ensure_ascii=False))
    text = "\n".join(p for p in parts if p)
    if not text:
        text = "（server 返回了空内容）"
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…[截断，原始长度 {len(text)} 字符]"
    return text, is_error


def _supported_versions_from_error(error: dict) -> list[str]:
    """从 UnsupportedProtocolVersionError（code -32022）里尽量抠出支持的版本列表。"""
    data = error.get("data")
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, str):
            if len(node) >= 8 and node[:4].isdigit() and node.count("-") >= 2:
                found.append(node)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(data)
    return found


# ------------------------------------------------------------------ 客户端


class McpClient:
    """一个 MCP server 子进程 + 一条请求/响应通道。"""

    def __init__(self, config: ServerConfig, cwd: Path | None = None) -> None:
        self.config = config
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.mode = ""              # "modern" / "legacy"
        self.protocol_version = ""
        self.server_info: dict = {}
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stderr: list[str] = []
        self._stderr_lock = threading.Lock()
        self._next_id = 1
        self._eof = threading.Event()
        self._threads: list[threading.Thread] = []
        self.startup_ms = 0.0
        self.last_call_ms = 0.0

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self.proc is not None:
            return
        env = None
        if self.config.env:
            env = {**os.environ, **self.config.env}
        cwd = self.config.cwd or (str(self.cwd) if self.cwd else None)
        popen_kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "env": env,
            "cwd": cwd,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(self.config.argv(), **popen_kwargs)
        except FileNotFoundError as exc:
            raise McpError(f"起不来 server {self.config.name}：找不到 {self.config.command}") from exc
        except OSError as exc:
            raise McpError(f"起不来 server {self.config.name}：{exc}") from exc

        self._threads = [
            threading.Thread(target=self._read_stdout, name=f"mcp-out-{self.config.name}", daemon=True),
            threading.Thread(target=self._read_stderr, name=f"mcp-err-{self.config.name}", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        started = time.perf_counter()
        try:
            self._handshake()
        finally:
            self.startup_ms = round((time.perf_counter() - started) * 1000, 1)

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        self._eof.set()
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        _kill_process_tree(proc)
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads = []

    # ---------------------------------------------------------------- 读线程

    def _read_stdout(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    # server 往 stdout 打日志是常见毛病：不崩，记下来给诊断用
                    self._note_diagnostic(line)
                    continue
                if isinstance(message, dict):
                    self._queue.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self._eof.set()

    def _read_stderr(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stderr is not None
        try:
            for line in proc.stderr:
                self._note_diagnostic(line.rstrip())
        except (OSError, ValueError):
            pass

    def _note_diagnostic(self, line: str) -> None:
        with self._stderr_lock:
            self._stderr.append(line)
            if len(self._stderr) > STDERR_LINES:
                del self._stderr[: len(self._stderr) - STDERR_LINES]

    def stderr_tail(self, lines: int = 12) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr[-lines:])

    # ---------------------------------------------------------------- 请求/响应

    def _write(self, message: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise McpError(f"server {self.config.name} 没有在运行")
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except OSError as exc:
            raise McpError(f"给 server {self.config.name} 写请求失败：{exc}") from exc

    def _meta(self) -> dict:
        return {
            "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": "minicode", "version": __version__},
            "io.modelcontextprotocol/clientCapabilities": {},
        }

    def _request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        """发一个请求并等对应 id 的响应。返回 result；错误抛 McpError。"""
        request_id = self._next_id
        self._next_id += 1
        message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        payload = dict(params or {})
        if self.mode == "modern":
            payload["_meta"] = self._meta()
        if payload:
            message["params"] = payload
        self._write(message)

        deadline = time.perf_counter() + (timeout or self.config.timeout)
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise McpError(f"server {self.config.name} 在 {timeout or self.config.timeout:.0f}s 内没回应 {method}")
            try:
                incoming = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self._eof.is_set() and self.proc is not None and self.proc.poll() is not None:
                    raise McpError(self._died_message(method))
                continue
            if incoming.get("id") != request_id:
                # 通知（无 id）或者已经超时请求的迟到响应：丢掉
                continue
            if "error" in incoming:
                error = incoming.get("error") or {}
                raise _McpResponseError(error, method, self.config.name)
            result = incoming.get("result")
            if not isinstance(result, dict):
                raise McpError(f"server {self.config.name} 对 {method} 返回了非法结果：{incoming!r}"[:300])
            return result

    def _died_message(self, method: str) -> str:
        tail = self.stderr_tail()
        code = self.proc.returncode if self.proc is not None else None
        message = f"server {self.config.name} 在 {method} 期间退出（退出码 {code}）"
        if tail:
            message += f"；stderr 末尾：\n{tail}"
        return message

    # ---------------------------------------------------------------- 握手

    def _handshake(self) -> None:
        # 1) 现代协议：直接 tools/list，带 _meta。成功就没有握手。
        self.mode = "modern"
        self.protocol_version = MODERN_VERSION
        try:
            result = self._request("tools/list", {}, timeout=START_TIMEOUT)
        except _McpResponseError as exc:
            supported = _supported_versions_from_error(exc.error)
            legacy = [v for v in supported if v in LEGACY_VERSIONS]
            self._queue = queue.Queue()   # 丢掉可能残留的响应
            if not self._legacy_handshake(legacy):
                raise McpError(
                    f"server {self.config.name} 既不认现代协议 {MODERN_VERSION}，"
                    f"旧版握手也失败（它支持的版本：{', '.join(supported) or '未知'}）"
                ) from exc
            self._tools_cache = []
            return
        except McpError:
            # 连接层面失败（server 直接退出/没反应）：也试一次旧版
            self._queue = queue.Queue()
            if not self._legacy_handshake([]):
                raise
            self._tools_cache = []
            return
        self._tools_cache = list(result.get("tools") or [])

    def _legacy_handshake(self, preferred: list[str]) -> bool:
        candidates = list(dict.fromkeys([*preferred, *LEGACY_VERSIONS]))
        errors: list[str] = []
        for version in candidates:
            try:
                result = self._request(
                    "initialize",
                    {
                        "protocolVersion": version,
                        "capabilities": {},
                        "clientInfo": {"name": "minicode", "version": __version__},
                    },
                    timeout=START_TIMEOUT,
                )
            except (McpError, _McpResponseError) as exc:
                errors.append(f"{version}: {exc}")
                continue
            self.mode = "legacy"
            self.protocol_version = str(result.get("protocolVersion") or version)
            self.server_info = result.get("serverInfo") or {}
            caps = result.get("capabilities") or {}
            if "tools" not in caps:
                raise McpError(f"server {self.config.name} 的旧版握手没声明 tools 能力：{caps!r}"[:300])
            try:
                self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except McpError:
                pass
            self._tools_cache = []
            return True
        self._legacy_errors = errors
        return False

    def _ensure_started(self) -> None:
        if self.proc is None:
            self.start()

    # ---------------------------------------------------------------- 能力

    def list_tools(self) -> list[dict]:
        self._ensure_started()
        if self.mode == "modern":
            result = self._request("tools/list", {})
            return list(result.get("tools") or [])
        cached = getattr(self, "_tools_cache", None)
        if cached:
            return list(cached)
        result = self._request("tools/list", {})
        self._tools_cache = list(result.get("tools") or [])
        return list(self._tools_cache)

    def call_tool(self, name: str, arguments: dict | None = None) -> tuple[str, bool, float]:
        """调一个工具。返回 (文本, 是否错误, 耗时毫秒)。"""
        self._ensure_started()
        started = time.perf_counter()
        result = self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
        )
        self.last_call_ms = round((time.perf_counter() - started) * 1000, 1)
        result_type = result.get("resultType", "complete")
        if result_type != "complete":
            raise McpError(
                f"MCP 工具 {name} 返回了 {result_type}（需要客户端补充输入，暂不支持）"
            )
        text, is_error = render_tool_result(result)
        return text, is_error, self.last_call_ms


class _McpResponseError(McpError):
    """server 明确回了一个 JSON-RPC error。"""

    def __init__(self, error: dict, method: str, server: str) -> None:
        self.error = error
        code = error.get("code")
        message = error.get("message", "")
        super().__init__(f"server {server} 对 {method} 返回错误 [{code}] {message}"[:500])


# ------------------------------------------------------------------ 进程收尾


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """连同子进程一起杀掉（和 bash 工具踩过同一个坑：只杀父进程会留孤儿）。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def find_command(command: str) -> str | None:
    """`minicode mcp check` 用：确认命令找得到。"""
    if Path(command).exists():
        return command
    return shutil.which(command)
