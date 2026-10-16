"""对手阵容卡图窗口用的卡图（对手阵容卡图窗口设计文档第 6.2 节）：按需从 HearthstoneJSON 的 art
接口下载 256×256 的原画裁切，按卡牌 ID 永久缓存在本机的缓存目录，在后台线程里解码成 Pillow 的图。

- 只下载要画的牌（对手最近一次交手时的随从），不整批预取；带 cards.fetch 的 User-Agent；
- 后台线程（lscs-art）只产出 Pillow 的图，**不碰 Tk**：界面线程定时看 generation 变了没有，
  变了就重画（照片由 overlay.portraits 在界面线程里做）；
- 没有 Pillow（用户没重新 uv sync）时 available 为 False：不开线程、不下载，界面只画名字；
- 404 这次运行里放弃；其他失败（超时、断网、5xx、不是 JPEG、解码失败）过 RETRY_SECONDS 再试，
  试满 MAX_ATTEMPTS 次放弃，同一个原因只记一次警告。线程里的任何异常都接住，线程不退出；意外的
  错误也按一次失败算，线程等 RETRY_SECONDS 再继续，不空转。
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from lscs.cards.fetch import USER_AGENT

if TYPE_CHECKING:
    from PIL.Image import Image

log = logging.getLogger(__name__)

ART_URL = "https://art.hearthstonejson.com/v1/256x/{card_id}.jpg"
ART_DIR = "art"  # default_cache_dir() / "art" / "{card_id}.jpg"，永不过期
TIMEOUT_SECONDS = 10
RETRY_SECONDS = 30  # 临时失败（超时、5xx、断网）多久后再试
MAX_ATTEMPTS = 3  # 同一张最多试几次，之后这次运行里不再试
MAX_DECODED = 160  # 内存里最多留几张解码后的原图（256×256 RGB 约 200 KB 一张）
MAX_BYTES = 2_000_000  # 一张图最多读这么多字节（正常约 13 KB）
JPEG_MAGIC = b"\xff\xd8"
_CARD_ID = re.compile(r"^[A-Za-z0-9_]+$")  # 卡牌 ID 只有字母、数字和下划线：不会拼出别的路径
GOLDEN_SUFFIX = "_G"  # 金色卡（BG21_015_G）用普通卡的原画：金色由卡图窗口的外圈表示

Opener = Callable[[urllib.request.Request], BinaryIO]


def pillow_version() -> str | None:
    """装了 Pillow 时返回版本号，否则 None（延迟导入：没有 Pillow 时 lscs 其余部分照常）。"""
    try:
        import PIL
        from PIL import Image  # noqa: F401  能真的解码才算
    except ImportError:
        return None
    return PIL.__version__


def _open(request: urllib.request.Request) -> BinaryIO:
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)


def art_id(card_id: str) -> str:
    """下载、缓存用的卡牌 ID：金色卡去掉 _G 后缀。"""
    return card_id.removesuffix(GOLDEN_SUFFIX) or card_id


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


class _GiveUp(Exception):
    """这张图这次运行里不再试（404）。"""


class ArtCache:
    def __init__(
        self,
        cache_dir: Path,
        *,
        opener: Opener = _open,
        start: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """start：开后台线程（测试传 False，直接调 work_once）。"""
        self._dir = cache_dir
        self._opener = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()  # request() 和 stop() 叫醒线程
        self._halt = threading.Event()  # 只有 stop() 叫醒：意外出错后的等待不被 request() 打断
        self._stopped = False
        self._pending: dict[str, float] = {}  # 排队中的卡牌 ID → 最早什么时候处理（clock）
        self._attempts: dict[str, int] = {}  # 失败过几次
        self._gave_up: set[str] = set()
        self._warned: set[str] = set()  # 记过警告的原因
        self._images: OrderedDict[str, Image] = OrderedDict()  # 最近用过的在后面
        self._generation = 0
        self.available = pillow_version() is not None
        self._thread: threading.Thread | None = None
        if start and self.available:
            self._thread = threading.Thread(target=self._run, name="lscs-art", daemon=True)
            self._thread.start()

    # ---- 界面线程 ----

    def request(self, card_ids: Iterable[str]) -> None:
        """要这些卡图：已经有的、排着队的（包括等重试的）、放弃了的都不再加。"""
        if not self.available or self._stopped:
            return
        added = False
        with self._lock:
            for card_id in map(art_id, card_ids):
                if not _CARD_ID.match(card_id):
                    continue
                if card_id in self._images or card_id in self._pending:
                    continue
                if card_id in self._gave_up:
                    continue
                self._pending[card_id] = self._clock()
                added = True
        if added:
            self._wake.set()

    def image(self, card_id: str) -> Image | None:
        """解码好的原图（RGB；金色卡是普通卡的原图）；还没到、没有 Pillow 或者放弃了时 None。"""
        card_id = art_id(card_id)
        with self._lock:
            image = self._images.get(card_id)
            if image is not None:
                self._images.move_to_end(card_id)
            return image

    @property
    def generation(self) -> int:
        """每到一张图加一：界面看它变了就重画。"""
        with self._lock:
            return self._generation

    def stop(self) -> None:
        """让线程退出（不等它）；之后什么都不做。"""
        self._stopped = True
        self._halt.set()
        self._wake.set()

    # ---- 后台线程 ----

    def _run(self) -> None:
        while not self._stopped:
            self._wake.clear()
            card_id = None
            try:
                card_id = self._next_ready()
                if card_id is not None:
                    self._process(card_id)
                    continue
            except Exception as exc:  # _process 自己接住了下载和解码的错误；这里是兜底
                log.debug("卡图线程出错", exc_info=True)
                if card_id is not None:
                    self._unexpected(card_id, exc)
                self._halt.wait(RETRY_SECONDS)  # 不空转；stop() 会叫醒
                continue
            self._wake.wait(self._wait_seconds())

    def _unexpected(self, card_id: str, exc: Exception) -> None:
        """处理一张卡图时意外出错：按一次失败算；连记失败都出错就这次运行里放弃它。"""
        try:
            self._failed(card_id, exc)
        except Exception:
            log.debug("记录卡图 %s 的失败时出错", card_id, exc_info=True)
            with self._lock:
                self._pending.pop(card_id, None)
                self._gave_up.add(card_id)

    def _wait_seconds(self) -> float | None:
        """到下一个等重试的卡图还有多久；没有排队的时 None（等 request 叫醒）。"""
        with self._lock:
            if not self._pending:
                return None
            return max(0.05, min(self._pending.values()) - self._clock())

    def _next_ready(self) -> str | None:
        now = self._clock()
        with self._lock:
            for card_id, due in self._pending.items():
                if due <= now:
                    return card_id
        return None

    def work_once(self) -> bool:
        """处理一个到时间的卡图（读缓存文件或下载，再解码）；没有可做的返回 False。"""
        if self._stopped or not self.available:
            return False
        card_id = self._next_ready()
        if card_id is None:
            return False
        self._process(card_id)
        return True

    def _process(self, card_id: str) -> None:
        """读缓存文件或下载、解码一张卡图；下载和解码的错误在这里记成失败。"""
        try:
            image = self._load(card_id)
        except _GiveUp as exc:
            log.debug("卡图 %s 不存在（%s），这次不再下载", card_id, exc)
            with self._lock:
                self._pending.pop(card_id, None)
                self._gave_up.add(card_id)
            return
        except Exception as exc:  # 超时、断网、5xx、不是 JPEG、解码失败……
            self._failed(card_id, exc)
            return
        with self._lock:
            self._pending.pop(card_id, None)
            self._attempts.pop(card_id, None)
            self._images[card_id] = image
            self._images.move_to_end(card_id)
            while len(self._images) > MAX_DECODED:
                self._images.popitem(last=False)
            self._generation += 1

    def _failed(self, card_id: str, exc: Exception) -> None:
        reason = _reason(exc)
        with self._lock:
            attempts = self._attempts.get(card_id, 0) + 1
            self._attempts[card_id] = attempts
            if attempts < MAX_ATTEMPTS:
                self._pending[card_id] = self._clock() + RETRY_SECONDS
                give_up = False
            else:
                self._pending.pop(card_id, None)
                self._gave_up.add(card_id)
                give_up = True
            first = give_up and reason not in self._warned
            if first:
                self._warned.add(reason)
        if first:
            log.warning("卡图下载失败（%s），对手阵容里这些牌只显示名字", reason)
        else:
            log.debug("卡图 %s 第 %d 次失败：%s", card_id, attempts, reason)

    def _load(self, card_id: str) -> Image:
        path = self._dir / f"{card_id}.jpg"
        if path.exists():
            try:
                return _decode(path.read_bytes())
            except Exception:
                log.debug("卡图缓存 %s 读不了，重新下载", path, exc_info=True)
        data = self._download(card_id)
        image = _decode(data)  # 先解码：解不开的不写进缓存
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return image

    def _download(self, card_id: str) -> bytes:
        url = ART_URL.format(card_id=card_id)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener(request) as response:
                data = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise _GiveUp(f"HTTP {exc.code}") from exc
            raise
        if len(data) > MAX_BYTES:
            raise ValueError("卡图太大")
        if data[:2] != JPEG_MAGIC:
            raise ValueError("下载到的不是 JPEG（可能是错误页）")
        return data


def _decode(data: bytes) -> Image:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")
