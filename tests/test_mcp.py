"""MCP 接入：配置、协议两代、工具适配、白名单、超时、崩溃隔离。

为什么这些测试值钱：
    * MCP 的协议有**两代**（2026-07-28 现代版无握手 / 2025-11-25 及更早是 initialize 握手），
      回退逻辑错了，对接现成 server 时就是"连不上但说不清为什么"
    * 外部 server 的输出/存活都不可控，超时和崩溃隔离是必须锁住的行为
    * allowlist 是治理边界：既要"没列的工具不注册"，也要"运行时再拦一次"

全部用自写的 stdlib fixture server（tests/fixtures/mcp_echo_server.py），不依赖外网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from minicode.mcp import (
    MAX_RESULT_CHARS,
    McpClient,
    McpError,
    ServerConfig,
    load_servers,
    render_tool_result,
)
from minicode.tools.mcp import (
    McpSession,
    McpTool,
    connect_mcp,
    parse_name,
    schema_stats,
    tool_name,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")
PY = sys.executable


def make_config(**kw) -> ServerConfig:
    base = dict(name="fixture", command=PY, args=[FIXTURE], enabled=True, allow=["echo"])
    base.update(kw)
    return ServerConfig(**base)


def write_config(workspace: Path, servers: list[dict]) -> Path:
    path = workspace / ".minicode" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return path


@pytest.fixture()
def client():
    c = McpClient(make_config(), cwd=Path.cwd())
    c.start()
    yield c
    c.close()


# ------------------------------------------------------------------ 配置


def test_missing_config_is_empty(tmp_path):
    assert load_servers(tmp_path) == []


def test_config_parsed(tmp_path):
    write_config(tmp_path, [{
        "name": "fixture", "command": PY, "args": [FIXTURE],
        "enabled": True, "allow": ["echo", "add"], "timeout": 5,
    }])
    servers = load_servers(tmp_path)
    assert len(servers) == 1
    server = servers[0]
    assert server.name == "fixture"
    assert server.enabled is True
    assert server.allow == ["echo", "add"]
    assert server.timeout == 5.0


def test_config_defaults_to_disabled(tmp_path):
    """不写 enabled 就是关的 —— 配置里出现一个 server 不该等于"允许它跑"。"""
    write_config(tmp_path, [{"name": "fixture", "command": PY, "args": [FIXTURE]}])
    server = load_servers(tmp_path)[0]
    assert server.enabled is False
    assert server.allow == []


@pytest.mark.parametrize("bad", [
    {"servers": "nope"},
    {"servers": [{"name": "", "command": "x"}]},
    {"servers": [{"name": "a", "command": ""}]},
    {"servers": [{"name": "a", "command": "x"}, {"name": "a", "command": "y"}]},
    {"servers": [{"name": "a", "command": "x", "allow": "echo"}]},
    {"servers": [{"name": "a", "command": "x", "timeout": "soon"}]},
])
def test_bad_config_raises(tmp_path, bad):
    path = tmp_path / ".minicode" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(McpError):
        load_servers(tmp_path)


# ------------------------------------------------------------------ 协议两代


def test_modern_protocol_no_handshake(client):
    """最新的 2026-07-28 没有 initialize 握手：直接 tools/list 就通了。"""
    assert client.mode == "modern"
    assert client.protocol_version == "2026-07-28"
    assert client.startup_ms >= 0
    tools = client.list_tools()
    assert {t["name"] for t in tools} >= {"echo", "add", "write_note"}


def test_legacy_fallback_when_server_is_old(tmp_path):
    """只支持旧版的 server：现代请求被 -32022 拒掉后，要能回退到 initialize 握手。"""
    config = make_config(env={"FIXTURE_ERA": "legacy"})
    c = McpClient(config, cwd=tmp_path)
    try:
        c.start()
        assert c.mode == "legacy"
        assert c.protocol_version in ("2025-11-25", "2025-06-18", "2024-11-05")
        tools = c.list_tools()
        assert {t["name"] for t in tools} >= {"echo"}
        text, is_error, _ = c.call_tool("echo", {"text": "legacy"})
        assert text == "legacy" and is_error is False
    finally:
        c.close()


def test_modern_only_server(tmp_path):
    config = make_config(env={"FIXTURE_ERA": "modern"})
    c = McpClient(config, cwd=tmp_path)
    try:
        c.start()
        assert c.mode == "modern"
        assert c.protocol_version == "2026-07-28"
    finally:
        c.close()


def test_call_tool_echo(client):
    text, is_error, ms = client.call_tool("echo", {"text": "你好"})
    assert text == "你好"
    assert is_error is False
    assert ms >= 0


def test_unknown_tool_returns_error_content(client):
    """server 说工具不存在时，模型要看到 isError=true 而不是异常。"""
    text, is_error, _ = client.call_tool("nope", {})
    assert is_error is True
    assert "没有这个工具" in text


# ------------------------------------------------------------------ 治理：白名单


def test_session_only_exposes_allowlisted_tools(tmp_path):
    configs = [make_config(allow=["echo"])]
    session = McpSession().connect(configs, tmp_path)
    try:
        names = [tool.name for tool in session.tools]
        assert names == ["mcp__fixture__echo"]
        report = session.report["servers"][0]
        assert report["tools_allowed"] == ["echo"]
        assert "write_note" in report["tools_not_allowed"]
    finally:
        session.close()


def test_adapter_runtime_second_check(tmp_path):
    """就算绕过注册直接拿到工具对象，运行时也要再拦一次（双重校验）。"""
    c = McpClient(make_config(allow=["echo"]), cwd=tmp_path)
    c.start()
    try:
        info = next(t for t in c.list_tools() if t["name"] == "write_note")
        tool = McpTool(c, info, allowed=["echo"])       # allow 里没有 write_note
        target = tmp_path / "should_not_exist.txt"
        result = tool.run({"path": str(target), "text": "x"}, tmp_path)
        assert result.exit_code == 1
        assert "mcp_allowlist" in result.content
        assert not target.exists(), "被白名单拒绝的调用不该真的写文件"
    finally:
        c.close()


def test_allowed_tool_calls_server(tmp_path):
    c = McpClient(make_config(), cwd=tmp_path)
    c.start()
    try:
        info = next(t for t in c.list_tools() if t["name"] == "add")
        tool = McpTool(c, info, allowed=["add"])
        result = tool.run({"a": 2, "b": 40}, tmp_path)
        assert result.exit_code == 0
        assert result.content == "42"
        assert result.meta["source"] == "mcp"
        assert result.meta["server"] == "fixture"
        assert result.meta["mcp_tool"] == "add"
        assert result.meta["duration_ms"] >= 0
    finally:
        c.close()


def test_naming_and_parsing():
    assert tool_name("report", "get_stat") == "mcp__report__get_stat"
    assert parse_name("mcp__report__get_stat") == ("report", "get_stat")
    assert parse_name("bash") is None
    assert parse_name("mcp__only") == ("only", "")


def test_disabled_server_is_skipped(tmp_path):
    session = McpSession().connect([make_config(enabled=False)], tmp_path)
    try:
        assert session.tools == []
        assert session.report["skipped"][0]["server"] == "fixture"
    finally:
        session.close()


def test_connect_mcp_reads_workspace_config(tmp_path):
    write_config(tmp_path, [{
        "name": "fixture", "command": PY, "args": [FIXTURE],
        "enabled": True, "allow": ["echo"],
    }])
    session = connect_mcp(tmp_path)
    try:
        assert [t.name for t in session.tools] == ["mcp__fixture__echo"]
    finally:
        session.close()


# ------------------------------------------------------------------ 超时与崩溃隔离


def test_call_timeout(tmp_path):
    config = make_config(allow=["slow"], timeout=1.0)
    c = McpClient(config, cwd=tmp_path)
    c.start()
    try:
        with pytest.raises(McpError) as excinfo:
            c.call_tool("slow", {"seconds": 5})
        assert "没回应" in str(excinfo.value)
    finally:
        c.close()


def test_server_crash_is_isolated(tmp_path):
    """工具把 server 弄崩了：返回错误，不抛到主循环外面。"""
    config = make_config(allow=["crash", "echo"], timeout=5.0)
    c = McpClient(config, cwd=tmp_path)
    c.start()
    try:
        info = next(t for t in c.list_tools() if t["name"] == "crash")
        tool = McpTool(c, info, allowed=["crash", "echo"])
        result = tool.run({}, tmp_path)
        assert result.exit_code == 1
        assert "MCP 调用失败" in result.content
        # 再调一次：server 已经死了，也应该是错误而不是异常外溢
        again = tool.run({}, tmp_path)
        assert again.exit_code == 1
    finally:
        c.close()


# ------------------------------------------------------------------ 结果渲染


def test_render_text_and_error():
    text, is_error = render_tool_result({
        "content": [{"type": "text", "text": "ok"}], "isError": False,
    })
    assert (text, is_error) == ("ok", False)

    text, is_error = render_tool_result({
        "content": [{"type": "text", "text": "boom"}], "isError": True,
    })
    assert is_error is True


def test_render_handles_non_text():
    text, _ = render_tool_result({
        "content": [
            {"type": "image", "mimeType": "image/png", "data": "…"},
            {"type": "resource", "resource": {"uri": "file:///x"}},
        ],
    })
    assert "图片" in text and "资源" in text


def test_render_truncates_huge_output():
    huge = "x" * (MAX_RESULT_CHARS + 500)
    text, _ = render_tool_result({"content": [{"type": "text", "text": huge}]})
    assert len(text) < MAX_RESULT_CHARS + 200
    assert "截断" in text


def test_schema_stats():
    stats = schema_stats([{"name": "echo", "description": "回显"}])
    assert stats["count"] == 1
    assert stats["chars"] > 0
    assert stats["est_tokens"] > 0


# ------------------------------------------------------------------ 提示词与主循环


def test_prompt_mentions_mcp_servers():
    from minicode.prompts import default_system

    text = default_system(mcp_servers=["fixture", "report"])
    assert "MCP" in text and "fixture" in text and "report" in text
    assert "mcp__" in text

    assert "MCP" not in default_system()


def test_loop_can_call_mcp_tool(tmp_path):
    """主循环拿到注册好的 MCP 工具后，能像内置工具一样调用。"""
    from minicode.config import Settings
    from minicode.loop import run
    from minicode.provider import ModelReply, ToolCall
    from minicode.tools import build_registry

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        def complete(self, system, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelReply(
                    tool_calls=[ToolCall(id="t1", name="mcp__fixture__add",
                                         arguments={"a": 2, "b": 40})],
                    stop_reason="tool_use",
                )
            return ModelReply(text="42", stop_reason="end_turn")

    session = McpSession().connect([make_config(allow=["add"])], tmp_path)
    try:
        registry = build_registry(extra=session.tools)
        assert registry.get("mcp__fixture__add") is not None
        rec_events = []
        from minicode.events import Recorder

        result = run(
            "算 2+40",
            provider=FakeProvider(),
            tools=registry,
            settings=Settings(api_key="fake", model="m", max_steps=3),
            workspace=tmp_path,
            recorder=Recorder(on_event=lambda ev: rec_events.append(ev)),
        )
        assert result.ok is True
        assert result.tool_calls == 1
        calls = [ev for ev in rec_events if ev.kind == "tool_call"]
        assert calls and calls[0].data["meta"]["source"] == "mcp"
    finally:
        session.close()
