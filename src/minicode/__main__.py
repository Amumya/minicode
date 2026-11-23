"""python -m minicode 入口（桌面端用来把服务拆成独立子进程）。"""

from minicode.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
