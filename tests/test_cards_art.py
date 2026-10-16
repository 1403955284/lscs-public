"""卡图下载与缓存（对手阵容卡图窗口设计文档第 6.2 节）：注入假的 opener 和时钟，不碰网络；
卡图是 Pillow 现生成的 2×2 JPEG。"""

import io
import logging
import sys
import threading
import time
import urllib.error

from lscs.cards import art
from lscs.cards.art import ART_URL, ArtCache
from lscs.cards.fetch import USER_AGENT


def jpeg(color=(200, 30, 30), size=(2, 2)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


class Opener:
    """按卡牌 ID 返回字节或抛出异常；记下每次请求。"""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        card_id = request.full_url.rsplit("/", 1)[1].removesuffix(".jpg")
        result = self.responses[card_id]
        if isinstance(result, BaseException):
            raise result
        return io.BytesIO(result)

    @property
    def calls(self):
        return len(self.requests)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def cache(tmp_path, responses, clock=None):
    opener = Opener(responses)
    store = ArtCache(tmp_path, opener=opener, start=False, clock=clock or Clock())
    return store, opener


def not_found(card_id):
    url = ART_URL.format(card_id=card_id)
    return urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def test_first_request_downloads_caches_and_decodes(tmp_path):
    store, opener = cache(tmp_path, {"BG_A": jpeg()})
    assert store.available and store.generation == 0
    assert store.image("BG_A") is None
    store.request(["BG_A"])
    assert store.work_once() is True
    assert opener.calls == 1
    request = opener.requests[0]
    assert request.full_url == "https://art.hearthstonejson.com/v1/256x/BG_A.jpg"
    assert request.get_header("User-agent") == USER_AGENT
    assert (tmp_path / "BG_A.jpg").read_bytes()[:2] == b"\xff\xd8"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["BG_A.jpg"]  # 没有 .tmp 残留
    image = store.image("BG_A")
    assert image is not None and image.mode == "RGB" and image.size == (2, 2)
    assert store.generation == 1
    store.request(["BG_A"])  # 已经有了：不再排队、不再下载
    assert store.work_once() is False
    assert opener.calls == 1 and store.generation == 1


def test_cached_file_is_read_without_downloading(tmp_path):
    (tmp_path / "BG_A.jpg").write_bytes(jpeg((10, 200, 10)))
    store, opener = cache(tmp_path, {"BG_A": OSError("不该下载")})
    store.request(["BG_A"])
    assert store.work_once() is True
    assert opener.calls == 0
    assert store.image("BG_A").getpixel((0, 0))[1] > 150


def test_cache_dir_is_created_on_first_download(tmp_path):
    store, _opener = cache(tmp_path / "cache" / "art", {"BG_A": jpeg()})
    store.request(["BG_A"])
    store.work_once()
    assert (tmp_path / "cache" / "art" / "BG_A.jpg").exists()


def test_corrupt_cached_file_is_downloaded_again(tmp_path):
    (tmp_path / "BG_A.jpg").write_bytes(b"\xff\xd8 broken")
    store, opener = cache(tmp_path, {"BG_A": jpeg()})
    store.request(["BG_A"])
    store.work_once()
    assert opener.calls == 1 and store.image("BG_A") is not None
    assert store.image("BG_A").size == (2, 2)


def test_error_page_is_not_cached_and_counts_as_a_failure(tmp_path):
    clock = Clock()
    store, opener = cache(tmp_path, {"BG_A": b"<html>blocked</html>"}, clock)
    store.request(["BG_A"])
    assert store.work_once() is True
    assert list(tmp_path.iterdir()) == [] and store.image("BG_A") is None
    assert store.generation == 0
    clock.now = art.RETRY_SECONDS
    opener.responses["BG_A"] = jpeg()
    assert store.work_once() is True  # 临时失败：30 秒后再试
    assert opener.calls == 2 and store.image("BG_A") is not None


def test_not_found_gives_up_for_this_run(tmp_path, caplog):
    clock = Clock()
    store, opener = cache(tmp_path, {"BG_GONE": not_found("BG_GONE")}, clock)
    store.request(["BG_GONE"])
    with caplog.at_level(logging.DEBUG, logger="lscs.cards.art"):
        assert store.work_once() is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    clock.now = 10_000
    store.request(["BG_GONE"])  # 放弃了：不再排队
    assert store.work_once() is False
    assert opener.calls == 1 and store.image("BG_GONE") is None


def test_timeouts_retry_after_a_while_and_give_up_with_one_warning(tmp_path, caplog):
    clock = Clock()
    responses = {"BG_A": TimeoutError("timed out"), "BG_B": TimeoutError("timed out")}
    store, opener = cache(tmp_path, responses, clock)
    store.request(["BG_A", "BG_B"])
    with caplog.at_level(logging.DEBUG, logger="lscs.cards.art"):
        assert store.work_once() and store.work_once()
        assert opener.calls == 2
        assert store.work_once() is False  # 还没到重试时间
        clock.now = art.RETRY_SECONDS - 0.1
        assert store.work_once() is False
        store.request(["BG_A"])  # 排着队（等重试）：不重复
        clock.now = art.RETRY_SECONDS
        assert store.work_once() and store.work_once()
        assert store.work_once() is False
        clock.now = 2 * art.RETRY_SECONDS
        assert store.work_once() and store.work_once()  # 第 3 次：放弃
        clock.now = 100 * art.RETRY_SECONDS
        store.request(["BG_A", "BG_B"])
        assert store.work_once() is False
    assert opener.calls == 2 * art.MAX_ATTEMPTS
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # 同一个原因只记一次
    assert "timed out" in warnings[0].getMessage()


def test_decoded_images_are_capped_least_recently_used_first(tmp_path, monkeypatch):
    monkeypatch.setattr(art, "MAX_DECODED", 2)
    store, _opener = cache(tmp_path, {c: jpeg() for c in ("BG_A", "BG_B", "BG_C")})
    store.request(["BG_A", "BG_B"])
    store.work_once()
    store.work_once()
    assert store.image("BG_A") is not None  # A 刚用过：先丢 B
    store.request(["BG_C"])
    store.work_once()
    assert store.image("BG_B") is None
    assert store.image("BG_A") is not None and store.image("BG_C") is not None
    store.request(["BG_B"])  # 丢掉的可以再要：这次从文件读
    store.work_once()
    assert store.image("BG_B") is not None


def test_requests_are_deduplicated_and_strange_ids_ignored(tmp_path):
    store, opener = cache(tmp_path, {"BG_A": jpeg(), "BG_B": jpeg()})
    store.request(["BG_A", "BG_A", "BG_B", "", "../evil", "a/b"])
    store.request(["BG_A"])
    assert store.work_once() and store.work_once()
    assert store.work_once() is False
    assert opener.calls == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["BG_A.jpg", "BG_B.jpg"]


def test_stopped_cache_does_nothing(tmp_path):
    store, opener = cache(tmp_path, {"BG_A": jpeg()})
    store.request(["BG_A"])
    store.stop()
    assert store.work_once() is False
    store.request(["BG_A"])
    assert store.work_once() is False
    assert opener.calls == 0


def test_without_pillow_nothing_is_downloaded(tmp_path, monkeypatch):
    data = jpeg()
    monkeypatch.setitem(sys.modules, "PIL", None)  # import PIL 失败
    store, opener = cache(tmp_path, {"BG_A": data})
    assert store.available is False
    store.request(["BG_A"])
    assert store.work_once() is False
    assert store.image("BG_A") is None and opener.calls == 0
    assert art.pillow_version() is None


def test_pillow_version_is_reported():
    import PIL

    assert art.pillow_version() == PIL.__version__


def test_background_thread_downloads_and_survives_errors(tmp_path):
    """真的开线程：请求后在后台下载、解码；出错（这里是 opener 抛出意外的异常）线程也不退出。"""
    responses = {"BG_BAD": RuntimeError("意外"), "BG_A": jpeg()}
    opener = Opener(responses)
    store = ArtCache(tmp_path, opener=opener)
    try:
        store.request(["BG_BAD"])
        store.request(["BG_A"])
        deadline = time.monotonic() + 5
        while store.generation == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert store.image("BG_A") is not None
        assert any(t.name == "lscs-art" for t in threading.enumerate())
    finally:
        store.stop()


def test_default_opener_respects_the_network_guard(tmp_path):
    """默认的 opener 走 urllib.request.urlopen：测试里被 conftest 换成了报错，所以只会记一次失败。"""
    store = ArtCache(tmp_path, start=False, clock=Clock())
    store.request(["BG_A"])
    assert store.work_once() is True
    assert store.image("BG_A") is None and list(tmp_path.iterdir()) == []


def test_real_sized_art_decodes(tmp_path):
    store, _opener = cache(tmp_path, {"BG_A": jpeg(size=(256, 256))})
    store.request(["BG_A"])
    store.work_once()
    assert store.image("BG_A").size == (256, 256)


def test_the_guard_keeps_the_art_cache_away_from_the_real_one(tmp_path_factory):
    import lscs.cli
    from lscs.cards.fetch import default_cache_dir

    assert lscs.cli.ART_CACHE_DIR is not None
    assert lscs.cli.ART_CACHE_DIR.name == "art"
    assert default_cache_dir() not in lscs.cli.ART_CACHE_DIR.parents
    assert tmp_path_factory.getbasetemp() in lscs.cli.ART_CACHE_DIR.parents


def test_golden_cards_use_the_normal_cards_art(tmp_path):
    store, opener = cache(tmp_path, {"BG_A": jpeg()})
    store.request(["BG_A_G", "BG_A"])
    assert store.work_once() is True and store.work_once() is False
    assert opener.calls == 1 and opener.requests[0].full_url.endswith("/BG_A.jpg")
    assert store.image("BG_A_G") is store.image("BG_A") is not None
    assert art.art_id("BG_A_G") == "BG_A" and art.art_id("TB_BaconUps_001") == "TB_BaconUps_001"


def test_unexpected_thread_error_counts_as_a_failure_and_backs_off(tmp_path):
    """下载、解码以外的意外错误（这里是处理一张卡图时整个出错）：这张按失败算（RETRY_SECONDS 后
    再试），线程等 RETRY_SECONDS 再继续，不会马上又拿同一张空转。stop() 能叫醒这次等待。"""
    clock = Clock()
    store, opener = cache(tmp_path, {"BG_A": jpeg()}, clock)
    real_process = store._process

    def broken(card_id):
        raise RuntimeError("意外")

    waits = []

    class Halt:
        def wait(self, seconds):
            waits.append(seconds)
            store._stopped = True  # 只跑一圈
            return True

        def set(self):
            waits.append("set")

    store._process = broken
    store._halt = Halt()
    store.request(["BG_A"])
    store._run()
    assert waits == [art.RETRY_SECONDS]
    store._stopped = False
    store._process = real_process
    assert store.work_once() is False  # 记成一次失败：还没到重试时间
    clock.now = art.RETRY_SECONDS
    assert store.work_once() is True and store.image("BG_A") is not None
    store.stop()
    assert waits[-1] == "set"


def test_stop_wakes_the_back_off(tmp_path):
    store = ArtCache(tmp_path, opener=Opener({}), start=False)
    store.stop()
    assert store._halt.is_set() and store._wake.is_set()
