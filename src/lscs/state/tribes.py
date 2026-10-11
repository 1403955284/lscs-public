"""根据商店里出现过的随从推断本局种族。"""

from __future__ import annotations

from collections.abc import Iterable

from lscs.state.models import TribesConfidence

IGNORED_RACES = frozenset({"ALL", "INVALID"})


class TribeInference:
    def __init__(self, expected_count: int) -> None:
        self.expected_count = expected_count
        self._seen: set[str] = set()

    def observe(self, races: Iterable[str]) -> None:
        # 双种族随从只要任一种族在本局就会出现，不能证明某个具体种族
        candidates = [r for r in races if r not in IGNORED_RACES]
        if len(candidates) == 1:
            self._seen.add(candidates[0])

    @property
    def tribes(self) -> frozenset[str]:
        return frozenset(self._seen)

    @property
    def confidence(self) -> TribesConfidence:
        if len(self._seen) >= self.expected_count:
            return TribesConfidence.CONFIRMED
        return TribesConfidence.INFERRING
