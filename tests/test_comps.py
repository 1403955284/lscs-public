"""本赛季流派指南的解析、挑选和组块（整局计划与流派指南设计文档第 3 节）。"""

import logging
import re

import pytest
from helpers import FIXTURES, NEEDS_REAL_CARDS, REAL_CARDS

from lscs.advisor.names import TRIBE_ZH
from lscs.cli import DEFAULT_COMPS
from lscs.llm.comps import (
    CompGuide,
    CompsFile,
    comps_block,
    match_comp,
    parse_comps,
    select_comps,
)
from lscs.llm.prompt import load_guide

SAMPLE = FIXTURES / "comps_sample.md"
NEEDS_COMPS = pytest.mark.skipif(
    not DEFAULT_COMPS.exists(), reason="还没有 strategy/current/comps.md"
)
TITLE = "第 14 赛季流派指南（测试样例，不是真实资料）"


def sample() -> CompsFile:
    return parse_comps(load_guide(SAMPLE))


def names(guides) -> list[str]:
    return [g.name for g in guides]


def test_parse_reads_the_title_and_drops_the_sources_quote():
    comps = sample()
    assert comps.title == TITLE
    assert names(comps.guides) == [
        "畸变神明",
        "海盗刀剑",
        "元素刷新",
        "中立白板",
        "通用运营（本赛季）",
    ]
    assert comps.comp_count == 4
    assert all("合成" not in g.body for g in comps.guides)  # "> 来源"和"- 来源"都不发


def test_parse_keeps_the_body_without_the_tag_and_source_lines():
    first = sample().guides[0]
    assert first == CompGuide(
        "畸变神明",
        frozenset({"ABERRATION"}),
        "- 强度：最强\n"
        "- 核心卡：蚀脑怪（2 本）、飘荡祭品（3 本）、恩拉其工兵（5 本）\n"
        "- 启动/过渡卡：虚悯者（1 本）、孢林卵怪（2 本）\n"
        "- 什么时候走：前几回合商店给了两张以上畸变\n"
        "- 节奏：第 6 回合 4 本，第 8 回合 5 本\n"
        "- 成型标志：场上有两张蚀脑怪\n"
        "- 打法要点：蚀脑怪放左边\n"
        "- 弱点：怕剧毒",
    )


def test_parse_tribe_tags_accept_ascii_colons_lowercase_and_chinese_names():
    comps = sample()
    assert comps.guides[2].tribes == frozenset({"ELEMENTAL", "NAGA"})
    assert comps.guides[3].tribes == frozenset({"ALL"})
    general = comps.guides[4]
    assert general.tribes is None and general.general
    assert general.body == "- 前期按节奏升本，血量低于 15 先保血。"


def test_unknown_tribe_names_are_dropped_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="lscs.llm.comps"):
        comps = sample()
    assert comps.guides[1].tribes == frozenset({"PIRATE"})
    assert "海盗刀剑" in caplog.text and "foo" in caplog.text


def test_tags_with_nothing_known_match_no_tribe():
    comps = parse_comps("# 标题\n## 怪流派\n种族标签：火星人\n- 核心卡：甲\n")
    assert comps.guides[0].tribes == frozenset()
    assert select_comps(comps, frozenset({"BEAST", "PIRATE"})) == ()


def test_empty_or_sectionless_files():
    assert parse_comps("") == CompsFile("", ())
    assert parse_comps("# 只有标题\n> 来源：无\n随便写点").guides == ()
    assert parse_comps("（无）").guides == ()  # load_guide 读不到文件时的内容


def test_a_byte_order_mark_or_gbk_file_does_not_crash(tmp_path):
    path = tmp_path / "comps.md"
    path.write_bytes(b"\xef\xbb\xbf" + "# 标题\n## 甲\n种族标签：BEAST\n- 要点\n".encode())
    comps = parse_comps(load_guide(path))
    assert comps.title == "标题" and comps.guides[0].tribes == frozenset({"BEAST"})
    path.write_bytes("# 标题\n## 甲\n种族标签：BEAST\n".encode("gbk"))
    assert isinstance(parse_comps(load_guide(path)), CompsFile)


def test_select_by_the_lobby_tribes_in_file_order():
    comps = sample()
    chosen = select_comps(comps, frozenset({"ABERRATION", "PIRATE"}))
    assert names(chosen) == ["畸变神明", "海盗刀剑", "中立白板", "通用运营（本赛季）"]
    assert names(select_comps(comps, frozenset({"NAGA"}))) == [
        "元素刷新",
        "中立白板",
        "通用运营（本赛季）",
    ]


def test_select_before_any_tribe_is_known_takes_only_all_and_general():
    assert names(select_comps(sample(), frozenset())) == ["中立白板", "通用运营（本赛季）"]


def test_block_lists_the_chosen_sections_under_one_title():
    block = comps_block(sample(), frozenset({"PIRATE"}))
    assert block == (
        f"【本赛季流派指南（{TITLE}；只列本局种族能走的流派，按强度从高到低；"
        "定计划、选核心卡时以它为准，卡名和卡池块一致）】\n"
        "## 海盗刀剑\n- 强度：较强\n- 核心卡：刀剑收藏家（4 本）\n"
        "## 中立白板\n- 强度：一般\n- 核心卡：铁血防御机（3 本）\n"
        "## 通用运营（本赛季）\n- 前期按节奏升本，血量低于 15 先保血。"
    )


def test_block_over_the_limit_keeps_whole_sections_and_names_the_rest():
    comps = sample()
    tribes = frozenset({"ABERRATION", "PIRATE", "ELEMENTAL"})
    full = comps_block(comps, tribes, max_chars=100_000)
    assert "## 元素刷新" in full and "放不下" not in full
    first = full.index("## 海盗刀剑")  # 只放得下标题、通用运营和第一个流派
    general = len("\n## 通用运营（本赛季）\n- 前期按节奏升本，血量低于 15 先保血。")
    block = comps_block(comps, tribes, max_chars=first + general)
    lines = block.splitlines()
    assert "## 畸变神明" in lines and "## 海盗刀剑" not in lines
    assert lines[-3:] == [
        "## 通用运营（本赛季）",
        "- 前期按节奏升本，血量低于 15 先保血。",
        "（还有 3 个流派放不下：海盗刀剑、元素刷新、中立白板）",
    ]
    assert len("\n".join(lines[:-1])) <= first + general  # 放进去的段没超过上限


def test_the_general_section_is_always_kept():
    block = comps_block(sample(), frozenset({"ABERRATION"}), max_chars=10)
    lines = block.splitlines()
    assert lines[0].startswith("【本赛季流派指南（")
    assert lines[1:] == [
        "## 通用运营（本赛季）",
        "- 前期按节奏升本，血量低于 15 先保血。",
        "（还有 2 个流派放不下：畸变神明、中立白板）",
    ]


def test_no_block_without_any_chosen_section():
    comps = parse_comps("# 标题\n## 野兽\n种族标签：BEAST\n- 要点\n")
    assert comps_block(comps, frozenset({"PIRATE"})) is None
    assert comps_block(comps, frozenset()) is None
    assert comps_block(CompsFile("", ()), frozenset({"PIRATE"})) is None


def test_block_without_a_title():
    comps = parse_comps("## 野兽\n种族标签：BEAST\n- 要点\n")
    assert (
        comps_block(comps, frozenset({"BEAST"}))
        .splitlines()[0]
        .startswith("【本赛季流派指南（只列本局种族能走的流派")
    )


# ---- 仓库里的 comps.md（研究代理整理的当月资料；还没有时跳过） ----


@NEEDS_COMPS
def test_repository_comps_file_has_tagged_sections():
    comps = parse_comps(load_guide(DEFAULT_COMPS))
    assert comps.title and comps.comp_count >= 5
    for guide in comps.guides:
        assert guide.body, guide.name
        if guide.tribes is not None:
            assert guide.tribes and guide.tribes <= set(TRIBE_ZH), guide.name


def _card_items(line: str) -> list[tuple[str, str]]:
    """ "- 核心卡：……"一行里的 (卡名, 括号里的说明)：按括号外的顿号、分号、逗号、句号切开，
    "自伤线：灰烬腐蚀者"取冒号后面的卡名。"""
    text = re.split(r"[:：]", line, maxsplit=1)[1]
    parts, depth, current = [], 0, ""
    for ch in text:
        depth += ch in "（("
        depth -= ch in "）)"
        if depth == 0 and ch in "、；;，,。":
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    items = []
    for part in parts:
        match = re.fullmatch(r"\s*(?:[^（(：:]*[:：])?\s*([^（(]+?)\s*(?:[（(](.*)[）)])?\s*", part)
        if match is not None and match.group(1):
            items.append((match.group(1), match.group(2) or ""))
    return items


def _core_cards(body: str) -> list[str]:
    line = next(line for line in body.splitlines() if line.startswith("- 核心卡"))
    return [name for name, _note in _card_items(line)]


def test_core_cards_of_a_line_with_notes_and_alternatives():
    line = "- 核心卡：希望信使（5 本，补丁加强到 4/3、+4/+3）、执念诗心龙；自伤线：灰烬腐蚀者。"
    assert _card_items(line) == [
        ("希望信使", "5 本，补丁加强到 4/3、+4/+3"),
        ("执念诗心龙", ""),
        ("灰烬腐蚀者", ""),
    ]
    assert _core_cards(line) == ["希望信使", "执念诗心龙", "灰烬腐蚀者"]


@NEEDS_COMPS
def test_repository_comp_names_fit_the_plan():
    from lscs.llm.parse import PLAN_COMP_CHARS

    for guide in parse_comps(load_guide(DEFAULT_COMPS)).guides:
        if not guide.general:  # 面板和计划里的流派名就是标题，不能被截断
            assert len(guide.name) <= PLAN_COMP_CHARS, guide.name


@NEEDS_COMPS
@NEEDS_REAL_CARDS
def test_repository_core_and_transition_cards_are_pool_minions():
    from lscs.cards.db import CardDB

    cards = CardDB.from_json_file(REAL_CARDS)
    for guide in parse_comps(load_guide(DEFAULT_COMPS)).guides:
        if guide.general:
            continue
        lines = [
            line
            for line in guide.body.splitlines()
            if line.startswith(("- 核心卡", "- 启动/过渡卡"))
        ]
        assert lines[0].startswith("- 核心卡"), guide.name
        for card, note in (item for line in lines for item in _card_items(line)):
            info = cards.find_pool_minion(card)
            assert info is not None, (guide.name, card)
            # 中立、全种族、和本流派同种族的牌，或者标明了"……在时"（不在本局就刷不到）
            tribes = guide.tribes - {"ALL"}
            races = set(info.races)
            fits = not races or "ALL" in races or bool(races & tribes) or "在时" in note
            assert fits, (guide.name, card, sorted(races))


# ---- AI 写的流派名对到指南标题（实测：AI 每回合换个说法，不能算改计划） ----

HEADINGS = CompsFile(
    "标题",
    tuple(
        CompGuide(name, frozenset({"ALL"}), "- 要点")
        for name in (
            "守门融合怪喝茶流",
            "邪能野猪人商店吞噬流",
            "野猪人宝石流",
            "亡灵宰割流",
            "元素商店强化流",
            "恶魔商店强化吞噬流",
            "圣盾龙",
            "畸变怪养神流",
            "海盗金币流",
        )
    )
    + (CompGuide("通用运营（本赛季）", None, "- 要点"),),
)


@pytest.mark.parametrize(
    ("name", "heading"),
    [
        ("畸变养神（亡语/弃牌）", "畸变怪养神流"),  # 实测第 1 种说法
        ("畸变怪养神流（亡语/弃牌）", "畸变怪养神流"),  # 实测第 2 种说法
        ("畸变怪养神流", "畸变怪养神流"),
        ("畸变 养神", "畸变怪养神流"),
        ("恶魔吞噬流", "恶魔商店强化吞噬流"),
        ("邪能野猪人流", "邪能野猪人商店吞噬流"),
        ("圣盾龙流", "圣盾龙"),  # 一个包含另一个
        ("喝茶流", "守门融合怪喝茶流"),
        ("海盗", "海盗金币流"),
    ],
)
def test_rephrased_comp_names_match_the_guide_heading(name, heading):
    assert match_comp(name, HEADINGS) == heading


@pytest.mark.parametrize("name", ["畸变神明", "亡灵复生流", "龙", "未定", "", "通用运营"])
def test_unrelated_short_or_undecided_names_do_not_match(name):
    assert match_comp(name, HEADINGS) is None


def test_equally_close_headings_are_not_guessed_and_no_guide_means_no_match():
    one = CompsFile(
        "", (CompGuide("甲乙丙丁流", None, ""), CompGuide("甲乙丙丁派", frozenset({"ALL"}), ""))
    )
    assert match_comp("甲乙丙丁", one) == "甲乙丙丁派"  # 通用段不参与，只剩一个
    both = CompsFile(
        "",
        (
            CompGuide("甲乙丙丁流", frozenset({"ALL"}), ""),
            CompGuide("甲乙丙丁派", frozenset({"ALL"}), ""),
        ),
    )
    assert match_comp("甲乙丙丁", both) is None  # 一样像：不猜
    assert match_comp("畸变怪养神流", None) is None
