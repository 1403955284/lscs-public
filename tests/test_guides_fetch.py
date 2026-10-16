"""下载并缓存 HSReplay 的流派指南（流派指南自动更新设计文档第 3.1 节）：全部用 FakeOpener，
不碰网络。"""

import gzip
import io
import json
import os

import pytest

from lscs.cards.fetch import USER_AGENT
from lscs.guides.fetch import CACHE_NAME, GUIDES_URL, ensure_guides_json

BODY = json.dumps([{"id": 1, "name": "Menagerie", "core_cards": [1, 2]}]).encode()


class FakeResponse(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class FakeOpener:
    def __init__(self, data=BODY, error=None, headers=None):
        self.data, self.error, self.headers, self.requests = data, error, headers, []

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(self.data, self.headers)


def test_url_and_cache_name():
    assert GUIDES_URL == "https://hsreplay.net/api/v1/battlegrounds/comp_guides/"
    assert CACHE_NAME == "hsreplay-comp-guides.json"


def test_downloads_when_missing_with_headers(tmp_path):
    opener = FakeOpener()
    path = ensure_guides_json(tmp_path / "cache", opener=opener)
    assert path == tmp_path / "cache" / CACHE_NAME
    assert path.read_bytes() == BODY
    request = opener.requests[0]
    assert request.full_url == GUIDES_URL
    assert request.get_header("User-agent") == USER_AGENT
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("Accept-encoding") == "gzip"


def test_gzip_body_is_unpacked_and_a_plain_body_kept(tmp_path):
    path = ensure_guides_json(tmp_path / "a", opener=FakeOpener(gzip.compress(BODY)))
    assert path.read_bytes() == BODY
    opener = FakeOpener(BODY, headers={"Content-Encoding": "gzip"})  # 代理已经解压过
    assert ensure_guides_json(tmp_path / "b", opener=opener).read_bytes() == BODY


def test_uses_fresh_cache_and_refreshes_after_a_day(tmp_path):
    path = tmp_path / CACHE_NAME
    path.write_bytes(b"cached")
    os.utime(path, (1000, 1000))
    opener = FakeOpener()
    assert ensure_guides_json(tmp_path, opener=opener, now=lambda: 1000 + 23 * 3600) == path
    assert opener.requests == [] and path.read_bytes() == b"cached"
    ensure_guides_json(tmp_path, opener=opener, now=lambda: 1000 + 24 * 3600)
    assert len(opener.requests) == 1 and path.read_bytes() == BODY


def test_download_failure_falls_back_to_stale_cache_or_raises(tmp_path):
    from http.client import IncompleteRead

    with pytest.raises(OSError):
        ensure_guides_json(tmp_path, opener=FakeOpener(error=OSError("offline")))
    path = tmp_path / CACHE_NAME
    path.write_bytes(b"old")
    os.utime(path, (0, 0))
    for error in (OSError("offline"), IncompleteRead(b"x")):
        assert ensure_guides_json(tmp_path, opener=FakeOpener(error=error)) == path
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize(
    "body",
    [
        b"<html>login</html>",  # 登录页之类的网页
        b'{"results": []}',  # 不是列表
        b'[{"name": "A", "core_cards": []}, 3]',  # 元素不是对象
        b'[{"core_cards": [1]}]',  # 没有 name
        b'[{"name": 5, "core_cards": [1]}]',  # name 不是字符串
        b'[{"name": "A"}]',  # 没有 core_cards
        b'[{"name": "A", "core_cards": "1,2"}]',  # core_cards 不是列表
        b"\x1f\x8b broken gzip",
    ],
)
def test_bad_bodies_are_not_cached(tmp_path, body):
    with pytest.raises(ValueError, match="流派指南格式不对"):
        ensure_guides_json(tmp_path, opener=FakeOpener(body))
    assert list(tmp_path.iterdir()) == []  # 也没有留下 .tmp


def test_bad_body_keeps_the_old_cache(tmp_path):
    path = tmp_path / CACHE_NAME
    path.write_bytes(b"old")
    os.utime(path, (0, 0))
    assert ensure_guides_json(tmp_path, opener=FakeOpener(b'{"detail": "nope"}')) == path
    assert path.read_bytes() == b"old"


def test_default_opener_has_timeout(monkeypatch, tmp_path):
    import urllib.request

    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        return FakeResponse(BODY)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ensure_guides_json(tmp_path)
    assert seen["timeout"] == 30
