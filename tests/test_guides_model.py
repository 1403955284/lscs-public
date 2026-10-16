"""HSReplay 流派指南的解析、翻译和转成流派指南的段（流派指南自动更新设计文档第 3.2、3.3 节）。"""

import json
from datetime import UTC, date

import pytest
from helpers import (
    FIXTURES,
    GUIDES_FILE,
    NEEDS_GUIDES_FIXTURES,
    fixture_live_guides,
    guides_cards,
)

from lscs.guides.model import LIVE_MAX_CHARS, load_live_guides, to_comps_file
from lscs.llm.comps import CompsFile, comps_block, parse_comps, select_comps
from lscs.llm.parse import PLAN_COMP_CHARS
from lscs.llm.prompt import load_guide

pytestmark = NEEDS_GUIDES_FIXTURES
MANUAL = FIXTURES / "comps_sample.md"


def manual() -> CompsFile:
    return parse_comps(load_guide(MANUAL))


def by_name(live):
    return {g.name: g for g in live.guides}


def raw_entry(name):
    """夹具（不进公开仓库）里这一条的原样内容。"""
    return next(e for e in json.loads(GUIDES_FILE.read_text(encoding="utf-8")) if e["name"] == name)


def translated(text):
    """第 3.2 节的规则：[[英文名||dbf]] → 卡牌数据里的名字（查不到留英文名），换行换成" / "，
    连续空白合一。"""
    import re

    cards = guides_cards()

    def card(match):
        info = cards.get_by_dbf(int(match.group(2)))
        return info.name if info is not None else match.group(1).strip()

    text = re.sub(r"\[\[([^\]|]+)\|\|(\d+)\]\]", card, text)
    return " / ".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def test_counts_fresh_stale_and_hidden():
    live = fixture_live_guides()
    # 新鲜 6 条 + 假条目；补丁前 2 条；隐藏的拷贝不算
    assert len(live.guides) == 7
    # 全部 = 新鲜的 + 补丁前的（文件里 10 条，隐藏的那条不算，启动行的数才加得起来）
    assert (live.total, live.stale) == (9, 2)
    assert live.patch_date == date(2026, 10, 1)
    assert live.newest == date(2026, 10, 15)  # 假条目最新
    assert live.cached_age is None
    assert all(g.guide_id != 9001 for g in live.guides)
    assert "Beasts - Tasty Lobstah" not in by_name(live)


def test_sorted_by_tier_rank_and_name():
    names = [g.name for g in fixture_live_guides().guides]
    assert names == [
        "Menagerie",  # 1 梯队第 0 位
        "Demons - Self Damage",  # 1 梯队第 2 位
        "Murlocs - Keyword",  # 2 梯队
        "Test - Broken",  # tier 9 → 3，第 0 位
        "Aberrations - Deathrattle/Spells",  # 3 梯队第 1 位，按英文名
        "Aberrations - Sludge",
        "Dragons - Copebringer",
    ]


def test_fields_of_a_guide():
    sludge = by_name(fixture_live_guides())["Aberrations - Sludge"]
    assert (sludge.guide_id, sludge.tribe, sludge.tier, sludge.tier_rank) == (
        97,
        "ABERRATION",
        3,
        1,
    )
    assert sludge.difficulty == 2 and sludge.updated == date(2026, 10, 6)
    assert sludge.display == "畸变·梦魇侵蚀怪"
    assert sludge.core_cards == (
        "达卡莱附魔师",
        "邪火咒龙",
        "梦魇侵蚀怪",
        "神秘的克熙尔",
        "黑暗傀儡师",
    )
    assert sludge.addon_cards == ("恩拉其工兵", "摧心招募者", "恩拉其唤冰者")
    # 正文是 HSReplay 的英文原文：不在公开的测试里抄写，和夹具里的原文按规则比
    raw = raw_entry("Aberrations - Sludge")
    for field in ("summary", "when_to_commit", "how_to_play"):
        assert getattr(sludge, field) == translated(raw[field]), field
        assert "[[" not in getattr(sludge, field), field
    assert "梦魇侵蚀怪" in sludge.when_to_commit and "恩拉其唤冰者" in sludge.when_to_commit
    assert "达卡莱附魔师" in sludge.how_to_play


def test_menagerie_is_neutral_and_display_names_fit_the_plan():
    live = fixture_live_guides()
    menagerie = by_name(live)["Menagerie"]
    assert menagerie.tribe is None and menagerie.display == "中立·守门融合怪"
    assert by_name(live)["Murlocs - Keyword"].display == "鱼人·下水道逃生鱼人"
    assert all(len(g.display) <= PLAN_COMP_CHARS for g in live.guides)
    assert len({g.display for g in live.guides}) == len(live.guides)


def test_broken_entry_is_repaired_not_dropped():
    broken = by_name(fixture_live_guides())["Test - Broken"]
    assert (broken.tier, broken.difficulty) == (3, 2)  # 9 → 3（最弱），7 → 2（中等）
    assert broken.display == "野兽·Broken"  # 代表卡查不到：用英文名 " - " 后面的部分
    assert broken.core_cards == ("美味龙虾",)  # dbf 999999 查不到，跳过
    assert broken.how_to_play == "Find Nobody and / win with 美味龙虾."  # 查不到的留英文名
    assert broken.summary == "Line one / Line two"  # 换行换成" / "


def test_stale_guides_filter_out_cards_outside_the_shop():
    live = fixture_live_guides("2026-09-01")
    deity = by_name(live)["Aberrations - APM Deity"]
    assert "惧之煞" not in deity.core_cards  # 7 本
    assert deity.core_cards == ("空军上将罗杰斯", "骄傲的私掠者", "疑之影")
    assert "Beasts - Tasty Lobstah" not in by_name(live)  # 08-27 还是太早


def test_card_references_become_chinese_names():
    keyword = by_name(fixture_live_guides())["Murlocs - Keyword"]
    raw = raw_entry("Murlocs - Keyword")
    assert "[[Sewer Escapee||132881]]" in raw["when_to_commit"]
    assert keyword.when_to_commit == translated(raw["when_to_commit"])
    assert "下水道逃生鱼人" in keyword.when_to_commit and "布莱恩·铜须" in keyword.when_to_commit
    assert "[[" not in keyword.how_to_play and "下水道逃生鱼人" in keyword.how_to_play


def test_a_later_patch_date_leaves_only_newer_guides():
    live = fixture_live_guides("2026-10-14")
    assert [g.name for g in live.guides] == ["Test - Broken"]
    assert live.stale == 8


def test_fetched_time_is_the_cache_files_mtime(tmp_path):
    import os

    path = tmp_path / "g.json"
    path.write_text(GUIDES_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(path, (1_760_000_000, 1_760_000_000))
    live = load_live_guides(path, date(2026, 10, 1), guides_cards(), tz=UTC)
    assert live.fetched.timestamp() == 1_760_000_000 and live.fetched.tzinfo is UTC


def test_bad_entries_are_skipped_and_a_non_list_is_an_error(tmp_path):
    entries = json.loads(GUIDES_FILE.read_text(encoding="utf-8"))
    entries += [
        {"name": "No id", "core_cards": []},
        {"id": 5, "name": 3, "core_cards": []},
        {"id": 6, "name": "No date", "core_cards": [], "primary_tribe": 14},
        "not an object",
    ]
    path = tmp_path / "g.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    live = load_live_guides(path, date(2026, 10, 1), guides_cards(), tz=UTC)
    assert len(live.guides) == 7 and live.total == 10  # 有误的三条不算
    assert live.stale == 3  # 没有日期的当作补丁前
    path.write_text('{"detail": "x"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_live_guides(path, date(2026, 10, 1), guides_cards(), tz=UTC)


# ---- 转成流派指南的段（第 3.3 节） ----


def test_to_comps_file_sections():
    comps = to_comps_file(fixture_live_guides(), manual())
    sludge = next(g for g in comps.guides if g.name == "畸变·梦魇侵蚀怪")
    live = by_name(fixture_live_guides())["Aberrations - Sludge"]
    assert sludge.body == "\n".join(
        [
            # 第 3 梯队里 tier_rank 1 有三条（希望信使、无面转化者、梦魇侵蚀怪）：不写"第 N 位"
            "- 强度：第 3 梯队；难度 中；HSReplay 10-06 更新",
            f"- 一句话：{live.summary}",
            "- 核心卡：达卡莱附魔师（5 本）、邪火咒龙（5 本）、梦魇侵蚀怪（4 本）、神秘的克熙尔（5 本）、"
            "黑暗傀儡师（6 本）",
            "- 配合卡：恩拉其工兵（5 本）、摧心招募者（4 本）、恩拉其唤冰者（5 本）",
            f"- 什么时候走：{live.when_to_commit}",
            f"- 打法要点（英文原文）：{live.how_to_play}",
            "- 英文名：Aberrations - Sludge",
        ]
    )
    assert sludge.tribes == frozenset({"ABERRATION"})
    assert sludge.core_cards == (
        "达卡莱附魔师",
        "邪火咒龙",
        "梦魇侵蚀怪",
        "神秘的克熙尔",
        "黑暗傀儡师",
    )
    assert sludge.aliases == ("Aberrations - Sludge",)
    assert (sludge.tier, sludge.difficulty) == (3, 2)
    menagerie = comps.guides[0]
    assert menagerie.name == "中立·守门融合怪" and menagerie.tribes == frozenset({"ALL"})


def test_to_comps_file_skips_empty_lines():
    broken = next(
        g for g in to_comps_file(fixture_live_guides(), None).guides if g.name == "野兽·Broken"
    )
    assert broken.body == (
        "- 强度：第 3 梯队第 1 位；难度 中；HSReplay 10-15 更新\n"
        "- 一句话：Line one / Line two\n"
        "- 核心卡：美味龙虾（3 本）\n"
        "- 打法要点（英文原文）：Find Nobody and / win with 美味龙虾.\n"
        "- 英文名：Test - Broken"
    )


def test_to_comps_file_title_order_limit_and_the_general_section():
    comps = to_comps_file(fixture_live_guides(), manual())
    assert comps.title == (
        "第 14 赛季流派指南（数据来自 HSReplay，10-15 更新，7 条本补丁的指南；"
        "强度写梯队，同一梯队里新手优先难度低的；打法要点是英文原文）"
    )
    assert comps.max_chars == LIVE_MAX_CHARS == 10500
    assert [g.name for g in comps.guides] == [
        "中立·守门融合怪",
        "恶魔·灰烬腐蚀者",
        "鱼人·下水道逃生鱼人",
        "野兽·Broken",
        "畸变·无面转化者",
        "畸变·梦魇侵蚀怪",
        "龙·希望信使",
        "通用运营（本赛季）",  # comps.md 的通用运营接在最后
    ]
    assert comps.guides[-1] == manual().guides[-1]
    assert comps.comp_count == 7


def test_to_comps_file_without_comps_md():
    comps = to_comps_file(fixture_live_guides(), None)
    assert comps.title.startswith("本赛季流派指南（数据来自 HSReplay，")
    assert all(not g.general for g in comps.guides)


def test_selected_sections_follow_the_lobby():
    comps = to_comps_file(fixture_live_guides(), manual())
    chosen = select_comps(comps, frozenset({"ABERRATION", "MURLOC"}))
    assert [g.name for g in chosen] == [
        "中立·守门融合怪",
        "鱼人·下水道逃生鱼人",
        "畸变·无面转化者",
        "畸变·梦魇侵蚀怪",
        "通用运营（本赛季）",
    ]
    block = comps_block(comps, frozenset({"ABERRATION", "MURLOC"}))
    assert block.startswith("【本赛季流派指南（第 14 赛季流派指南（数据来自 HSReplay，")
    assert "放不下" not in block


def test_repeated_display_names_get_a_number(tmp_path):
    entries = json.loads(GUIDES_FILE.read_text(encoding="utf-8"))
    copy = dict(entries[0], id=777, name="Aberrations - Sludge Again", tier_rank=5)
    path = tmp_path / "g.json"
    path.write_text(json.dumps([*entries, copy]), encoding="utf-8")
    live = load_live_guides(path, date(2026, 10, 1), guides_cards(), tz=UTC)
    names = {g.name: g.display for g in live.guides}
    assert names["Aberrations - Sludge"] == "畸变·梦魇侵蚀怪"
    assert names["Aberrations - Sludge Again"] == "畸变·梦魇侵蚀怪2"


def test_long_display_names_are_cut_to_the_plan_limit(tmp_path):
    entries = json.loads(GUIDES_FILE.read_text(encoding="utf-8"))
    entry = dict(entries[0], id=778, representative_card="BG99_NOPE", name="X - " + "Y" * 20)
    path = tmp_path / "g.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    live = load_live_guides(path, date(2026, 10, 1), guides_cards(), tz=UTC)
    assert live.guides[0].display == "畸变·" + "Y" * 9
    assert len(live.guides[0].display) == PLAN_COMP_CHARS


def test_the_rank_is_written_only_when_unique_in_its_tier():
    comps = to_comps_file(fixture_live_guides(), None)
    strength = {g.name: g.body.splitlines()[0] for g in comps.guides}
    assert strength["中立·守门融合怪"] == "- 强度：第 1 梯队第 1 位；难度 中；HSReplay 10-05 更新"
    assert strength["鱼人·下水道逃生鱼人"].startswith("- 强度：第 2 梯队第 1 位；")
    for name in ("畸变·无面转化者", "畸变·梦魇侵蚀怪", "龙·希望信使"):  # 都是第 3 梯队、tier_rank 1
        assert strength[name].startswith("- 强度：第 3 梯队；"), name
    assert strength["野兽·Broken"].startswith("- 强度：第 3 梯队第 1 位；")  # tier_rank 0 只有它
