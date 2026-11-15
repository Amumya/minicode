"""开发时用的"一键起服务"。

为什么需要：每次手动要起 Redis、设环境变量、起 uvicorn、再打开浏览器 ——
四步、三个终端窗口，还容易忘掉设 `MINICODE_REDIS_URL` 导致"为什么历史没了"。

`minicode dev` 把这些合成一条命令，并且：
    * Redis 没起来就自己起（用仓库里带的便携版）
    * Ctrl+C 结束的时候把起过的进程一起收掉，不留孤儿
    * `--open` 顺便开窗口（复用 app_shell 的三级降级）
    * `--reload` 改完 Python 代码自动重载（前端还得 `--build` 或手动 build）

刻意没做的：自动安装依赖、自动 git 操作、后台常驻（要常驻不如直接开着窗口）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_REDIS_PORT = 6380


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """端口上有没有人在听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def find_bundled_redis(root: Path) -> Path | None:
    """找仓库里带的便携版 Redis。找不到返回 None。"""
    candidates = (
        root / ".tools" / "redis" / "redis-server.exe",
        root / ".tools" / "redis" / "redis-server",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def find_bundled_redis_conf(root: Path) -> Path | None:
    for name in (".tools/redis/redis.conf", ".tools/redis.conf"):
        path = root / name
        if path.is_file():
            return path
    return None


def redis_url(port: int = DEFAULT_REDIS_PORT) -> str:
    return f"redis://127.0.0.1:{port}/0"


def ensure_redis(root: Path, *, port: int = DEFAULT_REDIS_PORT,
                 enabled: bool = True) -> tuple[subprocess.Popen | None, str | None]:
    """确保 Redis 可用。返回 (我们自己起的进程, 应该用的 redis url)。

    * 端口已经在听 → 直接用，不启动新进程（返回 (None, url)）
    * 没在听但仓库里有便携版 → 启动它（返回 (proc, url)）
    * 没在听也没有便携版 → 返回 (None, None)，让调用方退化成内存存储
    """
    if not enabled:
        return None, None

    url = redis_url(port)
    if port_open("127.0.0.1", port):
        return None, url

    server = find_bundled_redis(root)
    if server is None:
        return None, None

    conf = find_bundled_redis_conf(root)
    args = [str(server)] + ([str(conf)] if conf else ["--port", str(port)])
    log = subprocess.DEVNULL
    proc = subprocess.Popen(args, cwd=str(root), stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL)

    deadline = time.time() + 10
    while time.time() < deadline:
        if port_open("127.0.0.1", port):
            return proc, url
        if proc.poll() is not None:
            return None, None
        time.sleep(0.15)
    return proc, url


def stop(proc: subprocess.Popen | None) -> None:
    """收掉我们起的进程（只收自己起的，不动别人的）。"""
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.terminate()


def build_frontend(root: Path) -> bool:
    """重新构建前端（产物是提交进仓库的，改了前端就必须重建）。"""
    web = root / "web"
    if not (web / "package.json").is_file():
        print("  没有 web/ 目录，跳过前端构建")
        return False
    print("  构建前端…")
    proc = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(web), shell=(sys.platform == "win32"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print("  前端构建失败：")
        print("  " + (proc.stderr or proc.stdout)[-400:].replace("\n", "\n  "))
        return False
    print("  前端已构建")
    return True


def apply_env(url: str | None) -> None:
    """把 redis url 写进环境变量，服务端启动时会读。"""
    if url:
        os.environ["MINICODE_REDIS_URL"] = url
