"""桌面壳的测试。

不起真浏览器、不起真服务 —— 只测"拼参数"和"找浏览器"这两件纯逻辑，
以及 dry-run 的输出形状。真窗口能不能弹出来只能人眼看，这里不假装能测。
"""

from pathlib import Path

from minicode.app_shell import (
    BROWSER_CANDIDATES,
    browser_args,
    find_browser,
    run_app,
    wait_for_health,
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


def test_dry_run_with_explicit_missing_browser(tmp_path, capsys):
    code = run_app(None, dry_run=True, browser=str(tmp_path / "nope.exe"))
    out = capsys.readouterr().out
    assert code == 0
    assert "没找到" in out
