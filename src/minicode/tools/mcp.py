"""把 MCP 工具适配成 minicode 的工具。

三层保护（缺一不可）：
    1. **配置层**：server 必须 `"enabled": true`，工具必须列进 `"allow"`（枚举白名单，
       默认未知一律拒绝）。没列进去的工具**不会注册**，模型根本看不到。
    2. **适配层**：`McpTool.run` 再查一次 allowlist —— 就算有人绕过注册直接拿到工具对象，
       调用也会被拒。这就是"注册 + 运行时"双重校验。
    3. **审计层**：返回的 ToolResult.meta 里带 `source=mcp / server / mcp_tool / duration_ms`，
       主循环会把它写进 tool_call 事件，界面上能看出"这个工具来自哪个 MCP server"。

为什么 MCP 工具不走 policy（bash 那种分词判命令）：
    外部工具的参数语义对我们是不透明的，没法像 shell 命令那样判定危险与否。
    所以把"信任"显式化：允许哪个 server、允许哪个具体工具，写进配置，默认全拒。
"""

from __future__ import annotations

import time
from pathlib import Path

from minicode.mcp import (
    McpClient,
    McpError,
    ServerConfig,
    load_servers,
    MAX_RESULT_CHARS,
)
from minicode.tools.base import ToolResult

# 工具名前缀：`mcp__<server>__<tool>`
MCP_PREFIX = "mcp__"


def tool_name(server: str, tool: str) -> str:
    return f"{MCP_PREFIX}{server}__{tool}"


def parse_name(name: str) -> tuple[str, str] | None:
    """从 `mcp__server__tool` 里拆出 (server, tool)。不是 MCP 工具返回 None。"""
    if not name.startswith(MCP_PREFIX):
        return None
    rest = name[len(MCP_PREFIX):]
    if "__" not in rest:
        return (rest, "")
    server, _, tool = rest.partition("__")
    return (server, tool)


class McpTool:
    """一个 MCP 工具。name 是 `mcp__<server>__<tool>`，避免和内置工具重名。"""

    def __init__(self, client: McpClient, info: dict, allowed: list[str]) -> None:
        self.client = client
        self.server = client.config.name
        self.tool = str(info.get("name", ""))
        self.allowed = list(allowed)
        self.name = tool_name(self.server, self.tool)
        description = str(info.get("description") or "").strip()
        self.description = f"[MCP:{self.server}] {description or self.tool}"
        schema = info.get("inputSchema")
        self.input_schema = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}

    # ---------------------------------------------------------------- 运行

    def run(self, args: dict, cwd: Path) -> ToolResult:
        started = time.perf_counter()
        # 2) 适配层再查一次白名单
        if self.tool not in self.allowed:
            return ToolResult(
                f"[denied: mcp_allowlist] 工具 {self.tool} 不在 server {self.server} 的 allow 列表里",
                exit_code=1,
                meta={"source": "mcp", "server": self.server, "mcp_tool": self.tool,
                      "denied": "mcp_allowlist"},
            )
        try:
            text, is_error, duration_ms = self.client.call_tool(self.tool, args)
        except McpError as exc:
            return ToolResult(
                f"MCP 调用失败：{exc}",
                exit_code=1,
                meta={"source": "mcp", "server": self.server, "mcp_tool": self.tool,
                      "duration_ms": round((time.perf_counter() - started) * 1000, 1)},
            )
        return ToolResult(
            text,
            exit_code=1 if is_error else 0,
            meta={
                "source": "mcp",
                "server": self.server,
                "mcp_tool": self.tool,
                "duration_ms": duration_ms,
                "mode": self.client.mode,
                "protocol_version": self.client.protocol_version,
            },
        )


class McpSession:
    """一批 MCP server 的连接。用完 close()。

    只连 `enabled` 的 server；每个 server 只暴露 `allow` 里列出的工具。
    """

    def __init__(self) -> None:
        self.clients: dict[str, McpClient] = {}
        self.tools: list[McpTool] = []
        self.report: dict = {"servers": [], "skipped": [], "warnings": []}

    # ---------------------------------------------------------------- 建立

    def connect(self, configs: list[ServerConfig], workspace: Path) -> "McpSession":
        for config in configs:
            if not config.enabled:
                self.report["skipped"].append({
                    "server": config.name,
                    "reason": "enabled=false（默认不启用）",
                })
                continue
            client = McpClient(config, cwd=workspace)
            try:
                client.start()
                raw_tools = client.list_tools()
            except McpError as exc:
                client.close()
                self.report["skipped"].append({"server": config.name, "reason": str(exc)})
                self.report["warnings"].append(f"server {config.name} 连接失败：{exc}")
                continue

            by_name = {str(t.get("name", "")): t for t in raw_tools if isinstance(t, dict)}
            allowed_present = [name for name in config.allow if name in by_name]
            missing = [name for name in config.allow if name not in by_name]
            for name in missing:
                self.report["warnings"].append(
                    f"server {config.name} 的 allow 里列了 {name}，但 server 没有这个工具"
                )
            for name in allowed_present:
                self.tools.append(McpTool(client, by_name[name], config.allow))
            self.clients[config.name] = client
            self.report["servers"].append({
                "server": config.name,
                "mode": client.mode,
                "protocol_version": client.protocol_version,
                "server_info": client.server_info,
                "startup_ms": client.startup_ms,
                "tools_total": len(raw_tools),
                "tools_allowed": allowed_present,
                "tools_not_allowed": [
                    str(t.get("name", "")) for t in raw_tools
                    if isinstance(t, dict) and str(t.get("name", "")) not in config.allow
                ],
            })
        return self

    # ---------------------------------------------------------------- 收尾

    def close(self) -> None:
        for client in self.clients.values():
            try:
                client.close()
            except OSError:
                pass
        self.clients.clear()

    def __enter__(self) -> "McpSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def connect_mcp(workspace: Path) -> McpSession:
    """按 workspace 里的配置连一批 server。配置不存在就是空 session。"""
    configs = load_servers(workspace)
    return McpSession().connect(configs, workspace)


def schema_stats(tools: list[dict]) -> dict:
    """统计 tools/list 的 schema 大小，用来估算每轮多花多少 token。"""
    import json

    if not tools:
        return {"count": 0, "chars": 0, "est_tokens": 0}
    payload = json.dumps(tools, ensure_ascii=False)
    return {
        "count": len(tools),
        "chars": len(payload),
        # 粗估：中英文混排约 1 token ≈ 1.6 字符（只用于量级，不当精确值）
        "est_tokens": int(len(payload) / 1.6),
    }


__all__ = [
    "McpTool",
    "McpSession",
    "connect_mcp",
    "parse_name",
    "tool_name",
    "schema_stats",
    "MAX_RESULT_CHARS",
]
