"""界面的测试。

界面本身（DOM 渲染）没法在这里自动验，但"服务有没有把前端正确发出去"可以验：
根路径要返回 HTML、静态资源要能取到、接口不能被静态挂载吃掉。
"""

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply
from minicode.service import create_app
from minicode.service.app import WEB_DIR
from minicode.service.store import RedisStore


class NoopProvider:
    def complete(self, system, messages, tools):
        return ModelReply(text="ok", stop_reason="end_turn")


@pytest.fixture()
def client(tmp_path):
    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    app = create_app(
        provider_factory=lambda s: NoopProvider(),
        settings=Settings(api_key="fake", model="m", max_steps=2),
        store=store,
        default_workspace=tmp_path,
    )
    return TestClient(app)


@pytest.mark.skipif(not WEB_DIR.is_dir(), reason="前端还没构建")
def test_root_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "minicode" in resp.text


@pytest.mark.skipif(not WEB_DIR.is_dir(), reason="前端还没构建")
def test_static_assets_are_served(client):
    index = client.get("/").text
    # 产物文件名带哈希，所以从 HTML 里把路径抠出来再请求，别写死
    assert "assets/" in index
    start = index.find("assets/")
    end = index.find('"', start)
    asset = index[start:end]
    resp = client.get("/" + asset)
    assert resp.status_code == 200
    assert len(resp.content) > 100


def test_api_endpoints_not_swallowed_by_mount(client):
    """静态挂载不能把接口吃掉 —— 这是挂载顺序搞错时最常见的症状。"""
    assert client.get("/api").json()["name"] == "minicode"
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/runs").status_code == 200


def test_unknown_path_is_404(client):
    assert client.get("/no/such/thing").status_code == 404
