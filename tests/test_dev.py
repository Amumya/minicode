"""`minicode dev` 相关的测试。

只测纯逻辑和"不炸"的部分：
    * 端口探测
    * 找便携版 Redis
    * ensure_redis 的三种分支（端口已监听 / 自行启动 / 均不可用）
不测真启动 —— 那属于集成测试，靠人跑一遍 `minicode dev`。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from minicode.dev import (
    DEFAULT_REDIS_PORT,
    ensure_redis,
    find_bundled_redis,
    find_bundled_redis_conf,
    port_open,
    redis_url,
    stop,
)


def test_port_open_on_closed_port():
    # 9 号端口（discard）几乎肯定没人听
    assert port_open("127.0.0.1", 9, timeout=0.2) is False


def test_port_open_on_listening_socket():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert port_open("127.0.0.1", port) is True
    finally:
        sock.close()


def test_find_bundled_redis_missing(tmp_path):
    assert find_bundled_redis(tmp_path) is None
    assert find_bundled_redis_conf(tmp_path) is None


def test_find_bundled_redis_found(tmp_path):
    exe = tmp_path / ".tools" / "redis" / "redis-server.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    assert find_bundled_redis(tmp_path) == exe


def test_find_bundled_redis_conf_variants(tmp_path):
    conf = tmp_path / ".tools" / "redis.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("port 6380", encoding="utf-8")
    assert find_bundled_redis_conf(tmp_path) == conf


def test_redis_url_shape():
    assert redis_url() == "redis://127.0.0.1:6380/0"
    assert redis_url(7000) == "redis://127.0.0.1:7000/0"


def test_ensure_redis_disabled():
    proc, url = ensure_redis(Path("."), enabled=False)
    assert proc is None and url is None


def test_ensure_redis_no_bundled_returns_none(tmp_path):
    """没有便携版的仓库（比如别人 clone 了但没放 .tools）应该优雅退化。"""
    proc, url = ensure_redis(tmp_path, port=1)
    assert proc is None and url is None


def test_ensure_redis_reuses_running(monkeypatch, tmp_path):
    """端口已经有人听 → 不启动新进程，直接用。"""
    import minicode.dev as dev

    monkeypatch.setattr(dev, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(dev, "redis_alive", lambda *a, **k: True)
    proc, url = ensure_redis(tmp_path)
    assert proc is None
    assert url == redis_url()


def test_ensure_redis_starts_bundled(monkeypatch, tmp_path):
    """有便携版、端口没开 → 应该启动它，并等到端口可用。"""
    import minicode.dev as dev

    exe = tmp_path / ".tools" / "redis" / "redis-server.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    calls = {"open": 0}

    def fake_open(*args, **kwargs):
        calls["open"] += 1
        return calls["open"] > 1        # 第一次 False（起来之前），之后 True

    started: list[list[str]] = []

    class FakeProc:
        pid = 4242
        def poll(self):
            return None

    def fake_popen(args, **kwargs):
        started.append(args)
        return FakeProc()

    monkeypatch.setattr(dev, "port_open", fake_open)
    monkeypatch.setattr(dev, "redis_alive", lambda *a, **k: True)
    monkeypatch.setattr(dev.subprocess, "Popen", fake_popen)

    proc, url = ensure_redis(tmp_path)
    assert proc is not None
    assert url == redis_url()
    assert started and str(exe) in started[0]


def test_stop_handles_none():
    stop(None)      # 不该抛异常


def test_stop_skips_finished_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    stop(proc)      # 已退出，不该再动手
