#!/usr/bin/env python3
"""测试用的 MCP server（纯标准库，不依赖外网/第三方包）。

支持两代协议的形态：
    * 现代（2026-07-28）：请求 `_meta` 里带协议版本，无 initialize，响应带 resultType
    * 旧版（2025-11-25 及更早）：先 initialize 握手 → notifications/initialized

用环境变量控制行为，便于测"现代不通→回退旧版"的分支：
    FIXTURE_ERA=dual    （默认）两代都支持
    FIXTURE_ERA=legacy  只支持旧版：现代 tools/list 回 -32022 + supportedVersions
    FIXTURE_ERA=modern  只支持现代版：initialize 回 -32601

工具：
    echo(text)                 原样返回
    add(a, b)                  相加
    write_note(path, text)     写文件（用来测 allowlist 是否真的挡住了写操作）
    crash()                    直接退出（测崩溃隔离）
    slow(seconds)              睡一会（测超时）
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ERA = os.environ.get("FIXTURE_ERA", "dual")
SERVER_INFO = {"name": "fixture-echo", "version": "1.0.0"}
TOOLS = [
    {
        "name": "echo",
        "description": "把传入的文本原样返回",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "两个整数相加",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "write_note",
        "description": "把一个文本写到指定路径（测试 allowlist 用）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["path", "text"],
        },
    },
    {
        "name": "crash",
        "description": "直接退出进程（测试崩溃隔离）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow",
        "description": "睡一段时间再返回（测试超时）",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
    },
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(request_id, result: dict, modern: bool) -> None:
    if modern:
        result = {"resultType": "complete", **result}
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def error(request_id, code: int, message: str, data: dict | None = None) -> None:
    payload = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    send({"jsonrpc": "2.0", "id": request_id, "error": payload})


def call_tool(name: str, arguments: dict) -> tuple[str, bool]:
    if name == "echo":
        return str(arguments.get("text", "")), False
    if name == "add":
        a = int(arguments.get("a", 0))
        b = int(arguments.get("b", 0))
        return str(a + b), False
    if name == "write_note":
        path = Path(str(arguments.get("path", "")))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(arguments.get("text", "")), encoding="utf-8")
        except OSError as exc:
            return f"写入失败：{exc}", True
        return f"已写入 {path}", False
    if name == "crash":
        os._exit(9)
    if name == "slow":
        time.sleep(float(arguments.get("seconds", 0.5)))
        return "醒来了", False
    return f"没有这个工具：{name}", True


def main() -> int:
    modern = False
    initialized = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        request_id = message.get("id")

        # 通知：没有 id，不回应
        if request_id is None:
            if method == "notifications/initialized":
                initialized = True
            continue

        params = message.get("params") or {}
        meta = params.get("_meta") or {}
        version = meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None
        if version and ERA != "legacy":
            modern = True

        if method == "initialize":
            if ERA == "modern":
                error(request_id, -32601, "method not found: initialize (modern-only server)")
                continue
            requested = str(params.get("protocolVersion", "2025-06-18"))
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": requested,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            })
            continue

        if method == "tools/list":
            if ERA == "legacy" and not initialized:
                # 现代请求先到：明确告诉客户端"你该用旧版"
                error(request_id, -32022, "unsupported protocol version",
                      {"supportedVersions": ["2025-06-18"]})
                continue
            result(request_id, {"tools": TOOLS}, modern)
            continue

        if method == "tools/call":
            if ERA == "legacy" and not initialized:
                error(request_id, -32002, "server not initialized")
                continue
            name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            text, is_error = call_tool(name, dict(arguments))
            result(request_id, {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            }, modern)
            continue

        error(request_id, -32601, f"method not found: {method}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
