from unittest.mock import Mock

import pytest

from arbitrage_telebot import HttpError, http_get_json, http_post_json


@pytest.mark.parametrize(
    "content_type,body",
    [
        ("text/html", "<html><body>upstream down</body></html>"),
        ("text/plain", "service unavailable"),
    ],
)
def test_http_get_json_parse_failure_includes_context(monkeypatch, content_type, body):
    response = Mock()
    response.status_code = 200
    response.content = body.encode("utf-8")
    response.text = body
    response.headers = {"Content-Type": content_type}
    response.json.side_effect = ValueError("invalid json")

    monkeypatch.setattr("arbitrage_telebot.requests.get", lambda *args, **kwargs: response)

    url = "https://api.example.com/ticker"
    with pytest.raises(HttpError) as exc_info:
        http_get_json(url, retries=1)

    message = str(exc_info.value)
    assert url in message
    assert "status=200" in message
    assert content_type in message
    assert body[:200] in message


def test_http_post_json_parse_failure_includes_context(monkeypatch):
    body = "<html>rate limited gateway</html>"
    response = Mock()
    response.status_code = 200
    response.content = body.encode("utf-8")
    response.text = body
    response.headers = {"Content-Type": "text/html"}
    response.json.side_effect = ValueError("invalid json")

    monkeypatch.setattr("arbitrage_telebot.requests.post", lambda *args, **kwargs: response)

    url = "https://api.example.com/order"
    with pytest.raises(HttpError) as exc_info:
        http_post_json(url, payload={"side": "buy"}, retries=1)

    message = str(exc_info.value)
    assert url in message
    assert "status=200" in message
    assert "text/html" in message
    assert body[:200] in message
