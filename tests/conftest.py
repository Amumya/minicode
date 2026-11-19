"""测试的全局隔离。

**这个文件是补上一个真实事故的补丁**：

    有些测试调 `create_app()` 时没传 `store=`，而 `create_app` 会回落到
    `default_store()`；`default_store()` 又读 `MINICODE_REDIS_URL`。
    结果：只要跑测试的那个终端里设了这个环境变量，**测试的假 run 就写进了真实 Redis**，
    界面历史里出现一堆 model=fake-model、workspace 指向 pytest 临时目录的记录。

修复手段：
    1. 每个测试前把 `MINICODE_REDIS_URL` / `MINICODE_WORKSPACE` 删掉
    2. 再把 `default_store` 换成"永远返回内存 store"——双保险，
       以后就算有人忘了传 store、环境里又有变量，也不会碰真实数据
"""

from __future__ import annotations

import pytest

from minicode.service.store import InMemoryStore


@pytest.fixture(autouse=True)
def _never_touch_real_redis(monkeypatch):
    monkeypatch.delenv("MINICODE_REDIS_URL", raising=False)
    monkeypatch.delenv("MINICODE_WORKSPACE", raising=False)
    from minicode.service import app as app_module

    # 每个测试使用独立的内存 store，避免状态复用。
    monkeypatch.setattr(app_module, "default_store", lambda: InMemoryStore())
    yield
