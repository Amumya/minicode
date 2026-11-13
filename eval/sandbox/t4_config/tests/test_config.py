from config import DEFAULT


def test_endpoint_is_https():
    assert DEFAULT["endpoint"].startswith("https")


def test_headers_present():
    assert DEFAULT["headers"]["X-Trace"] == "on"
