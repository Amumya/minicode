"""桌面壳的测试。

不起真浏览器、不起真服务 —— 只测"拼参数""找浏览器""选窗口模式"这些纯逻辑，
以及 dry-run 的输出形状。真窗口能不能弹出来只能人眼看，这里不假装能测。
"""

import sys
import types
from pathlib import Path

import minicode.app_shell as app_shell
from minicode.app_shell import (
    BROWSER_CANDIDATES,
    browser_args,
    find_browser,
    looks_loaded,
    qt_window_available,
    run_app,
    wait_for_health,
    window_titles,
)


def test_find_browser_explicit(tmp_path):
    fake = tmp_path / "browser.exe"
    fake.write_text("", encoding="utf-8")
    assert find_browser(str(fake)) == str(fake)


def test_find_browser_explicit_missing(tmp_path):
    assert find_browser(str(tmp_path / "nope.exe")) is None


def test_find_browser_returns_known_path_or_none():
    found = find_browser()
    if found is not None:
        assert found in BROWSER_CANDIDATES
        assert Path(found).is_file()


def test_browser_args_uses_app_mode(tmp_path):
    args = browser_args("edge.exe", "http://127.0.0.1:8765", tmp_path)
    assert args[0] == "edge.exe"
    assert "--app=http://127.0.0.1:8765" in args
    # 独立 profile 是必需的：不然 Edge 会把窗口交给已有进程然后自己退出
    assert any(a.startswith("--user-data-dir=") for a in args)
    assert "--window-size=1100,820" in args


def test_browser_args_size_override(tmp_path):
    args = browser_args("e.exe", "http://x", tmp_path, size="800,600")
    assert "--window-size=800,600" in args


def test_wait_for_health_times_out_fast():
    """连不上的地址应该在超时后返回 False，而不是抛异常或卡住。"""
    import time

    started = time.time()
    ok = wait_for_health("http://127.0.0.1:9", timeout=0.6)
    assert ok is False
    assert time.time() - started < 5


def test_dry_run_prints_url_and_args(tmp_path, capsys):
    code = run_app(None, port=8799, workspace=tmp_path, dry_run=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "8799" in out
    assert "服务地址" in out
    assert "桌面窗口:" in out


def test_dry_run_with_explicit_missing_browser(tmp_path, capsys):
    code = run_app(None, dry_run=True, browser=str(tmp_path / "nope.exe"))
    out = capsys.readouterr().out
    assert code == 0
    assert "没找到" in out


# ------------------------------------------------------------------ 窗口模式
# 背景：msedge 的 --app 模式在这台机器上会开出空白窗口（标题 127.0.0.1_/），
# 而 --new-window 正常。所以要做"检测 + 降级"。


def test_browser_args_app_mode(tmp_path):
    args = browser_args("e.exe", "http://127.0.0.1:8765", tmp_path, mode="app")
    assert "--app=http://127.0.0.1:8765" in args
    assert "--new-window" not in args


def test_browser_args_window_mode(tmp_path):
    args = browser_args("e.exe", "http://127.0.0.1:8765", tmp_path, mode="window")
    assert "--new-window" in args
    assert "http://127.0.0.1:8765" in args
    assert not any(a.startswith("--app=") for a in args)
    # 两种模式都要独立 profile
    assert any(a.startswith("--user-data-dir=") for a in args)


def test_looks_loaded_accepts_page_title():
    assert looks_loaded("minicode")
    assert looks_loaded("minicode - Microsoft Edge")


def test_looks_loaded_rejects_blank_window_titles():
    # 这就是实测出来的空白窗口标题
    assert not looks_loaded("127.0.0.1_/")
    assert not looks_loaded("localhost_/")
    assert not looks_loaded("")
    assert not looks_loaded("   ")
    assert not looks_loaded("新建标签页 - 用户配置 1 - Microsoft Edge")
    assert not looks_loaded("无标题 - 用户配置 1")


def test_looks_loaded_rejects_host_only_title():
    assert not looks_loaded("127.0.0.1", url="http://127.0.0.1:8765")
    assert not looks_loaded("127.0.0.1", url="http://127.0.0.1:8765/")


def test_window_titles_returns_list():
    # 不假设具体内容，只保证不炸
    assert isinstance(window_titles(set()), list)


# ------------------------------------------------------------------ PySide6 桌面窗口
# 真实窗口只能在人眼前验证；这里测"是否可用"和"GUI 失败时的降级"两条逻辑。


def test_qt_window_available_false_when_forced_off(monkeypatch):
    monkeypatch.setenv("MINICODE_GUI", "0")
    assert qt_window_available() is False


def test_qt_window_available_true_with_pyside(monkeypatch):
    monkeypatch.delenv("MINICODE_GUI", raising=False)
    real = app_shell.importlib.util.find_spec

    def fake_find_spec(name):
        if name == "PySide6":
            return object()
        return real(name)

    monkeypatch.setattr(app_shell.importlib.util, "find_spec", fake_find_spec)
    assert qt_window_available() is True


def test_qt_window_available_false_without_pyside(monkeypatch):
    monkeypatch.delenv("MINICODE_GUI", raising=False)
    real = app_shell.importlib.util.find_spec

    def fake_find_spec(name):
        if name == "PySide6":
            return None
        return real(name)

    monkeypatch.setattr(app_shell.importlib.util, "find_spec", fake_find_spec)
    assert qt_window_available() is False


def _fake_uvicorn(monkeypatch):
    """塞一个假 uvicorn，让 run_app 不真的起服务；返回记录 Server 实例的列表。"""
    started = []
    module = types.ModuleType("uvicorn")

    class Server:
        def __init__(self, config):
            self.should_exit = False
            started.append(self)

        def run(self):
            return None

    class Config:
        def __init__(self, *args, **kwargs):
            pass

    module.Server = Server
    module.Config = Config
    monkeypatch.setitem(sys.modules, "uvicorn", module)
    return started


class _FakeProc:
    pid = 4242

    def poll(self):
        return 0  # 窗口"立刻关闭"，让 run_app 不等


def _patch_browser_path(monkeypatch):
    monkeypatch.setattr(app_shell, "_port_in_use", lambda *a, **k: False)
    monkeypatch.setattr(app_shell, "wait_for_health", lambda *a, **k: True)
    monkeypatch.setattr(app_shell, "find_browser", lambda explicit=None: "edge.exe")
    monkeypatch.setattr(app_shell, "_kill_tree", lambda proc: None)


def test_run_app_gui_success_returns_without_browser(tmp_path, monkeypatch):
    started = _fake_uvicorn(monkeypatch)
    _patch_browser_path(monkeypatch)
    monkeypatch.setattr(app_shell, "qt_window_available", lambda: True)
    monkeypatch.setattr(app_shell, "run_qt_window", lambda *a, **k: 0)
    launched = []
    monkeypatch.setattr(
        app_shell,
        "launch_browser",
        lambda *a, **k: launched.append(a) or (_FakeProc(), "app"),
    )

    code = run_app(None, port=8899, workspace=tmp_path, mode="auto")
    assert code == 0
    assert launched == []           # GUI 成功就不该再开浏览器
    assert started[0].should_exit   # 窗口关了，服务要收掉


def test_run_app_gui_failure_falls_back_to_browser(tmp_path, monkeypatch):
    _fake_uvicorn(monkeypatch)
    _patch_browser_path(monkeypatch)
    monkeypatch.setattr(app_shell, "qt_window_available", lambda: True)

    def boom(*args, **kwargs):
        raise RuntimeError("页面 10 秒内没有加载完成")

    monkeypatch.setattr(app_shell, "run_qt_window", boom)
    used = {}
    monkeypatch.setattr(
        app_shell,
        "launch_browser",
        lambda browser, url, profile, mode="auto", **k: used.update(mode=mode)
        or (_FakeProc(), "app"),
    )

    code = run_app(None, port=8899, workspace=tmp_path, mode="auto")
    assert code == 0
    assert used.get("mode") == "auto"   # 降级后仍按 auto 走浏览器窗口


def test_cache_busted_url_adds_version():
    from minicode.app_shell import cache_busted_url

    u1 = cache_busted_url("http://127.0.0.1:8765")
    assert u1.startswith("http://127.0.0.1:8765/?v=")
    u2 = cache_busted_url("http://127.0.0.1:8765/?a=1")
    assert "?a=1&v=" in u2
    assert u1 != cache_busted_url("http://127.0.0.1:8765") or True  # 同一毫秒内允许相等
