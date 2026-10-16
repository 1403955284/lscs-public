"""本赛季流派指南 strategy/current/comps.md（整局计划与流派指南设计文档第 3 节）。

文件按固定格式写：第一行 "# 标题"，之后每个流派一段 "## 流派名"，段里第一行"种族标签："写
hearthstone 的种族枚举名（ALL 表示任何种族都能走），下面是给 AI 看的要点；没有"种族标签"行的段
（"通用运营"）总是选中。"- 来源：…"行只给人看，不发给 AI。

招募的局面消息里，卡池块和战绩块之后放一块"本赛季流派指南"：只列本局种族能走的流派（按文件
顺序，也就是强度从高到低），超过 CompsFile.max_chars（comps.md 是 COMPS_MAX_CHARS）时整段整段地
去掉排在后面的流派，只留名字。这一块只随本局种族变，和卡池块一起组成跨回合的缓存前缀。

每段的"- 核心卡："行解析成 CompGuide.core_cards（商店核心卡标记用，流派指南自动更新设计文档
第 5 节）。打开 [guides] 时，HSReplay 的指南（guides.model.to_comps_file）也写成同样的 CompsFile
代替这里的流派段；它的段带英文名作别名（match_comp 认）、梯队和难度。
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
    core_cards: tuple[str, ...] = ()  # "- 核心卡："行里的卡名（去重保序）；商店核心卡标记用
    aliases: tuple[str, ...] = ()  # match_comp 也认的别名（HSReplay 的英文名）
    tier: int | None = None  # HSReplay 的梯队（1~3）；comps.md 的段没有
    difficulty: int | None = None  # HSReplay 的难度（1 低、2 中、3 高）；comps.md 的段没有

    @property
    def general(self) -> bool:
        """没有种族标签的段（通用运营）。"""
        return self.tribes is None


@dataclass(frozen=True)
class CompsFile:
    title: str  # 第一行 "# " 后面的文字
    guides: tuple[CompGuide, ...]
    max_chars: int = COMPS_MAX_CHARS  # 流派指南块最多几个字（HSReplay 的是 LIVE_MAX_CHARS）

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


_CORE = re.compile(r"^\s*[-*]\s*核心卡\s*[:：](.*)$")
_CARD_NOTE = re.compile(r"[（(][^（）()]*[）)]")
_CARD_SPLIT = re.compile(r"[、，,；;。]")
_PREFIX = re.compile(r"^.*[:：]")


def core_card_names(text: str) -> tuple[str, ...]:
    """核心卡行（"- 核心卡："）后面的卡名：去掉括号里的说明（"（5 本，补丁加强到 4/3）"），
    按顿号、逗号、分号、句号切开，去掉"弃牌线："这类冒号前缀；去重保序。"""
    previous = None
    while previous != text:  # 括号里还有括号时一层一层去掉
        previous, text = text, _CARD_NOTE.sub("", text)
    names: list[str] = []
    for part in _CARD_SPLIT.split(text):
        card = _PREFIX.sub("", part).strip()
        if card and card not in names:
            names.append(card)
    return tuple(names)


def _guide(name: str, lines: list[str]) -> CompGuide:
    tribes: frozenset[str] | None = None
    body: list[str] = []
    for line in lines:
        match = _TAG.match(line) if tribes is None else None
        if match is not None:
            tribes = _tribes(match.group(1), name)
            continue
        body.append(line)
    kept = _drop_sources(body)
    core = next((m.group(1) for m in map(_CORE.match, kept) if m is not None), "")
    return CompGuide(name, tribes, "\n".join(kept).strip(), core_cards=core_card_names(core))


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


def _block_title(comps: CompsFile) -> str:
    return BLOCK_TITLE.format(title=f"{comps.title}；" if comps.title else "")


def kept_comps(
    comps: CompsFile, tribes: frozenset[str], max_chars: int | None = None
) -> tuple[tuple[CompGuide, ...], tuple[str, ...]]:
    """流派指南块里放得下的段（按文件顺序，通用运营总是在）和放不下、只写名字的流派。超过
    max_chars（默认 comps.max_chars）时流派按顺序整段放，第一段放不下起后面的都放不下。
    comps_block 和商店核心卡标记（advisor.core_cards）都按它：面板不标 AI 没收到的流派的核心卡。"""
    chosen = select_comps(comps, tribes)
    if max_chars is None:
        max_chars = comps.max_chars
    used = len(_block_title(comps)) + sum(len(_section(g)) + 1 for g in chosen if g.general)
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
    return tuple(kept), tuple(dropped)


def comps_block(
    comps: CompsFile, tribes: frozenset[str], max_chars: int | None = None
) -> str | None:
    """本赛季流派指南块：标题一行，下面是选中的段（按文件顺序，见 kept_comps），放不下的流派在
    最后一行"还有 N 个流派放不下"里只写名字。一段都没选中时为 None。"""
    kept, dropped = kept_comps(comps, tribes, max_chars)
    if not kept and not dropped:
        return None
    lines = [_block_title(comps), *(_section(g) for g in kept)]
    if dropped:
        lines.append(OVERFLOW.format(count=len(dropped), names="、".join(dropped)))
    return "\n".join(lines)


_NOTE = re.compile(r"[（(][^（）()]*[）)]")
_PUNCT = re.compile(r"[\s·・/／、,，;；:：+＋\-—_|]+")
MIN_SHARED = 4  # 两个名字至少按顺序共有这么多个字，才算同一个流派


def _bare(name: str) -> str:
    """比较流派名用：去掉括号里的说明、空白和标点，英文字母转小写。"""
    return _PUNCT.sub("", _NOTE.sub("", name)).lower()


def _shared(a: str, b: str) -> int:
    """a、b 按顺序共有的字数（最长公共子序列）。"""
    row = [0] * (len(b) + 1)
    for ch in a:
        prev = 0
        for j, other in enumerate(b, start=1):
            prev, row[j] = row[j], prev + 1 if ch == other else max(row[j], row[j - 1])
    return row[-1]


def _score(bare: str, other: str, fuzzy: bool = True) -> tuple[int, int] | None:
    """两个去掉标点的名字有多像：一个包含另一个 (2, 短的长度)，按顺序共有的字够多 (1, 共有的字数；
    fuzzy 为假时不看这一条)，都不是时 None。"""
    short = min(len(bare), len(other))
    if short >= 2 and (bare in other or other in bare):
        return (2, short)
    if not fuzzy:
        return None
    shared = _shared(bare, other)
    if shared >= MIN_SHARED and shared >= 0.6 * short:
        return (1, shared)
    return None


def match_comp(name: str, comps: CompsFile | None, undecided: str = "未定") -> str | None:
    """AI 写的流派名对应的指南标题（整局计划与流派指南修正：AI 每回合换个说法，不能算改计划）。
    去掉括号和标点后，一个包含另一个（至少 2 个字），或者按顺序共有至少 MIN_SHARED 个字、占短的那个
    的六成以上，就算同一个；几个标题一样像时不认。标题之外也比别名（HSReplay 的英文名，
    不分大小写），别名只认"一个包含另一个"（英文字母按顺序共有几个太容易凑上："General" 和
    "Aberrations - Deathrattle/Spells" 共有 6 个），对上别名时照样返回标题。"未定"、没有指南、
    对不上时为 None。"""
    bare = _bare(name)
    if comps is None or len(bare) < 2 or bare == _bare(undecided):
        return None
    scored: list[tuple[tuple[int, int], str]] = []
    for guide in comps.guides:
        if guide.general:
            continue
        scores = [_score(bare, _bare(guide.name))]
        scores += [_score(bare, _bare(alias), fuzzy=False) for alias in guide.aliases]
        best = max((score for score in scores if score is not None), default=None)
        if best is not None:
            scored.append((best, guide.name))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # 一样像：不猜
    return scored[0][1]
