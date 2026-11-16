"""HTTP 本地 token 鉴权。

设计取舍：
    * 默认**不启用**（不设 MINICODE_TOKEN 就是原来的行为）—— 单机自用没必要每次都填
    * 启用后只放行页面骨架 / 静态资源 / 健康检查，否则浏览器连页面都打不开
    * 浏览器 EventSource 不能自定义请求头，所以额外接受 `?token=`
    * 比对用 hmac.compare_digest，避免逐字符比较的时序差异
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply
from minicode.service import create_app
from minicode.service.store import InMemoryStore, RunState

TOKEN = "s3cret-token"


class Echo:
    def complete(self, system, messages, tools):
        return ModelReply(text="ok", stop_reason="end_turn")


def _client(tmp_path, token=None):
    return TestClient(create_app(
        provider_factory=lambda s: Echo(),
        settings=Settings(api_key="fake", model="m", max_steps=2),
        store=InMemoryStore(),
        default_workspace=tmp_path,
        token=token,
    ))


def _authed(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ 不启用时


def test_no_token_means_open(tmp_path):
    client = _client(tmp_path)
    assert client.get("/conversations").status_code == 200
    assert client.get("/runs").status_code == 200


# ------------------------------------------------------------------ 启用后


def test_healthz_is_public(tmp_path):
    client = _client(tmp_path, TOKEN)
    assert client.get("/healthz").status_code == 200


def test_page_and_assets_are_public(tmp_path):
    client = _client(tmp_path, TOKEN)
    assert client.get("/").status_code == 200
    # 静态资源也不能挡，否则页面加载不出来
    from minicode.service.app import WEB_DIR
    assets = list((WEB_DIR / "assets").glob("*.js"))
    if assets:
        assert client.get(f"/assets/{assets[0].name}").status_code == 200


def test_api_requires_token(tmp_path):
    client = _client(tmp_path, TOKEN)
    resp = client.get("/conversations")
    assert resp.status_code == 401
    assert "令牌" in resp.json()["detail"]


def test_header_token_works(tmp_path):
    client = _client(tmp_path, TOKEN)
    assert client.get("/conversations", headers=_authed()).status_code == 200
    assert client.get("/runs", headers=_authed()).status_code == 200


def test_query_token_works_for_sse(tmp_path):
    """EventSource 加不了请求头，SSE 必须支持 ?token=。"""
    client = _client(tmp_path, TOKEN)
    assert client.get(f"/conversations?token={TOKEN}").status_code == 200


def test_wrong_token_rejected(tmp_path):
    client = _client(tmp_path, TOKEN)
    assert client.get("/conversations", headers=_authed("nope")).status_code == 401


def test_protected_write_endpoints(tmp_path):
    client = _client(tmp_path, TOKEN)
    assert client.post("/runs", json={"task": "x"}).status_code == 401
    assert client.post("/runs", json={"task": "x"}, headers=_authed()).status_code == 202
    assert client.delete("/runs/nope").status_code == 401


def test_token_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_TOKEN", TOKEN)
    client = TestClient(create_app(
        provider_factory=lambda s: Echo(),
        settings=Settings(api_key="fake", model="m", max_steps=2),
        store=InMemoryStore(),
        default_workspace=tmp_path,
    ))
    assert client.get("/conversations").status_code == 401
    assert client.get("/conversations", headers=_authed()).status_code == 200
