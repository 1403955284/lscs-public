from lscs.cards.db import CardDB, clean_text

RAW = [
    {
        "id": "BG21_029",
        "name": "粗鄙小鬼",
        "type": "MINION",
        "techLevel": 1,
        "races": ["DEMON"],
        "attack": 1,
        "health": 1,
        "isBattlegroundsPoolMinion": True,
        "text": "<b>亡语：</b>召唤两个1/1的小鬼。",
        "dbfId": 72058,
    },
    {"id": "BG21_029_G", "name": "粗鄙小鬼", "type": "MINION", "techLevel": 1, "races": ["DEMON"]},
    {
        "id": "BG25_XXX",
        "name": "双人专属",
        "type": "MINION",
        "techLevel": 2,
        "isBattlegroundsPoolMinion": True,
        "isBattlegroundsDuosExclusive": True,
    },
    {
        "id": "BG28_500",
        "name": "酒馆法术",
        "type": "BATTLEGROUND_SPELL",
        "techLevel": 1,
        "isBattlegroundsPoolSpell": True,
        "race": "INVALID",
        "cost": 1,
    },
    {"id": "OLD_1", "name": "旧卡", "type": "MINION", "race": "BEAST"},
]


def test_clean_text():
    assert clean_text("[x]<b>亡语：</b>召唤\n两个") == "亡语：召唤 两个"
    assert clean_text(None) == ""


def test_lookup_and_fields():
    db = CardDB(RAW)
    imp = db.get("BG21_029")
    assert (imp.name, imp.tier, imp.races, imp.atk, imp.health) == ("粗鄙小鬼", 1, ("DEMON",), 1, 1)
    assert imp.text == "亡语：召唤两个1/1的小鬼。"
    assert db.get("OLD_1").races == ("BEAST",)  # 只有 race 字段的旧数据
    assert db.get("NOPE") is None


def test_names_including_golden_fallback():
    db = CardDB([RAW[0]])
    assert db.name("BG21_029") == "粗鄙小鬼"
    assert db.name("BG21_029_G") == "粗鄙小鬼(金)"  # 数据里没有金色卡时用普通卡的名字
    assert db.name("UNKNOWN_ID") == "UNKNOWN_ID"
    assert CardDB(RAW).name("BG21_029_G") == "粗鄙小鬼"  # 数据里有金色卡就直接用


def test_pool_minions_exclude_duos_by_default():
    db = CardDB(RAW)
    assert [c.card_id for c in db.pool_minions()] == ["BG21_029"]
    assert [c.card_id for c in db.pool_minions(include_duos=True)] == ["BG21_029", "BG25_XXX"]


def test_cost_and_pool_spells():
    db = CardDB(RAW)
    assert db.get("BG28_500").cost == 1
    assert db.get("BG21_029").cost is None  # 数据里没有费用
    assert [c.card_id for c in db.pool_spells()] == ["BG28_500"]
    duo_spell = {**RAW[3], "id": "BG28_DUO", "isBattlegroundsDuosExclusive": True}
    assert [c.card_id for c in CardDB([*RAW, duo_spell]).pool_spells()] == ["BG28_500"]


def test_from_json_file(tmp_path):
    import json

    path = tmp_path / "cards.json"
    path.write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
    assert CardDB.from_json_file(path).name("BG21_029") == "粗鄙小鬼"


def test_lookup_by_dbf_id():
    db = CardDB(RAW)
    assert db.get_by_dbf(72058).card_id == "BG21_029"
    assert db.get("BG21_029").dbf_id == 72058
    assert db.get_by_dbf(1) is None


def test_text_repeated_twice_is_collapsed():
    # HearthstoneJSON zhCN 里凯瑞尔"定罪"（BG21_HERO_000p）的原文：同一段说明重复了一遍，中间夹着"1"
    raw = (
        "随机使1个友方随从获得+1/+1。<b><b>被动</b>：</b>在每场战斗后，选择一项提升。"
        "1随机使1个友方随从获得+1/+1。<b><b>被动</b>：</b>在每场战斗后，选择一项提升。"
    )
    assert clean_text(raw) == "随机使1个友方随从获得+1/+1。被动：在每场战斗后，选择一项提升。"
    assert clean_text("召唤两个1/1的小鬼。@召唤两个1/1的小鬼。") == "召唤两个1/1的小鬼。"
    assert clean_text("召唤两个1/1的小鬼。召唤两个1/1的小鬼。") == "召唤两个1/1的小鬼。"


def test_short_or_different_halves_are_kept():
    assert clean_text("+1+1") == "+1+1"  # 太短，不当成重复
    assert clean_text("获得+1/+1") == "获得+1/+1"
    assert clean_text("嘲讽。圣盾。12嘲讽。圣盾。x") == "嘲讽。圣盾。12嘲讽。圣盾。x"


def mini_db():
    from helpers import FIXTURES

    return CardDB.from_json_file(FIXTURES / "cards_mini.json")


def test_normal_dbf_id_is_read():
    db = mini_db()
    assert db.get("TB_BaconUps_159").normal_dbf_id == 64054
    assert db.get("BG36_099_G").normal_dbf_id == 131696
    assert db.get("BG36_099").normal_dbf_id is None


def test_normal_id_maps_all_golden_spellings():
    db = mini_db()
    assert db.normal_id("BG36_099_G") == "BG36_099"  # 普通卡 + _G
    assert db.normal_id("TB_BaconUps_159") == "BGS_119"  # 旧式金色卡：battlegroundsNormalDbfId
    assert db.normal_id("BGS_119") == "BGS_119"
    assert db.normal_id("BG32_236") == "BG32_236"  # 不认识的原样返回
    assert db.normal_id("NOPE_G") == "NOPE"
    # normal_dbf_id 指向卡牌数据里没有的卡：原样返回
    lonely = CardDB([{"id": "TB_BaconUps_X", "name": "孤儿", "battlegroundsNormalDbfId": 1}])
    assert lonely.normal_id("TB_BaconUps_X") == "TB_BaconUps_X"


def test_find_pool_minion_by_name():
    db = mini_db()
    assert db.find_pool_minion("蚀脑怪").card_id == "BG36_099"
    assert db.find_pool_minion(" 蚀脑怪(金) ").card_id == "BG36_099"
    assert db.find_pool_minion("厄祖玛特的触须") is None  # 衍生物，不是卡池随从
    assert db.find_pool_minion("没有这张") is None
    assert db.find_pool_minion("") is None


def test_find_pool_minion_prefers_the_lowest_tier():
    raw = [
        {"id": "B", "name": "同名", "techLevel": 3, "isBattlegroundsPoolMinion": True},
        {"id": "A", "name": "同名", "techLevel": 2, "isBattlegroundsPoolMinion": True},
    ]
    assert CardDB(raw).find_pool_minion("同名").card_id == "A"


def test_find_pool_minion_builds_its_name_index_once(monkeypatch):
    db = mini_db()
    calls = []
    original = db.pool_minions

    def counting(include_duos=False):
        calls.append(include_duos)
        return original(include_duos)

    monkeypatch.setattr(db, "pool_minions", counting)
    assert db.find_pool_minion("蚀脑怪").card_id == "BG36_099"
    assert db.find_pool_minion("孢林卵怪").card_id == "BG36_116"
    assert db.find_pool_minion("没有这张") is None
    assert calls == [False]  # 第一次查时建索引，之后直接查
