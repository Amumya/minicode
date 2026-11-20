"""流式输出与工具输出预览的测试。

为什么要有流式：非流式时界面要等整段回答生成完才显示，看起来像"卡住然后一次性跳出来"。
这里的验证点是：
    * provider 支持 stream 时，文字增量会变成 `assistant_delta` 事件
    * 增量要**合并**（每个 token 一条事件会把事件表撑爆）
    * provider 不支持 stream 时自动退回 complete（假 provider 不用改）
    * 工具调用事件带输出预览，界面不用点开事件流就能看到命令结果
"""

from __future__ import annotations

from pathlib import Path

from minicode.config import Settings
from minicode.events import Recorder
from minicode.loop import run
from minicode.provider import ModelReply, ToolCall
from minicode.tools import default_registry


class StreamingProvider:
    """模拟流式 provider：把回答拆成若干片回调出去。"""

    def __init__(self, pieces: list[str]):
        self.pieces = pieces

    def stream(self, system, messages, tools, on_text):
        for piece in self.pieces:
            on_text(piece)
        return ModelReply(text="".join(self.pieces), stop_reason="end_turn",
                          usage={"input_tokens": 5, "output_tokens": 3})

    def complete(self, system, messages, tools):
        raise AssertionError("有 stream 时不该再调 complete")


class PlainProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, tools):
        self.calls += 1
        return ModelReply(text="普通回答", stop_reason="end_turn")


def _settings(max_steps=3):
    return Settings(api_key="fake", model="m", max_steps=max_steps)


def _deltas(rec: Recorder) -> list[str]:
    return [e.data["text"] for e in rec.events if e.kind == "assistant_delta"]


def test_streaming_provider_emits_delta_events(tmp_path):
    provider = StreamingProvider(["你", "好", "，", "世", "界"])
    rec = Recorder()
    result = run("打个招呼", provider=provider, tools=default_registry(),
                 settings=_settings(), workspace=tmp_path, recorder=rec)

    assert result.ok
    assert result.final_text == "你好，世界"
    deltas = _deltas(rec)
    assert deltas, "应该产生 assistant_delta 事件"
    assert "".join(deltas) == "你好，世界", f"增量拼起来要等于完整回答，实际 {deltas}"


def test_deltas_are_coalesced(tmp_path):
    """每个 token 一条事件太吵，必须合并。"""
    pieces = [f"第{i}段" for i in range(200)]
    provider = StreamingProvider(pieces)
    rec = Recorder()
    run("x", provider=provider, tools=default_registry(),
        settings=_settings(), workspace=tmp_path, recorder=rec)

    deltas = _deltas(rec)
    assert "".join(deltas) == "".join(pieces)
    assert len(deltas) < len(pieces), f"200 片应该被合并，实际发了 {len(deltas)} 条"


def test_plain_provider_still_works(tmp_path):
    """没有 stream 方法的 provider 要能退回 complete。"""
    provider = PlainProvider()
    rec = Recorder()
    result = run("x", provider=provider, tools=default_registry(),
                 settings=_settings(), workspace=tmp_path, recorder=rec)
    assert result.ok
    assert provider.calls == 1
    assert _deltas(rec) == [], "非流式 provider 不该有增量事件"


def test_tool_call_event_has_output_preview(tmp_path):
    class OneTool:
        def __init__(self):
            self.n = 0

        def complete(self, system, messages, tools):
            self.n += 1
            if self.n == 1:
                return ModelReply(tool_calls=[ToolCall(id="t1", name="bash",
                                  arguments={"command": "echo 你好"})],
                                  stop_reason="tool_use")
            return ModelReply(text="完", stop_reason="end_turn")

    rec = Recorder()
    run("x", provider=OneTool(), tools=default_registry(),
        settings=_settings(), workspace=tmp_path, recorder=rec)

    tool_events = [e for e in rec.events if e.kind == "tool_call"]
    assert tool_events, "应该有一次工具调用"
    data = tool_events[0].data
    assert data["args"]["command"] == "echo 你好", "命令原文要留在事件里（界面要显示）"
    assert "output" in data and "你好" in data["output"], \
        f"事件里要有输出预览，实际 {data.get('output')!r}"
    assert len(data["output"]) <= 400



class TooLongThenOk:
    """第一次流式报上下文过长，第二次正常返回。"""

    def __init__(self):
        self.calls = 0

    def stream(self, system, messages, tools, on_text):
        self.calls += 1
        if self.calls == 1:
            on_text("A" * 200)
            raise ValueError("prompt_too_long: context length exceeded")
        on_text("完")
        on_text("整")
        return ModelReply(text="完整", stop_reason="end_turn",
                          usage={"input_tokens": 1, "output_tokens": 1})


def test_reactive_retry_emits_stream_reset(tmp_path):
    from minicode.compact import CompactConfig, Compactor

    provider = TooLongThenOk()
    comp = Compactor(tmp_path, CompactConfig(), summarize=lambda prompt: "摘要")
    rec = Recorder()
    result = run(
        "流式重试",
        provider=provider,
        tools=default_registry(),
        settings=Settings(api_key="fake", model="m", max_steps=3),
        workspace=tmp_path,
        recorder=rec,
        compactor=comp,
    )
    assert result.ok
    assert rec.count("stream_reset") == 1
    deltas = "".join(str(e.data.get("text", "")) for e in rec.events
                     if e.kind == "assistant_delta")
    # 后端事件里第一次失败的增量仍在（轨迹要完整记录），
    # 但 stream_reset 事件让前端在合并/实时渲染时把它作废。
    assert "完整" in deltas
