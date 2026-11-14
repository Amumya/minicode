"""桌面壳：起本地服务，再用浏览器"应用模式"开一个独立窗口。

为什么这么做而不是 Electron/pywebview：

    Electron 要下载约 100MB，这台机器的代理不稳；
    Tauri 要 Rust（没装）；pywebview 要 pythonnet，多一层原生依赖。
    而 `msedge --app=URL` 的本机条件已经具备（Edge 和 WebView2 都装好了），
    零新依赖，三十行就能跑。

它**不是另一个应用**：uvicorn 还在，`curl` 照样能看事件流。
这很重要 —— 这个项目的一部分价值是"可被检查"，桌面壳不该把这点藏起来。

一个细节：必须给浏览器一个**独立 profile**（`--user-data-dir`）。
否则 Edge 会把窗口交给已有进程、自己立刻退出，我们的循环就会以为"窗口关了"
然后把服务关掉 —— 而窗口还开着。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# 常见的 Edge / Chrome 安装位置（Windows）
BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_browser(explicit: str | None = None) -> str | None:
    """找一个能用应用模式的浏览器。找不到返回 None（调用方退化成"只打印地址"）。"""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def browser_args(browser: str, url: str, profile_dir: Path,
                 size: str = "1100,820") -> list[str]:
    """拼出"应用模式"窗口的参数。

    独立 profile 是必须的（见模块开头）；window-size 只是让第一次打开不像手机页面。
    """
    return [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        f"--window-size={size}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def wait_for_health(base_url: str, timeout: float = 15.0) -> bool:
    """等后端起来。返回是否成功 —— 失败时调用方应该放弃开窗口并报错。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(0.2)
    return False


def run_app(
    app,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    workspace: Path | None = None,
    browser: str | None = None,
    dry_run: bool = False,
    wait_window: bool = True,
) -> int:
    """起服务 + 开窗口。窗口关掉之后把服务也收掉。"""
    url = f"http://{host}:{port}"
    profile = (Path(workspace) if workspace else Path.cwd()) / ".minicode" / "edge-profile"
    found = find_browser(browser)

    if dry_run:
        print(f"  服务地址: {url}")
        print(f"  浏览器  : {found or '(没找到，会退化成只打印地址)'}")
        if found:
            print(f"  启动参数: {' '.join(browser_args(found, url, profile))}")
        return 0

    try:
        import uvicorn
    except ImportError:
        print('没装 uvicorn。装法: pip install -e ".[serve]"', file=sys.stderr)
        return 2

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    thread.start()

    if not wait_for_health(url):
        print("后端没起来，放弃开窗口", file=sys.stderr)
        server.should_exit = True
        return 1

    print(f"服务已就绪: {url}")

    if found is None:
        print("没找到 Edge/Chrome，请手动打开上面的地址。按 Ctrl+C 结束。")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        thread.join(timeout=5)
        return 0

    profile.mkdir(parents=True, exist_ok=True)
    print(f"打开窗口（关闭窗口即退出）：{found}")
    proc = subprocess.Popen(browser_args(found, url, profile))
    try:
        if wait_window:
            proc.wait()
        else:
            print("（--no-wait：窗口在后台，服务继续跑，按 Ctrl+C 结束）")
            while thread.is_alive():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
        server.should_exit = True
        thread.join(timeout=5)
    print("已退出")
    return 0
