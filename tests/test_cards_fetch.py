import io
import os

import pytest

from lscs.cards.fetch import CACHE_NAME, USER_AGENT, ensure_cards_json


class FakeOpener:
    def __init__(self, data=b"[]", error=None):
        self.data, self.error, self.requests = data, error, []

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(self.data)


def test_downloads_when_missing_with_user_agent(tmp_path):
    opener = FakeOpener(b'[{"id": "X"}]')
    path = ensure_cards_json(tmp_path / "cache", opener=opener)
    assert path.read_bytes() == b'[{"id": "X"}]'
    assert opener.requests[0].get_header("User-agent") == USER_AGENT


def test_uses_fresh_cache_without_downloading(tmp_path):
    (tmp_path / CACHE_NAME).write_bytes(b"cached")
    opener = FakeOpener()
    assert ensure_cards_json(tmp_path, opener=opener).read_bytes() == b"cached"
    assert opener.requests == []


def test_refreshes_stale_cache(tmp_path):
    path = tmp_path / CACHE_NAME
    path.write_bytes(b"old")
    os.utime(path, (0, 0))
    ensure_cards_json(tmp_path, opener=FakeOpener(b"[2]"))
    assert path.read_bytes() == b"[2]"


def test_download_failure_falls_back_to_stale_cache(tmp_path):
    path = tmp_path / CACHE_NAME
    path.write_bytes(b"old")
    os.utime(path, (0, 0))
    assert ensure_cards_json(tmp_path, opener=FakeOpener(error=OSError("offline"))) == path
    assert path.read_bytes() == b"old"


def test_download_failure_without_cache_raises(tmp_path):
    with pytest.raises(OSError):
        ensure_cards_json(tmp_path, opener=FakeOpener(error=OSError("offline")))


def test_incomplete_read_falls_back_to_stale_cache(tmp_path):
    from http.client import IncompleteRead

    path = tmp_path / CACHE_NAME
    path.write_bytes(b"old")
    os.utime(path, (0, 0))
    assert ensure_cards_json(tmp_path, opener=FakeOpener(error=IncompleteRead(b"x"))) == path
    assert path.read_bytes() == b"old"


def test_incomplete_read_without_cache_raises(tmp_path):
    from http.client import HTTPException, IncompleteRead

    with pytest.raises(HTTPException):
        ensure_cards_json(tmp_path, opener=FakeOpener(error=IncompleteRead(b"x")))


def test_non_json_body_is_not_cached(tmp_path):
    path = tmp_path / CACHE_NAME
    path.write_bytes(b"[1]")
    os.utime(path, (0, 0))
    assert ensure_cards_json(tmp_path, opener=FakeOpener(b"<html>portal</html>")) == path
    assert path.read_bytes() == b"[1]"


def test_non_json_body_without_cache_raises_and_writes_nothing(tmp_path):
    with pytest.raises(ValueError):
        ensure_cards_json(tmp_path, opener=FakeOpener(b"<html>portal</html>"))
    assert not (tmp_path / CACHE_NAME).exists()


def test_json_that_is_not_a_list_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ensure_cards_json(tmp_path, opener=FakeOpener(b'{"a": 1}'))


def test_default_opener_has_timeout(monkeypatch, tmp_path):
    import urllib.request

    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        return io.BytesIO(b"[]")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ensure_cards_json(tmp_path)
    assert seen["timeout"] == 30
