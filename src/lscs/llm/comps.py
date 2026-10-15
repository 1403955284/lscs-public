"""本赛季流派指南 strategy/current/comps.md（整局计划与流派指南设计文档第 3 节）。

文件按固定格式写：第一行 "# 标题"，之后每个流派一段 "## 流派名"，段里第一行"种族标签："写
hearthstone 的种族枚举名（ALL 表示任何种族都能走），下面是给 AI 看的要点；没有"种族标签"行的段
（"通用运营"）总是选中。"- 来源：…"行只给人看，不发给 AI。

招募的局面消息里，卡池块和战绩块之后放一块"本赛季流派指南"：只列本局种族能走的流派（按文件
顺序，也就是强度从高到低），超过 COMPS_MAX_CHARS 时整段整段地去掉排在后面的流派，只留名字。
这一块只随本局种族变，和卡池块一起组成跨回合的缓存前缀。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from lscs.advisor.names import TRIBE_ZH

log = logging.getLogger(__name__)

# 流派指南块最多几个字（约 3600 token，在缓存前缀里）；5 个种族的大厅都放得下
COMPS_MAX_CHARS = 6000
ALL = "ALL"
_TAG = re.compile(r"^\s*(?:[-*]\s*)?种族标签\s*[:：]\s*(.*)$")
_SOURCE = re.compile(r"^\s*[-*]\s*来源\s*[:：]")
_SPLIT = re.compile(r"[,，、;；\s]+")
_ZH_TRIBES = {zh: race for race, zh in TRIBE_ZH.items()}
BLOCK_TITLE = (
    "【本赛季流派指南（{title}只列本局种族能走的流派，按强度从高到低；"
    "定计划、选核心卡时以它为准，卡名和卡池块一致）】"
)
OVERFLOW = "（还有 {count} 个流派放不下：{names}）"


@dataclass(frozen=True)
class CompGuide:
    name: str  # "## " 后面的标题
    tribes: frozenset[str] | None  # 种族标签；None 表示没有这一行（通用运营），总是选中
    body: str  # 标题和种族标签之后的正文，去掉"- 来源：…"行，首尾空白去掉

    @property
    def general(self) -> bool:
        """没有种族标签的段（通用运营）。"""
        return self.tribes is None


@dataclass(frozen=True)
class CompsFile:
    title: str  # 第一行 "# " 后面的文字
    guides: tuple[CompGuide, ...]

    @property
    def comp_count(self) -> int:
        """流派的个数（不算通用运营）。"""
        return sum(1 for g in self.guides if not g.general)


def _tribes(raw: str, section: str) -> frozenset[str]:
    """种族标签：逗号、顿号、空格分开；枚举名不分大小写，也认中文种族名（"畸变"）。
    不认识的名字记一条日志后丢掉。"""
    tribes = set()
    for token in _SPLIT.split(raw.strip()):
        if not token:
            continue
        race = token.upper() if token.upper() in TRIBE_ZH else _ZH_TRIBES.get(token)
        if race is None:
            log.warning("流派指南「%s」的种族标签里有不认识的种族：%s（已忽略）", section, token)
            continue
        tribes.add(race)
    return frozenset(tribes)


def _drop_sources(lines: Iterable[str]) -> list[str]:
    """去掉"- 来源："行和它缩进的续行。"""
    kept: list[str] = []
    in_source = False
    for line in lines:
        if _SOURCE.match(line):
            in_source = True
            continue
        if in_source and line[:1].isspace() and line.strip():
            continue  # 来源的续行
        in_source = False
        kept.append(line)
    return kept


def _guide(name: str, lines: list[str]) -> CompGuide:
    tribes: frozenset[str] | None = None
    body: list[str] = []
    for line in lines:
        match = _TAG.match(line) if tribes is None else None
        if match is not None:
            tribes = _tribes(match.group(1), name)
            continue
        body.append(line)
    return CompGuide(name, tribes, "\n".join(_drop_sources(body)).strip())


def parse_comps(text: str) -> CompsFile:
    """按 "## " 切段；第一行 "# " 是标题，标题之后到第一段之前的内容（"> 来源"引用）丢掉。
    空文件、没有任何段时 guides 为空。不抛异常。"""
    title = ""
    guides: list[CompGuide] = []
    name: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if name is not None:
                guides.append(_guide(name, lines))
            name, lines = line[3:].strip(), []
        elif name is not None:
            lines.append(line)
        elif not title and line.startswith("# "):
            title = line[2:].strip()
    if name is not None:
        guides.append(_guide(name, lines))
    return CompsFile(title, tuple(g for g in guides if g.name))


def select_comps(comps: CompsFile, tribes: frozenset[str]) -> tuple[CompGuide, ...]:
    """按文件顺序选中：通用运营、种族标签有 ALL 的、和本局种族有交集的。本局种族还不知道
    （空集）时只有前两类。"""
    return tuple(
        g for g in comps.guides if g.tribes is None or ALL in g.tribes or bool(g.tribes & tribes)
    )


def _section(guide: CompGuide) -> str:
    return f"## {guide.name}\n{guide.body}" if guide.body else f"## {guide.name}"


def comps_block(
    comps: CompsFile, tribes: frozenset[str], max_chars: int = COMPS_MAX_CHARS
) -> str | None:
    """本赛季流派指南块：标题一行，下面是选中的段（按文件顺序）。超过 max_chars 时通用运营总是
    保留，流派按顺序整段放，第一段放不下起后面的都只写名字（最后一行"还有 N 个流派放不下"）。
    一段都没选中时为 None。"""
    chosen = select_comps(comps, tribes)
    if not chosen:
        return None
    title = BLOCK_TITLE.format(title=f"{comps.title}；" if comps.title else "")
    used = len(title) + sum(len(_section(g)) + 1 for g in chosen if g.general)
    kept: list[CompGuide] = []
    dropped: list[str] = []
    for guide in chosen:
        size = len(_section(guide)) + 1
        if guide.general:
            kept.append(guide)
        elif not dropped and used + size <= max_chars:
            used += size
            kept.append(guide)
        else:
            dropped.append(guide.name)
    lines = [title, *(_section(g) for g in kept)]
    if dropped:
        lines.append(OVERFLOW.format(count=len(dropped), names="、".join(dropped)))
    return "\n".join(lines)


_NOTE = re.compile(r"[（(][^（）()]*[）)]")
_PUNCT = re.compile(r"[\s·・/／、,，;；:：+＋\-—_|]+")
MIN_SHARED = 4  # 两个名字至少按顺序共有这么多个字，才算同一个流派


def _bare(name: str) -> str:
    """比较流派名用：去掉括号里的说明、空白和标点。"""
    return _PUNCT.sub("", _NOTE.sub("", name))


def _shared(a: str, b: str) -> int:
    """a、b 按顺序共有的字数（最长公共子序列）。"""
    row = [0] * (len(b) + 1)
    for ch in a:
        prev = 0
        for j, other in enumerate(b, start=1):
            prev, row[j] = row[j], prev + 1 if ch == other else max(row[j], row[j - 1])
    return row[-1]


def match_comp(name: str, comps: CompsFile | None, undecided: str = "未定") -> str | None:
    """AI 写的流派名对应的指南标题（整局计划与流派指南修正：AI 每回合换个说法，不能算改计划）。
    去掉括号和标点后，一个包含另一个（至少 2 个字），或者按顺序共有至少 MIN_SHARED 个字、占短的那个
    的六成以上，就算同一个；几个标题一样像时不认。"未定"、没有指南、对不上时为 None。"""
    bare = _bare(name)
    if comps is None or len(bare) < 2 or bare == undecided:
        return None
    scored: list[tuple[tuple[int, int], str]] = []
    for guide in comps.guides:
        if guide.general:
            continue
        other = _bare(guide.name)
        short = min(len(bare), len(other))
        if short >= 2 and (bare in other or other in bare):
            scored.append(((2, short), guide.name))
            continue
        shared = _shared(bare, other)
        if shared >= MIN_SHARED and shared >= 0.6 * short:
            scored.append(((1, shared), guide.name))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # 一样像：不猜
    return scored[0][1]
