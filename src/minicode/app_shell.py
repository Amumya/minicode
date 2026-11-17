"""桌面壳：起本地服务，再开一个窗口。

两种窗口，按可用性降级：

    1. **PySide6 原生窗口**（可选依赖 `.[desktop]`）：QMainWindow + QWebEngineView。
       直接依赖 Python 轮子，不碰 pythonnet/.NET。QtWebEngine 的 Chromium 沙箱
       在部分环境里起不来，启动前默认补 `--no-sandbox`（窗口只加载 127.0.0.1
       上的自家页面）；用户已设置 `QTWEBENGINE_CHROMIUM_FLAGS` 时不覆盖。
       页面 10 秒没加载出来就关窗并降级。

    2. **浏览器 `--app` 窗口**：没装 PySide6 或 GUI 起不来时的兜底，零依赖。
       踩过的坑（2026-11-15）：`msedge --app=URL --user-data-dir=<新目录>` 在这台机器上
       会开出空白窗口（标题 `127.0.0.1_/`），所以要先等窗口标题确认页面真的加载了，
       没加载就收掉进程退回 `--new-window`。

窗口关掉之后，本地服务（以及 dev 起的 Redis）一起收掉。
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
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

# 页面标题里应该出现的东西（index.html 的 <title>）
EXPECTED_TITLE = "minicode"

# 浏览器"没加载成功"时出现的标题特征
_BAD_TITLE_MARKERS = ("新建标签页", "New tab", "无标题", "Untitled")

# 桌面窗口标题（和 index.html 的 <title> 一致）
QT_WINDOW_TITLE = "minicode"


def qt_window_available() -> bool:
    """装了 PySide6 才开桌面窗口；MINICODE_GUI=0 可以强制关掉。"""
    if os.environ.get("MINICODE_GUI", "").strip().lower() in ("0", "off", "false"):
        return False
    return importlib.util.find_spec("PySide6") is not None


def _ensure_qt_flags() -> None:
    """给 QtWebEngine 补 Chromium 兼容参数。

    Chromium 的 GPU/沙箱进程在部分环境（远程会话、受限 Job）里起不来，窗口会白屏。
    窗口只加载 127.0.0.1 上的自家页面，所以默认补 --no-sandbox；
    用户/系统已经设置过 flags 时不覆盖。
    """
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--no-sandbox" not in flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{flags} --no-sandbox".strip()


def run_qt_window(
    url: str,
    *,
    workspace: Path | None = None,
    width: int = 1440,
    height: int = 900,
) -> int:
    """用 PySide6 的 QWebEngineView 开原生窗口，阻塞到窗口关闭。

    页面 10 秒内没加载出来就把窗口关掉并抛异常，由调用方降级到浏览器窗口。
    """
    _ensure_qt_flags()

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QMainWindow

    profile_dir = (Path(workspace) if workspace else Path.cwd()) / ".minicode" / "qt-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([sys.argv[0]])
    window = QMainWindow()
    window.setWindowTitle(QT_WINDOW_TITLE)
    window.resize(width, height)
    window.setMinimumSize(980, 620)

    view = QWebEngineView()
    profile = QWebEngineProfile("minicode", app)
    profile.setPersistentStoragePath(str(profile_dir / "storage"))
    profile.setCachePath(str(profile_dir / "cache"))
    page = QWebEnginePage(profile, view)
    view.setPage(page)
    window.setCentralWidget(view)

    state = {"loaded": False, "failed": False}

    def _on_loaded(ok: bool) -> None:
        state["loaded"] = bool(ok)

    def _check_loaded() -> None:
        if not state["loaded"]:
            state["failed"] = True
            window.close()

    view.loadFinished.connect(_on_loaded)
    view.load(QUrl(url))
    window.show()

    # 页面 10 秒还没加载出来就当失败：关窗让 exec() 返回，调用方降级
    QTimer.singleShot(10_000, _check_loaded)
    app.exec()

    if state["failed"]:
        raise RuntimeError("页面 10 秒内没有加载完成")
    return 0


def _port_in_use(host: str, port: int, timeout: float = 0.3) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def find_browser(explicit: str | None = None) -> str | None:
    """找一个能用的浏览器。找不到返回 None（调用方退化成"只打印地址"）。"""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def browser_args(browser: str, url: str, profile_dir: Path,
                 mode: str = "app", size: str = "1100,820") -> list[str]:
    """拼启动参数。

    mode="app"    独立窗口（没有地址栏），但不是所有环境都能正常渲染
    mode="window" 普通窗口 + 一个标签页，稳定（作为降级方案）

    两种都要独立 profile：不给的话 Edge 会把窗口交给已有实例、自己立刻退出，
    我们就分不清"窗口关了"和"根本没开"。
    """
    common = [
        f"--user-data-dir={profile_dir}",
        f"--window-size={size}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if mode == "window":
        return [browser, "--new-window", url, *common]
    return [browser, f"--app={url}", *common]


def looks_loaded(title: str, url: str = "") -> bool:
    """窗口标题看起来像"页面真的加载了"吗。

    这个判断必须保守：只认真正的页面标题。空白窗口的典型标题是
    `127.0.0.1_/`（主机名 + 什么都没加载），或者浏览器的"新建标签页"。
    """
    title = (title or "").strip()
    if not title:
        return False
    if EXPECTED_TITLE in title.lower():
        return True
    if any(marker in title for marker in _BAD_TITLE_MARKERS):
        return False
    # 形如 "127.0.0.1_/" / "localhost/"：主机名加个斜杠，说明导航没成功
    stripped = title.strip("_/ ")
    if stripped and stripped.replace(".", "").replace(":", "").isdigit():
        return False
    if url:
        host = url.split("://")[-1].split("/")[0].split(":")[0]
        if title.strip("_/ ") == host:
            return False
    return False


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def window_titles(pids: set[int]) -> list[str]:
    """枚举这些进程的可见窗口标题（Windows）。其它平台返回空列表。"""
    if sys.platform != "win32" or not pids:
        return []
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        return []

    cb_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    titles: list[str] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if buf.value.strip():
            titles.append(buf.value)
        return True

    user32.EnumWindows(cb_type(callback), 0)
    return titles


def _pids_of_child(proc: subprocess.Popen) -> set[int]:
    """拿子进程树里所有 msedge 的 PID。

    直接问 WMI 太慢，这里用任务的 PID + 它派生出来的进程。
    简单起见：先只查 WMI 一次（可缓存），失败就退化成"只看主进程"。
    """
    pids = {proc.pid}
    name = Path(proc.args[0] if isinstance(proc.args, list) else proc.args).name
    try:
        out = subprocess.run(
            ["pwsh", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"Name='{name}'\" | "
             f"Where-Object {{ $_.ParentProcessId -eq {proc.pid} -or $_.ProcessId -eq {proc.pid} }} | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, encoding="utf-8", timeout=8,
        )
        for token in out.stdout.split():
            if token.isdigit():
                pids.add(int(token))
    except (OSError, subprocess.SubprocessError):
        pass
    return pids


def wait_for_health(base_url: str, timeout: float = 15.0) -> bool:
    """等后端起来。"""
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


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.terminate()


def _wait_loaded(proc: subprocess.Popen, url: str, seconds: float) -> bool:
    """等页面渲染出来。返回是否成功。

    为什么要验证：有些环境下窗口开得出来但**渲染不了**（标题停在"无标题"或
    `127.0.0.1_/`），这种"看起来打开了"的假象比直接报错更难查。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if any(looks_loaded(t, url) for t in window_titles(_pids_of_child(proc))):
            return True
        time.sleep(0.4)
    return False


def open_via_shell(url: str) -> bool:
    """交给系统默认浏览器打开（explorer 是独立进程，不继承本进程的限制）。

    Windows 上 `explorer.exe <url>` 会用默认浏览器打开，这是实测唯一
    在受限环境里也能正常渲染的办法。
    """
    if sys.platform == "win32":
        try:
            subprocess.Popen(["explorer.exe", url])
            return True
        except OSError:
            return False
    try:
        return subprocess.Popen(["xdg-open", url]).pid > 0
    except OSError:
        return False


def launch_browser(browser: str, url: str, profile_dir: Path,
                   mode: str = "auto", verify_seconds: float = 6.0
                   ) -> tuple[subprocess.Popen | None, str]:
    """开窗口，返回 (进程, 实际模式)；进程为 None 表示交给系统浏览器了。

    顺序：
        1. mode=app   —— 独立窗口，最接近"桌面应用"
        2. mode=window —— 普通窗口
        3. 系统默认浏览器（explorer）—— 兜底，一定能渲染，但和本进程解绑
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    order = ["app", "window"] if mode == "auto" else [mode]

    for attempt in order:
        proc = subprocess.Popen(browser_args(browser, url, profile_dir, attempt))
        if _wait_loaded(proc, url, verify_seconds):
            return proc, attempt
        _kill_tree(proc)
        time.sleep(0.5)

    return None, "shell"


def run_app(
    app,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    workspace: Path | None = None,
    browser: str | None = None,
    mode: str = "auto",
    dry_run: bool = False,
    wait_window: bool = True,
) -> int:
    """起服务 + 开窗口。窗口关掉之后把服务也收掉。"""
    url = f"http://{host}:{port}"
    profile = (Path(workspace) if workspace else Path.cwd()) / ".minicode" / "edge-profile"
    found = find_browser(browser)
    has_gui = qt_window_available()

    if dry_run:
        print(f"  服务地址: {url}")
        print(f"  桌面窗口: {'PySide6（可用）' if has_gui else '(没装 PySide6，用浏览器窗口)'}")
        print(f"  浏览器  : {found or '(没找到，会退化成只打印地址)'}")
        print(f"  窗口模式: {mode}")
        if found:
            print(f"  app 参数   : {' '.join(browser_args(found, url, profile, 'app'))}")
            print(f"  window 参数: {' '.join(browser_args(found, url, profile, 'window'))}")
        return 0

    # 端口被旧实例占着时，我们起的 uvicorn 会绑定失败，但 wait_for_health
    # 会从旧实例拿到 200 —— 结果窗口指向旧服务，用户以为在用新的。
    if _port_in_use(host, port):
        print(f"端口 {port} 已经被占用（可能之前的服务还在跑）。", file=sys.stderr)
        print(f"  换个端口：minicode app --port {port + 1}", file=sys.stderr)
        return 1

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

    use_gui = mode in ("gui", "auto") and has_gui
    if mode == "gui" and not has_gui:
        print('没装 PySide6，装法: pip install -e ".[desktop]"', file=sys.stderr)
    if use_gui:
        print("正在打开桌面窗口（PySide6 / QWebEngineView）…")
        try:
            run_qt_window(url, workspace=workspace)
        except Exception as exc:  # noqa: BLE001 - 窗口失败要降级，不能让整个命令挂掉
            print(f"桌面窗口没起来（{type(exc).__name__}: {exc}），退回浏览器窗口", file=sys.stderr)
        else:
            print("窗口已关闭")
            server.should_exit = True
            thread.join(timeout=5)
            return 0

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

    print("正在打开窗口…")
    proc, used = launch_browser(found, url, profile, mode)

    if proc is None:
        if not open_via_shell(url):
            print(f"打不开浏览器，请手动访问 {url}", file=sys.stderr)
        else:
            print(f"已交给系统默认浏览器打开（页面没能在独立窗口里渲染）。")
            print("这种方式下窗口和本程序解绑：关闭窗口不会停服务，按 Ctrl+C 结束。")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        thread.join(timeout=5)
        print("已退出")
        return 0

    print(f"窗口已打开（{used} 模式）。关闭窗口即退出。")

    try:
        if wait_window:
            while proc.poll() is None:
                time.sleep(0.5)
            print("窗口已关闭")
        else:
            print("（--no-wait：窗口在后台，服务继续跑，按 Ctrl+C 结束）")
            while thread.is_alive():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _kill_tree(proc)
        server.should_exit = True
        thread.join(timeout=5)
    print("已退出")
    return 0
