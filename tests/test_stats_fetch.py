"""下载并缓存 Firestone 的统计（本版本统计设计文档第 4 节）：全部用 FakeOpener，不碰网络。"""

import gzip
import io
import json
import os

import pytest

from lscs.cards.fetch import USER_AGENT
from lscs.stats.fetch import cache_name, ensure_stats_json, stats_url

HERO_BODY = json.dumps({"heroStats": [], "lastUpdateDate": "2026-10-14T03:10:27.372Z"}).encode()


class FakeResponse(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class FakeOpener:
    def __init__(self, data=HERO_BODY, error=None, headers=None):
        self.data, self.error, self.headers, self.requests = data, error, headers, []

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(self.data, self.headers)


def test_urls_and_cache_names_for_each_kind():
    base = "https://static.zerotoheroes.com/api/bgs/"
    assert stats_url("hero", 100, "last-patch") == (
        base + "hero-stats/mmr-100/last-patch/overview-from-hourly.gz.json"
    )
    assert stats_url("card", 50, "last-patch") == (
        base + "card-stats/mmr-50/last-patch/overview-from-hourly.gz.json"
    )
    # 饰品的路径没有分段（加 mmr-100/ 会 403）：分段在每个条目里
    assert stats_url("trinket", 100, "past-seven") == (
        base + "trinket-stats/past-seven/overview-from-hourly.gz.json"
    )
    assert cache_name("hero", 100, "last-patch") == "firestone-hero-mmr100-last-patch.json"
    assert cache_name("card", 50, "last-patch") == "firestone-card-mmr50-last-patch.json"
    assert cache_name("trinket", 100, "past-seven") == "firestone-trinket-past-seven.json"


def test_downloads_when_missing_with_headers(tmp_path):
    opener = FakeOpener()
    path = ensure_stats_json(tmp_path / "cache", "hero", 100, "last-patch", opener=opener)
    assert path == tmp_path / "cache" / "firestone-hero-mmr100-last-patch.json"
    assert path.read_bytes() == HERO_BODY
    request = opener.requests[0]
    assert request.full_url == stats_url("hero", 100, "last-patch")
    assert request.get_header("User-agent") == USER_AGENT
    assert request.get_header("Accept-encoding") == "gzip"


def test_gzip_body_is_unpacked_before_caching(tmp_path):
    # 正文以 1f 8b 开头（没有 Content-Encoding 头也认）
    path = ensure_stats_json(
        tmp_path, "hero", 100, "last-patch", opener=FakeOpener(gzip.compress(HERO_BODY))
    )
    assert path.read_bytes() == HERO_BODY


def test_only_the_gzip_magic_triggers_unpacking(tmp_path):
    # 响应头说是 gzip、正文却已经是明文（中间有代理解压过）：看正文开头，不看响应头
    opener = FakeOpener(HERO_BODY, headers={"Content-Encoding": "gzip"})
    assert (
        ensure_stats_json(tmp_path, "hero", 100, "last-patch", opener=opener).read_bytes()
        == HERO_BODY
    )


def test_plain_body_is_kept_as_is(tmp_path):
    body = b'{"trinketStats": [{"trinketCardId": "X"}]}'
    path = ensure_stats_json(tmp_path, "trinket", 100, "last-patch", opener=FakeOpener(body))
    assert path.read_bytes() == body


def test_uses_fresh_cache_without_downloading(tmp_path):
    path = tmp_path / cache_name("hero", 100, "last-patch")
    path.write_bytes(b"cached")
    opener = FakeOpener()
    assert ensure_stats_json(tmp_path, "hero", 100, "last-patch", opener=opener) == path
    assert opener.requests == [] and path.read_bytes() == b"cached"


def test_refreshes_stale_cache(tmp_path):
    path = tmp_path / cache_name("hero", 100, "last-patch")
    path.write_bytes(b"old")
    os.utime(path, (0, 0))
    ensure_stats_json(tmp_path, "hero", 100, "last-patch", opener=FakeOpener())
    assert path.read_bytes() == HERO_BODY


def test_max_age_is_a_day(tmp_path):
    path = tmp_path / cache_name("hero", 100, "last-patch")
    path.write_bytes(b"cached")
    os.utime(path, (1000, 1000))
    opener = FakeOpener()
    ensure_stats_json(
        tmp_path, "hero", 100, "last-patch", opener=opener, now=lambda: 1000 + 23 * 3600
    )
    assert opener.requests == []
    ensure_stats_json(
        tmp_path, "hero", 100, "last-patch", opener=opener, now=lambda: 1000 + 24 * 3600
    )
    assert len(opener.requests) == 1


def test_download_failure_falls_back_to_stale_cache(tmp_path):
    path = tmp_path / cache_name("hero", 100, "last-patch")
    path.write_bytes(b"old")
    os.utime(path, (0, 0))
    opener = FakeOpener(error=OSError("offline"))
    assert ensure_stats_json(tmp_path, "hero", 100, "last-patch", opener=opener) == path
    assert path.read_bytes() == b"old"


def test_download_failure_without_cache_raises(tmp_path):
    with pytest.raises(OSError):
        ensure_stats_json(
            tmp_path, "hero", 100, "last-patch", opener=FakeOpener(error=OSError("x"))
        )


def test_incomplete_read_falls_back_or_raises(tmp_path):
    from http.client import HTTPException, IncompleteRead

    with pytest.raises(HTTPException):
        ensure_stats_json(
            tmp_path, "card", 100, "last-patch", opener=FakeOpener(error=IncompleteRead(b"x"))
        )
    path = tmp_path / cache_name("card", 100, "last-patch")
    path.write_bytes(b"old")
    os.utime(path, (0, 0))
    opener = FakeOpener(error=IncompleteRead(b"x"))
    assert ensure_stats_json(tmp_path, "card", 100, "last-patch", opener=opener) == path


@pytest.mark.parametrize(
    "body",
    [
        b"<html>portal</html>",  # 登录认证页之类的网页
        b"[1, 2]",  # 不是对象
        b'{"cardStats": []}',  # 对象，但没有英雄文件的顶层键
        b'{"heroStats": {}}',  # 顶层键的值不是列表
        b"\x1f\x8b broken gzip",
    ],
)
def test_bad_bodies_are_not_cached(tmp_path, body):
    with pytest.raises(ValueError):
        ensure_stats_json(tmp_path, "hero", 100, "last-patch", opener=FakeOpener(body))
    assert not (tmp_path / cache_name("hero", 100, "last-patch")).exists()
    assert list(tmp_path.iterdir()) == []  # 也没有留下 .tmp


def test_bad_body_keeps_the_old_cache(tmp_path):
    path = tmp_path / cache_name("hero", 100, "last-patch")
    path.write_bytes(b"old")
    os.utime(path, (0, 0))
    opener = FakeOpener(b'{"error": "nope"}')
    assert ensure_stats_json(tmp_path, "hero", 100, "last-patch", opener=opener) == path
    assert path.read_bytes() == b"old"


def test_default_opener_has_timeout(monkeypatch, tmp_path):
    import urllib.request

    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        return FakeResponse(HERO_BODY)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ensure_stats_json(tmp_path, "hero", 100, "last-patch")
    assert seen["timeout"] == 30
