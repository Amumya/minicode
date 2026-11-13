from config import DEFAULT, DESCRIPTION


def test_timeout_changed():
    assert DEFAULT["timeout"] == 60


def test_retries_changed():
    assert DEFAULT["retries"] == 3


def test_headers_intact():
    """中文和引号那块不能改坏。"""
    assert DEFAULT["headers"]["X-Note"] == "如果超时就把 timeout 调大"
    assert DEFAULT["headers"]["X-Trace"] == "on"


def test_endpoint_intact():
    assert DEFAULT["endpoint"] == "https://example.com/api"


def test_description_intact():
    assert DESCRIPTION.startswith("默认配置")
