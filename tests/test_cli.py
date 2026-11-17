"""命令行的测试。

用假的 provider 顶替真模型，所以整条命令行路径（解析参数 → 组装 →
跑主循环 → 打印 → 退出码）都能离线测。
"""

import json

import pytest

from minicode.cli import main
from minicode.provider import ModelReply, ToolCall


class FakeProvider:
    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, system, messages, tools):
        return self._replies.pop(0)


def _factory(replies):
    def make(settings):
        return FakeProvider(replies)
    return make


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "minicode" in capsys.readouterr().out


def test_run_without_task_fails():
    with pytest.raises(SystemExit) as exc:
        main(["run"])
    assert exc.value.code != 0


def test_run_ok(tmp_path, capsys):
    factory = _factory([
        ModelReply(tool_calls=[ToolCall(id="t1", name="bash",
                                        arguments={"command": "echo hi"})],
                   stop_reason="tool_use", usage={"input_tokens": 5, "output_tokens": 2}),
        ModelReply(text="完事了", stop_reason="end_turn",
                   usage={"input_tokens": 1, "output_tokens": 1}),
    ])
    code = main(["run", "打个招呼", "--workspace", str(tmp_path),
                 "--max-steps", "3"], provider_factory=factory)
    out = capsys.readouterr().out
    assert code == 0
    assert "完事了" in out
    assert "tool_call" in out


def test_run_json_output(tmp_path, capsys):
    factory = _factory([
        ModelReply(text="ok", stop_reason="end_turn",
                   usage={"input_tokens": 1, "output_tokens": 1}),
    ])
    code = main(["run", "随便", "--workspace", str(tmp_path), "--json"],
                provider_factory=factory)
    out = capsys.readouterr().out
    assert code == 0
    data = json.loads(out)
    assert data["result"]["ok"] is True
    assert data["result"]["final_text"] == "ok"


def test_run_denied_command_reports_denial(tmp_path, capsys):
    factory = _factory([
        ModelReply(tool_calls=[ToolCall(id="t1", name="bash",
                                        arguments={"command": "git reset --hard"})],
                   stop_reason="tool_use"),
        ModelReply(text="被拒了", stop_reason="end_turn"),
    ])
    code = main(["run", "回滚", "--workspace", str(tmp_path), "--json"],
                provider_factory=factory)
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["result"]["denied"] == 1


def test_no_api_key_returns_2(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    code = main(["run", "x", "--workspace", str(tmp_path)])
    assert code == 2
    assert "API key" in capsys.readouterr().err

def test_dev_no_redis_does_not_crash(monkeypatch, tmp_path):
    """--no-redis 路径曾经 NameError：cli.py 里用了 os.environ 却没 import os，一跑就崩。"""
    from argparse import Namespace

    import uvicorn

    import minicode.cli as cli
    import minicode.dev as dev
    import minicode.service as service

    monkeypatch.setenv("MINICODE_REDIS_URL", "redis://127.0.0.1:6380/0")
    monkeypatch.setattr(dev, "build_frontend", lambda root: None)
    monkeypatch.setattr(dev, "ensure_redis", lambda root, enabled=True: (None, None))
    monkeypatch.setattr(dev, "apply_env", lambda url: None)
    monkeypatch.setattr(service, "create_app", lambda **kwargs: object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    args = Namespace(
        host="127.0.0.1", port=8999, workspace=tmp_path, token=None,
        open=False, reload=False, build=False, no_redis=True, mode="auto",
    )
    assert cli.cmd_dev(args) == 0
    # --no-redis 必须把外部设置的 Redis 地址清掉，否则会静默连上 Redis
    import os
    assert "MINICODE_REDIS_URL" not in os.environ

def test_run_prints_thinking_with_step_and_newlines(tmp_path, capsys):
    """thinking 事件按"第 N 步"输出，多行内容原样换行。"""
    factory = _factory([
        ModelReply(
            tool_calls=[ToolCall(id="t1", name="bash", arguments={"command": "echo hi"})],
            stop_reason="tool_use",
            thinking=[{
                "type": "thinking",
                "thinking": "第一行思考\n第二行思考",
                "signature": "sig-1",
            }],
        ),
        ModelReply(text="完事了", stop_reason="end_turn"),
    ])
    code = main(
        ["run", "打个招呼", "--workspace", str(tmp_path), "--max-steps", "3"],
        provider_factory=factory,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "thinking" in out
    assert "第 1 步" in out
    assert "    第一行思考" in out
    assert "    第二行思考" in out

def test_cmd_app_defaults_to_persistent_redis(monkeypatch, tmp_path):
    """minicode app 默认启动仓库里的便携 Redis，历史不再丢。"""
    from argparse import Namespace

    import minicode.app_shell as app_shell
    import minicode.cli as cli
    import minicode.dev as dev
    import minicode.service as service

    calls = {}
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("MINICODE_REDIS_URL", raising=False)
    monkeypatch.setattr(dev, "ensure_redis", lambda root: (object(), "redis://127.0.0.1:6380/0"))
    monkeypatch.setattr(dev, "apply_env", lambda url: calls.setdefault("applied", url))
    monkeypatch.setattr(dev, "stop", lambda proc: calls.setdefault("stopped", True))
    monkeypatch.setattr(service, "create_app", lambda **kw: object())
    monkeypatch.setattr(app_shell, "run_app", lambda *a, **k: 0)

    args = Namespace(host="127.0.0.1", port=8765, workspace=tmp_path, browser=None,
                     mode="auto", dry_run=False, no_wait=False, no_redis=False)
    assert cli.cmd_app(args) == 0
    assert calls["applied"] == "redis://127.0.0.1:6380/0"
    assert calls["stopped"] is True


def test_cmd_serve_defaults_to_persistent_redis(monkeypatch, tmp_path):
    from argparse import Namespace
    import sys
    import types

    import minicode.cli as cli
    import minicode.dev as dev
    import minicode.service as service

    calls = {}
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda *a, **k: calls.setdefault("ran", True)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("MINICODE_REDIS_URL", raising=False)
    monkeypatch.setattr(dev, "ensure_redis", lambda root: (object(), "redis://127.0.0.1:6380/0"))
    monkeypatch.setattr(dev, "apply_env", lambda url: calls.setdefault("applied", url))
    monkeypatch.setattr(dev, "stop", lambda proc: calls.setdefault("stopped", True))
    monkeypatch.setattr(service, "create_app", lambda **kw: object())

    args = Namespace(host="127.0.0.1", port=8080, workspace=tmp_path, token=None, no_redis=False)
    assert cli.cmd_serve(args) == 0
    assert calls["applied"] == "redis://127.0.0.1:6380/0"
    assert calls["ran"] is True
    assert calls["stopped"] is True
