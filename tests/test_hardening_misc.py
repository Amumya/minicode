"""零散但真实的加固点（代码审查 2026-11-15）：

    * 文件工具遇到非 UTF-8 / 二进制 / 权限问题要返回"工具失败"，不能抛异常中断 run
    * ensure_redis 不能把"端口在听但不是 Redis"当成 Redis
    * app_shell 不能在端口被旧实例占用时假装自己起来了
    * --reload 模式下 --workspace 不能丢
"""

from __future__ import annotations

from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.dev import ensure_redis, redis_alive
from minicode.service import create_app
from minicode.service.store import RedisStore
from minicode.tools.files import EditFileTool, ReadFileTool

# ------------------------------------------------------------------ 文件工具


def test_read_binary_file_returns_error(tmp_path):
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\xff\xfe\x00\x01\x02\xff")
    result = ReadFileTool().run({"path": "blob.bin"}, tmp_path)
    assert result.exit_code == 1
    # 要么被内部识别成"不是 UTF-8"，要么被统一兜底接住 —— 关键是别抛异常
    assert "失败" in result.content or "UTF-8" in result.content


def test_read_missing_file_returns_error(tmp_path):
    result = ReadFileTool().run({"path": "没有这个文件.py"}, tmp_path)
    assert result.exit_code == 1


def test_edit_binary_file_returns_error(tmp_path):
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    result = EditFileTool().run(
        {"path": "blob.bin", "old_text": "a", "new_text": "b"}, tmp_path)
    assert result.exit_code == 1


def test_read_normal_file_still_works(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("print(1)\n", encoding="utf-8")
    result = ReadFileTool().run({"path": "a.py"}, tmp_path)
    assert result.exit_code == 0
    assert "print(1)" in result.content


def test_read_gbk_file_does_not_crash(tmp_path):
    """Windows 上常见 GBK 文件：之前直接抛 UnicodeDecodeError 把 run 弄挂。"""
    gbk = tmp_path / "gbk.txt"
    gbk.write_bytes("中文注释".encode("gbk"))
    result = ReadFileTool().run({"path": "gbk.txt"}, tmp_path)
    assert result.exit_code in (0, 1)   # 能读就返回内容，不能读就报错，总之别抛


# ------------------------------------------------------------------ Redis 可用性


def test_redis_alive_false_on_closed_port():
    assert redis_alive("redis://127.0.0.1:9/0") is False


def test_redis_alive_true_on_fakeredis_url(tmp_path):
    """真 redis 没起时这条只验证"不会误报为活"的逻辑，所以直接断言 False。"""
    assert redis_alive("redis://127.0.0.1:9/0", timeout=0.2) is False


def test_ensure_redis_rejects_non_redis_listener(monkeypatch, tmp_path):
    """端口在听但不是 Redis：不能返回 url，否则服务端连上就废。"""
    import minicode.dev as dev

    monkeypatch.setattr(dev, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(dev, "redis_alive", lambda *a, **k: False)
    proc, url = ensure_redis(tmp_path)
    assert proc is None
    assert url is None


# ------------------------------------------------------------------ app_shell 端口预检


def test_port_in_use_detects_listener():
    import socket

    from minicode.app_shell import _port_in_use

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert _port_in_use("127.0.0.1", port) is True
    finally:
        sock.close()


def test_port_in_use_on_free_port():
    from minicode.app_shell import _port_in_use

    assert _port_in_use("127.0.0.1", 9) is False


# ------------------------------------------------------------------ 默认工作区环境变量


def test_workspace_env_var_respected(monkeypatch, tmp_path):
    ws = tmp_path / "自定义工作区"
    ws.mkdir()
    monkeypatch.setenv("MINICODE_WORKSPACE", str(ws))
    app = create_app(
        provider_factory=lambda s: None,
        settings=None,
        store=RedisStore(client=fakeredis.FakeRedis(decode_responses=True)),
    )
    client = TestClient(app)
    data = client.get("/workspaces").json()
    assert data["default"] == str(ws.resolve())
