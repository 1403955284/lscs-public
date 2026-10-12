from pathlib import Path

from factory import hand_minion, make_state, minion, power, shop_minion
from helpers import FIXTURES, TEST_POOL

from lscs.advisor.context import build_context
from lscs.advisor.rules_config import load_rules_config
from lscs.cards.db import CardDB
from lscs.llm.prompt import (
    AIMemory,
    build_hero_message,
    build_hero_system_prompt,
    build_system_prompt,
    build_user_message,
    card_pool_block,
    load_guide,
    repair_text,
)
from lscs.pipeline import Pipeline
from lscs.state.models import EventType, OpponentView
from lscs.state.tracker import StateTracker

ROOT = Path(__file__).resolve().parents[1]
CARDS = CardDB.from_json_file(FIXTURES / "cards_mini.json")
RULES = load_rules_config(ROOT / "strategy" / "current" / "rules.toml")

POOL_TURN_2 = """【本局卡池（本局种族的随从和中立随从，按酒馆等级）】
[1本] 爆裂飓风 2/1（元素）
[2本] 熔岩潜伏者 2/5（纳迦）每回合从手牌对本随从使用的第一张塑造法术的法术牌永久有效。（还剩1张！）
[4本] 刷新畸体 4/5（元素）战吼：获得2次免费的刷新。
【酒馆法术】
[5本 法术 3金] 护甲储备：将你的护甲值变为5点。"""

OPPONENTS_TITLE = (
    "【对手】按名次；▶ 下一个对手；★ 最强威胁（0~100，按最近见到的阵容、酒馆等级和血量粗估）。"
    "阵容是最近一次交手时看到的，之后可能变强了。同一种族被多家主打时，这个种族的牌更难凑。"
)
# 拉卡尼休第 1 回合交过手（金色小鬼和刷新畸体），费林大使是下一个对手、还没交过手
OPPONENTS_TURN_2 = (
    OPPONENTS_TITLE
    + """
主打种族：元素 1 家、未知 1 家
★ 第 2 名 拉卡尼休 | 血量 30+0 | 酒馆 1 本 | 威胁 78 | 第 1 回合见到 2 个随从，元素为主（1 个），总身材 9，金色 1 张，主力：粗鄙小鬼(金)、刷新畸体
▶ 第 3 名 费林大使 | 血量 30+0 | 酒馆 1 本 | 威胁 23 | 还没见过阵容"""
)

# 这份合成日志没有选项列表，所以英雄技能是"还不确定"（真实日志见 test_hero_power_line_*）
EXPECTED_TURN_2 = (
    POOL_TURN_2
    + "\n\n"
    + OPPONENTS_TURN_2
    + """

第 2 回合（招募）| 英雄：米尔豪斯·法力风暴 | 血量 27+0 | 铸币 4 | 酒馆 1 本
升本 4 金 | 刷新 1 金 | 买牌 2 金 | 卖牌 +1 金
预算：铸币 4，只买随从最多 2 张；随从上限 7，现在 1 个（场面 1 + 手牌随从 0），还能再买 6 个随从；英雄技能现在不能用
英雄技能：法力风暴（还不确定本回合能不能用——不要安排 hero_power）：购买随从和刷新的消耗改为（2）枚铸币。升级酒馆所需的铸币增加（1）枚。
本局种族：恶魔、元素、纳迦、海盗（推断中）
商店：
  [#50] 熔岩潜伏者 2/5 | 2 本 | 纳迦 | 每回合从手牌对本随从使用的第一张塑造法术的法术牌永久有效。（还剩1张！）
  [#51] 护甲储备（法术） | 将你的护甲值变为5点。
场面（已用 1/7）：
  [#31] 海盗无赖 3/1 | 1 本 | 海盗 | 亡语：召唤一个1/1的海盗并使其立即发起攻击。
手牌：
  （空）"""
)

EXPECTED_SYSTEM = """你是《炉石传说》酒馆战棋的高分教练，正在给一位 6000 分以下的新手实时指挥招募阶段的操作。目标是稳定进前四、争取第一。请用简体中文，具体、直接。

【先分析，再给操作】
先在 analysis 里用两三句话写清：现在是什么阶段、血量和场面强不强、经济怎么样；本局准备走什么方向（种族/流派）以及理由；本回合的目标（升本？补战力？找对子三连？找核心卡？）。然后再按这个目标给操作。
summary 用一句话（25 字以内）直接写本回合要做什么、为了什么，它会显示在游戏画面上的小面板里，所以要短、要完整，不要半句话。

【运营方法】
- 阶段：
  - 前期（1~3 本，大约第 1~5 回合）：按节奏升本；剩下的铸币买当前最能打、或者有成长/配合/经济价值的随从。前期身材重要，但同样身材时优先带圣盾、复生、能成长或能给钱的牌。
  - 中期（4 本，大约第 6~8 回合）：根据场面、商店和本局种族定下一个主方向，开始找这个方向的核心卡；手里有对子就值得刷新找第三张；三连发现的牌优先选方向核心。
  - 后期（5~6 本）：围绕核心卡成型；铸币主要用来刷新找核心卡、三连，以及把方向外的过渡白板换成方向内的牌。
- 牌的价值不只是身材：能成长（每回合/每次触发变大）、和场上种族或机制配合、提供经济（给铸币、免费刷新、减费）、带关键词（圣盾、复生、剧毒、风怒）的牌，往往比一张身材大但没效果的白板更值钱。酒馆法术（加属性、给铸币）也是资源。
- 刷新：**刷新后程序会立刻把新商店发给你，你会再给一次建议，所以可以放心建议刷新。** 这些情况应该刷新：该买的买完了还有铸币；商店里没有对方向有用的牌；在找三连的第三张或核心卡。不要为了凑合而买一张对阵容没帮助的白板。
- 升本：参考攻略里的节奏；血量健康、场面不差时可以提前升本；连续掉血、场面明显偏弱时先补战力。升本的回合，剩下的铸币再买牌或刷新。
- 冻结：只在商店里有核心卡/三连需要的牌、但这回合买不起时冻结。
- 卖牌：场面满了要买关键牌时，卖掉对方向没用、身材最小的过渡牌（卖出 +1 金）。被养大的随从、金色随从和方向核心不要卖。
- 三连：凑成三连会拿到金色随从并发现一张高一本的牌，是重要的强度来源。

【必须遵守的规则】
1. 只能使用局面中给出的实体编号（#数字）：buy 只能买"商店"里的，sell 只能卖"场面"上的。
2. 每个动作都要填 gold_after（执行完这一步后剩多少铸币，不能小于 0）和 minions_after（场面 + 手牌随从一共几个，买 +1、卖 -1、凑成三连 -2），任何一步都不能超过随从上限。
3. 最多 5 个动作；refresh 要放在最后（之后看不到新商店，所以 refresh 后面不要再冻结）；freeze 只能放在最后。
4. 英雄技能：局面里写了"不要安排 hero_power"时绝对不要安排；可以用时考虑使用时机（买来的随从在手牌里，要先打出去，给场上随从加属性的技能才会作用到它）；需要目标时 entity_id 填目标编号。
5. 不要建议发现选择、黑暗之赐或调整站位。

只输出一个 json 对象，格式：
{"analysis": "局势判断、本局方向、本回合目标（两三句）", "actions": [{"type": "buy", "entity_id": 1689, "gold_after": 4, "minions_after": 5, "reason": "一句话理由"}, {"type": "refresh", "gold_after": 3, "minions_after": 5, "reason": "..."}], "summary": "本回合目标，25 字以内，例如：补两张畸变铺场，下回合再升 4 本", "direction": "方向/流派", "look_for": ["接下来要找的牌名"], "confidence": 0.7}
动作类型：buy（买）、sell（卖）、upgrade（升本）、refresh（刷新）、freeze（冻结）、hero_power（英雄技能）。buy 和 sell 必须带 entity_id。

【攻略要点】
要点"""


def recruit_state(mini_lines, turn):
    found = []
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: found.append((s, e)))
    for line in mini_lines:
        pipeline.push(line)
    pipeline.flush()
    return next(
        s
        for s, ev in found
        if any(e.type == EventType.RECRUIT_STARTED and e.turn == turn for e in ev)
    )


def full_message(state, memory=None, cards=CARDS, pool=None, all_opponents=True):
    ctx = build_context(state, RULES.buy_cost_for(state.me.hero_card_id), pool=pool)
    return build_user_message(state, ctx, cards, memory, all_opponents=all_opponents)


def message_parts(state, memory=None, **kwargs):
    """（本局卡池, 对手, 局面）三段，用空行分隔。"""
    pool, opponents, situation = full_message(state, memory, **kwargs).split("\n\n", 2)
    assert pool.startswith("【本局卡池") and opponents.startswith("【对手】")
    return pool, opponents, situation


def message_for(state, memory=None, **kwargs):
    """去掉开头的本局卡池和对手，只看局面部分。"""
    return message_parts(state, memory, **kwargs)[2]


def test_user_message_snapshot(mini_lines):
    assert full_message(recruit_state(mini_lines, 2)) == EXPECTED_TURN_2


def test_rule_plans_are_not_sent(mini_lines):
    text = full_message(recruit_state(mini_lines, 2))
    assert "规则建议" not in text and "A. " not in text


POOL_RAW = [
    {
        "id": "D2",
        "name": "B恶魔",
        "type": "MINION",
        "techLevel": 1,
        "races": ["DEMON"],
        "attack": 2,
        "health": 2,
        "isBattlegroundsPoolMinion": True,
        "text": "<b>嘲讽</b>",
    },
    {
        "id": "D1",
        "name": "A恶魔",
        "type": "MINION",
        "techLevel": 1,
        "races": ["DEMON"],
        "attack": 1,
        "health": 3,
        "isBattlegroundsPoolMinion": True,
    },
    {
        "id": "BEAST",
        "name": "野兽",
        "type": "MINION",
        "techLevel": 1,
        "races": ["BEAST"],
        "attack": 3,
        "health": 3,
        "isBattlegroundsPoolMinion": True,
    },
    {
        "id": "DUAL",
        "name": "双种族",
        "type": "MINION",
        "techLevel": 2,
        "races": ["BEAST", "MURLOC"],
        "attack": 2,
        "health": 4,
        "isBattlegroundsPoolMinion": True,
    },
    {
        "id": "NEUTRAL",
        "name": "中立随从",
        "type": "MINION",
        "techLevel": 3,
        "attack": 3,
        "health": 3,
        "isBattlegroundsPoolMinion": True,
        "text": "战吼：获得一个铸币。",
    },
    {
        "id": "ALL",
        "name": "全能",
        "type": "MINION",
        "techLevel": 2,
        "races": ["ALL"],
        "attack": 2,
        "health": 1,
        "isBattlegroundsPoolMinion": True,
    },
    {
        "id": "DUOS",
        "name": "双人专属",
        "type": "MINION",
        "techLevel": 1,
        "races": ["DEMON"],
        "attack": 1,
        "health": 1,
        "isBattlegroundsPoolMinion": True,
        "isBattlegroundsDuosExclusive": True,
    },
    {
        "id": "OLD",
        "name": "已退环境",
        "type": "MINION",
        "techLevel": 1,
        "races": ["DEMON"],
        "attack": 1,
        "health": 1,
    },
    {
        "id": "T7",
        "name": "七本恶魔",
        "type": "MINION",
        "techLevel": 7,
        "races": ["DEMON"],
        "attack": 9,
        "health": 9,
        "isBattlegroundsPoolMinion": True,
    },
    {
        "id": "S7",
        "name": "七本法术",
        "type": "BATTLEGROUND_SPELL",
        "techLevel": 7,
        "cost": 4,
        "isBattlegroundsPoolSpell": True,
        "text": "七本才有。",
    },
    {
        "id": "S2",
        "name": "铸币",
        "type": "BATTLEGROUND_SPELL",
        "techLevel": 1,
        "cost": 1,
        "isBattlegroundsPoolSpell": True,
        "text": "获得一枚铸币。",
    },
    {
        "id": "S1",
        "name": "加攻",
        "type": "BATTLEGROUND_SPELL",
        "techLevel": 3,
        "cost": 2,
        "isBattlegroundsPoolSpell": True,
        "text": "使一个随从获得+2/+2。",
    },
    {
        "id": "S3",
        "name": "双人法术",
        "type": "BATTLEGROUND_SPELL",
        "techLevel": 1,
        "cost": 0,
        "isBattlegroundsPoolSpell": True,
        "isBattlegroundsDuosExclusive": True,
    },
]


def test_card_pool_keeps_lobby_tribes_neutral_and_all():
    block = card_pool_block(CardDB(POOL_RAW), frozenset({"DEMON", "MURLOC"}))
    assert (
        block
        == """【本局卡池（本局种族的随从和中立随从，按酒馆等级）】
[1本] A恶魔 1/3（恶魔）
[1本] B恶魔 2/2（恶魔）嘲讽
[2本] 全能 2/1（全种族）
[2本] 双种族 2/4（野兽、鱼人）
[3本] 中立随从 3/3（中立）战吼：获得一个铸币。
【酒馆法术】
[1本 法术 1金] 铸币：获得一枚铸币。
[3本 法术 2金] 加攻：使一个随从获得+2/+2。"""
    )


def test_card_pool_leaves_out_tier_7():
    block = card_pool_block(CardDB(POOL_RAW), frozenset({"DEMON"}))
    assert "七本" not in block  # 酒馆最高 6 本，7 本的牌不会出现在商店里


def test_card_pool_while_tribes_are_unknown():
    block = card_pool_block(CardDB(POOL_RAW), frozenset())
    assert "恶魔）" not in block and "（中立）" in block and "（全种族）" in block
    empty = card_pool_block(CardDB([]), frozenset({"DEMON"}))
    assert empty == (
        "【本局卡池（本局种族的随从和中立随从，按酒馆等级）】\n（无）\n【酒馆法术】\n（无）"
    )


def test_card_pool_uses_the_inferred_tribes_of_the_state():
    state = make_state(shop=[shop_minion(20, "BGS_061", 3, 1)])
    state = state.model_copy(update={"tribes": frozenset({"BEAST"})})
    pool = full_message(state, cards=CardDB(POOL_RAW)).split("\n\n", 1)[0]
    assert "[1本] 野兽 3/3（野兽）" in pool and "A恶魔" not in pool


MEMORY = AIMemory(
    turn=6,
    deep=False,
    direction="畸变",
    look_for=("飘荡祭品", "神明"),
    analysis="血量健康，\n准备升本。",
)


def test_memory_line_from_an_earlier_judgement():
    state = make_state(turn=7, shop=[shop_minion(20, "BGS_061", 3, 1)])
    assert message_for(state, MEMORY).splitlines()[-1] == (
        "你之前的判断（第 6 回合）：方向 畸变；要找：飘荡祭品、神明；分析：血量健康， 准备升本。"
    )
    deep_last_turn = AIMemory(turn=6, deep=True, direction=None)
    assert message_for(state, deep_last_turn).splitlines()[-1] == (
        "你之前的判断（第 6 回合）：方向 未定；要找：无"
    )
    assert "你之前的判断" not in message_for(state)


def test_memory_line_with_this_turns_deep_analysis():
    state = make_state(turn=6, shop=[shop_minion(20, "BGS_061", 3, 1)])
    deep = AIMemory(
        turn=6, deep=True, direction="畸变", look_for=("神明",), analysis="提前升 5 本。"
    )
    assert message_for(state, deep).splitlines()[-1] == (
        "本回合深度分析：方向 畸变；要找：神明；分析：提前升 5 本。"
    )
    same_turn_fast = AIMemory(turn=6, deep=False, direction="畸变")
    assert (
        message_for(state, same_turn_fast).splitlines()[-1].startswith("你之前的判断（第 6 回合）")
    )


def test_opponent_board_seen_in_combat(mini_lines):
    state = recruit_state(mini_lines, 2)
    state = state.model_copy(update={"next_opponent_id": 3})
    _, opponents, situation = message_parts(state)
    # 下一个对手列完整阵容（带身材），其他对手只写摘要
    assert opponents.splitlines()[2:] == [
        "▶★ 第 2 名 拉卡尼休 | 血量 30+0 | 酒馆 1 本 | 威胁 78 | 第 1 回合见到 2 个随从，元素为主（1 个），"
        "总身材 9，金色 1 张，阵容：粗鄙小鬼(金) 2/2、刷新畸体 1/4",
        "  第 3 名 费林大使 | 血量 30+0 | 酒馆 1 本 | 威胁 23 | 还没见过阵容",
    ]
    assert "下一个对手" not in situation and "其他对手" not in situation


def test_trinkets_and_global_mechanics_lines():
    state = make_state(
        trinkets=["BG36_MagicItem_402"],
        global_dbfs=[132532],
        board=[minion(10, "BG21_029", 1, 1, races=["DEMON"])],
        shop=[shop_minion(20, "BGS_061", 3, 1, races=["PIRATE"])],
    )
    text = message_for(state)
    assert (
        "饰品：精神鞭笞者肖像（获取一张残忍的精神鞭笞者。你的残忍的精神鞭笞者还会使相邻的随从获得属性值。）"
        in text
    )
    assert "本局机制：亚煞极（" in text


def test_unknown_cards_fall_back_to_ids():
    state = make_state(hero="UNKNOWN_HERO", hero_power=None, shop=[shop_minion(20, "NOPE", 2, 2)])
    text = message_for(state)
    assert "英雄：UNKNOWN_HERO | 血量" in text
    assert "  [#20] NOPE 2/2 | 1 本 | 中立" in text


def test_system_prompt_snapshot():
    assert build_system_prompt("要点") == EXPECTED_SYSTEM


def test_system_prompt_derives_limit_and_example(monkeypatch):
    import lscs.llm.prompt

    monkeypatch.setattr(lscs.llm.prompt, "MAX_ACTIONS", 4)
    monkeypatch.setattr(lscs.llm.prompt, "EXAMPLE", {"actions": []})
    system = build_system_prompt("要点")
    assert "3. 最多 4 个动作；" in system
    assert '格式：\n{"actions": []}\n动作类型' in system


def test_system_prompt_has_json_example_and_guide():
    system = build_system_prompt(load_guide(ROOT / "strategy" / "current" / "guide.md"))
    assert "json" in system
    assert '"entity_id": 1689' in system and '"look_for": ["接下来要找的牌名"]' in system
    assert "\n\n【攻略要点】\n# 酒馆战棋新手要点" in system


def test_missing_guide(tmp_path):
    assert load_guide(tmp_path / "guide.md") == "（无）"


def test_guide_saved_by_notepad_does_not_crash(tmp_path):
    path = tmp_path / "guide.md"
    path.write_bytes(b"\xef\xbb\xbf" + "# 要点\n".encode())
    assert load_guide(path) == "# 要点"  # 去掉 BOM
    path.write_bytes("# 要点\n先升本\n".encode("gbk"))
    assert isinstance(load_guide(path), str)  # GBK 编码：乱码但不报错


def test_budget_line_counts_hand_minions_and_max_tier():
    board = [minion(10 + i, "BG21_029", 1, 1) for i in range(5)]  # 米尔豪斯：买牌 2 金
    state = make_state(
        gold=7, upgrade_cost=None, board=board, hand=[hand_minion(30, "BGS_061", 3, 1)]
    )
    lines = message_for(state).splitlines()
    assert lines[2] == (
        "预算：铸币 7，只买随从最多 3 张；随从上限 7，现在 6 个（场面 5 + 手牌随从 1），"
        "还能再买 1 个随从；已是最高等级，不能升本"
    )


def test_budget_line_when_already_over_the_limit():
    board = [minion(10 + i, "BG21_029", 1, 1) for i in range(7)]
    state = make_state(gold=3, board=board, hand=[hand_minion(30, "BGS_061", 3, 1)])
    lines = message_for(state).splitlines()
    assert lines[2] == (
        "预算：铸币 3，只买随从最多 1 张；随从上限 8，现在 8 个（场面 7 + 手牌随从 1），"
        "还能再买 0 个随从"
    )


def test_budget_line_survives_zero_buy_cost():
    state = make_state(gold=3)
    ctx = build_context(state, 0)
    lines = build_user_message(state, ctx, CARDS).split("\n\n", 2)[2].splitlines()
    assert lines[2].startswith("预算：铸币 3，只买随从最多 7 张；")


def test_other_opponents_without_place():
    state = make_state(shop=[shop_minion(20, "BGS_061", 3, 1)])
    state = state.model_copy(
        update={
            "opponents": {
                3: OpponentView(
                    player_id=3, hero_card_id="NOPE", health=20, armor=0, place=None, tavern_tier=2
                ),
                4: OpponentView(
                    player_id=4, hero_card_id="NOPE2", health=15, armor=0, place=2, tavern_tier=3
                ),
            }
        }
    )
    _, opponents, _ = message_parts(state)
    assert opponents.splitlines()[1:] == [
        "主打种族：未知 2 家",
        "★ 第 2 名 NOPE2 | 血量 15+0 | 酒馆 3 本 | 威胁 33 | 还没见过阵容",
        "  第 ? 名 NOPE | 血量 20+0 | 酒馆 2 本 | 威胁 27 | 还没见过阵容",
    ]
    assert "None" not in opponents


DESC = "购买随从和刷新的消耗改为（2）枚铸币。升级酒馆所需的铸币增加（1）枚。"


def power_line(hp, **kwargs):
    state = make_state(hero_power_state=hp, **kwargs)
    lines = [line for line in message_for(state).splitlines() if line.startswith("英雄技能：")]
    assert len(lines) == 1
    return lines[0]


NO = "——不要安排 hero_power"


def test_hero_power_line_for_each_state():
    hp = "TB_BaconShop_HP_054"  # 卡牌数据里有名字和描述

    def line(**kwargs):
        return power_line(power(hp, **kwargs))

    assert line() == f"英雄技能：法力风暴（0 金，本回合可用，不需要目标）：{DESC}"
    used = f"英雄技能：法力风暴（本回合已使用，不能再用{NO}）：{DESC}"
    assert line(used=True) == used
    assert line(option="unusable", reason="REQ_NOT_EXHAUSTED_HERO_POWER") == used
    assert line(option="absent") == f"英雄技能：法力风暴（被动，不能主动使用{NO}）：{DESC}"
    # 本回合第一组选项里有它、后来不见了：不能说成被动
    assert line(option="absent", passive=False) == f"英雄技能：法力风暴（现在不能用{NO}）：{DESC}"
    unknown = f"英雄技能：法力风暴（还不确定本回合能不能用{NO}）：{DESC}"
    assert line(option="unknown") == unknown
    assert line(option="unusable", reason="REQ_TARGET_TO_PLAY") == (
        f"英雄技能：法力风暴（现在不能用：没有可选的目标{NO}）：{DESC}"
    )
    assert line(option="unusable", reason="127") == f"英雄技能：法力风暴（现在不能用{NO}）：{DESC}"


def test_hero_power_line_when_only_gold_is_missing():
    def line(needs):
        hp = power(
            "TB_BaconShop_HP_054",
            cost=2,
            option="unusable",
            reason="REQ_ENOUGH_MANA",
            needs_target=needs,
        )
        return power_line(hp)

    head = "英雄技能：法力风暴（2 金，现在铸币不够，凑够铸币后可用，"
    assert line(False) == f"{head}不需要目标）：{DESC}"
    # 铸币不够时选项不列目标：要目标就只能从场上的随从里选
    assert line(True) == f"{head}需要目标；凑够铸币后在场上的随从里选）：{DESC}"
    # 不知道要不要目标：让 AI 填一个场上的随从，校验器才会接受
    assert line(None) == f"{head}是否需要目标未知；要用的话 entity_id 填场上的一个随从）：{DESC}"


def test_hero_power_line_lists_targets():
    line = power_line(
        power("NOPE", cost=2, targets=(10, 20, 99)),
        board=[minion(10, "BG21_029", 1, 1)],
        shop=[shop_minion(20, "BGS_061", 3, 1)],
    )
    assert line == "英雄技能：NOPE（2 金，本回合可用，需要目标：#10 粗鄙小鬼、#20 海盗无赖、#99）"


def test_no_hero_power_line_without_a_hero_power():
    text = message_for(make_state(hero_power_state=None))
    assert "英雄技能" not in text


def test_budget_line_mentions_a_paid_hero_power():
    ready = make_state(gold=7, hero_power_state=power(cost=2))
    assert message_for(ready).splitlines()[2].endswith("；英雄技能 2 金")
    short = power(cost=2, option="unusable", reason="REQ_ENOUGH_MANA")
    assert (
        message_for(make_state(gold=1, hero_power_state=short))
        .splitlines()[2]
        .endswith("；英雄技能 2 金")
    )
    assert (
        "英雄技能" not in message_for(make_state(gold=7, hero_power_state=power())).splitlines()[2]
    )


def test_budget_line_says_when_the_hero_power_cannot_be_used():
    def budget(hp):
        return message_for(make_state(gold=7, hero_power_state=hp)).splitlines()[2]

    assert budget(power(cost=2, used=True)).endswith("；英雄技能本回合已用过，不能再用")
    assert budget(power(option="absent")).endswith("；英雄技能是被动的")
    for hp in (power(option="absent", passive=False), power(option="unknown")):
        assert budget(hp).endswith("；英雄技能现在不能用")


def test_system_prompt_rules():
    system = build_system_prompt("要点")
    assert '"gold_after": 4, "minions_after": 5' in system
    assert "refresh 要放在最后（之后看不到新商店，所以 refresh 后面不要再冻结）" in system
    assert "2. 每个动作都要填 gold_after" in system
    assert "可以放心建议刷新" in system
    assert "不要建议发现选择、黑暗之赐或调整站位" in system
    assert "买来的随从在手牌里，要先打出去，给场上随从加属性的技能才会作用到它" in system
    assert "hero_power（英雄技能）" in system
    assert "需要目标时 entity_id 填目标编号" in system


# ---- 选英雄 ----

EXPECTED_HERO_SYSTEM = """你是《炉石传说》酒馆战棋的高分教练，正在帮一位 6000 分以下的新手在开局的 4 个备选英雄里选一个。只有大约 1 分钟，请用简体中文，直接给结论。

【怎么判断】
- 护甲：官方用起始护甲做平衡——越强的英雄给的护甲越少，护甲最少的英雄通常就是当前版本最强的。这是最可靠的强度信号，默认按它选；只有技能明显不适合新手、或明显依赖本局没有的条件时才偏离。
- 技能：看技能描述。新手优先选"每回合都有稳定收益、不需要复杂操作"的技能（加属性、给铸币、减费、免费刷新），少选要靠特定种族、特定卡或精细计算才强的技能。
- 本局种族在选英雄时还看不到，所以依赖某个种族的技能要打折扣；只有本局机制（如神明）是已知的，可以结合它判断。
- 不知道的事不要编：没有给出的数据（如胜率榜）不要假装知道。

只输出一个 json 对象，格式：
{"pick": 89, "reason": "为什么选它，30 字以内，会显示在游戏画面上", "heroes": [{"entity_id": 90, "comment": "一句点评，15 字以内"}, {"entity_id": 91, "comment": "..."}], "confidence": 0.7}
pick 必须是备选英雄的实体编号；heroes 给其他每个英雄一句点评（推荐的那个也可以给）。

【攻略要点】
要点

【本月英雄强度资料】
（本月还没有可靠的英雄强度资料，请按护甲和技能判断。）"""


def test_hero_system_prompt_snapshot():
    assert build_hero_system_prompt("要点", None) == EXPECTED_HERO_SYSTEM
    assert build_hero_system_prompt("要点", "") == EXPECTED_HERO_SYSTEM  # 空文件当作没有
    with_note = build_hero_system_prompt("要点", "2026-10-20 英雄榜：A 档 凯瑞尔")
    assert with_note == EXPECTED_HERO_SYSTEM.rsplit("\n", 1)[0] + "\n2026-10-20 英雄榜：A 档 凯瑞尔"


def test_hero_system_prompt_leaves_the_recruit_prompt_alone():
    assert build_system_prompt("要点") == EXPECTED_SYSTEM
    assert not build_hero_system_prompt("要点", None).startswith(EXPECTED_SYSTEM[:40])


HERO_CARDS = CardDB(
    [
        {"id": "BG21_HERO_000", "dbfId": 59000, "name": "凯瑞尔·罗姆", "type": "HERO"},
        {
            "id": "BG21_HERO_000p",
            "dbfId": 73941,
            "name": "定罪",
            "type": "HERO_POWER",
            "cost": 0,
            "text": "随机使1个友方随从获得+1/+1。",
        },
        {"id": "TB_BaconShop_HERO_91", "dbfId": 61000, "name": "了不起的杰弗里斯", "type": "HERO"},
        {
            "id": "TB_BaconShop_HP_102",
            "dbfId": 64486,
            "name": "三个愿望",
            "type": "HERO_POWER",
            "cost": 3,
            "text": "如果你有两个相同的随从，找到第三个。",
        },
        {"id": "BG28_HERO_800", "dbfId": 105000, "name": "泰瑟兰·血望者", "type": "HERO"},
        {"id": "BG33_HERO_001", "dbfId": 129000, "name": "洛，在世传奇", "type": "HERO"},
        {
            "id": "BG33_HERO_001p",
            "dbfId": 129164,
            "name": "英勇鼓舞",
            "type": "HERO_POWER",
            "text": "被动：在15个友方随从攻击后，获取一份三连奖励。",
        },
        {"id": "BGFYM_011", "dbfId": 132532, "name": "亚煞极", "type": "MINION", "text": "神明。"},
    ]
)


def hero_state(rerolled_heroes=None, global_dbfs=(132532,)):
    from lscs.state.models import HeroOption, HeroSelect, Phase

    heroes = rerolled_heroes or [
        (92, "TB_BaconShop_HERO_91", 17, 64486),
        (89, "BG21_HERO_000", 10, 73941),
        (90, "BG28_HERO_800", 0, 105432),  # 卡牌数据里没有这个技能；没有护甲标签
        (91, "BG33_HERO_001", 19, 129164),  # 技能卡没有费用
    ]
    select = HeroSelect(
        offered=tuple(
            HeroOption(entity_id=e, card_id=c, position=i, armor=a, health=30, hero_power_dbf=dbf)
            for i, (e, c, a, dbf) in enumerate(heroes, start=1)
        )
    )
    state = make_state(
        turn=0, phase=Phase.HERO_SELECT, hero="TB_BaconShop_HERO_PH", global_dbfs=global_dbfs
    )
    return state.model_copy(update={"hero_select": select})


def test_hero_message():
    assert build_hero_message(hero_state(), HERO_CARDS) == (
        "选英雄（4 选 1）\n"
        "本局机制：亚煞极（神明。）\n"
        "本局种族：未知（选英雄时还看不到）\n"
        "备选英雄：\n"
        "  [#92] 了不起的杰弗里斯 | 护甲 17 | 技能 三个愿望（3 金）：如果你有两个相同的随从，找到第三个。\n"
        "  [#89] 凯瑞尔·罗姆 | 护甲 10 | 技能 定罪（0 金）：随机使1个友方随从获得+1/+1。\n"
        "  [#90] 泰瑟兰·血望者 | 护甲 未知 | 技能 未知\n"
        "  [#91] 洛，在世传奇 | 护甲 19 | 技能 英勇鼓舞（? 金）：被动：在15个友方随从攻击后，获取一份三连奖励。"
    )


def test_hero_message_after_reroll_without_mechanics():
    heroes = [(15, "UNKNOWN_HERO", 12, None), (89, "BG21_HERO_000", 10, 73941)]
    text = build_hero_message(hero_state(heroes, global_dbfs=()), HERO_CARDS, rerolled=True)
    assert text.splitlines() == [
        "选英雄（2 选 1，已重新随机）",
        "本局种族：未知（选英雄时还看不到）",
        "备选英雄：",
        "  [#15] UNKNOWN_HERO | 护甲 12 | 技能 未知",
        "  [#89] 凯瑞尔·罗姆 | 护甲 10 | 技能 定罪（0 金）：随机使1个友方随从获得+1/+1。",
    ]


# ---- 选项 ----

EXPECTED_CHOICE_SYSTEM = """你是《炉石传说》酒馆战棋的高分教练，正在帮一位 6000 分以下的新手在游戏弹出的几个选项里选一个（三连奖励的发现、黑暗之赐、饰品、英雄技能的升级选项，或者随从效果的发现）。玩家几秒钟内就要点，请用简体中文，直接给结论。

【怎么判断】
- 先看本局方向和场面：和当前方向（种族/流派）、场上核心配合的选项优先；还没有方向时选单独最强、最稳、新手最容易用好的。
- 三连奖励 / 发现随从：优先方向核心卡、能成长的牌、有经济价值的牌，其次才看身材；能和手里、场上凑成对子或三连的也值得。
- 黑暗之赐：每个选项的属性已经算上了赐福。比较"随从本身 + 赐福"对现在阵容的价值；圣盾、复生、风怒这类关键词放在主力随从身上最值。
- 饰品：看它在接下来几个回合能带来多少收益；和方向配合、现在就能用上的优先；要花铸币的饰品要考虑这回合剩多少铸币、会不会耽误升本。
- 英雄技能的升级选项：选和自己的阵容、打法最契合的；几个选项差不多时选收益最稳定的。
- 不知道的事不要编：没有给出的数据不要假装知道。

只输出一个 json 对象，格式：
{"pick": 6933, "reason": "为什么选它，25 字以内", "confidence": 0.7}
pick 必须是选项的实体编号；reason 会显示在游戏画面上，25 字以内。

【攻略要点】
要点"""


def test_choice_system_prompt_snapshot():
    from lscs.llm.prompt import build_choice_system_prompt

    assert build_choice_system_prompt("要点") == EXPECTED_CHOICE_SYSTEM


def test_choice_system_prompt_leaves_the_other_prompts_alone():
    from lscs.llm.prompt import build_choice_system_prompt

    assert build_system_prompt("要点") == EXPECTED_SYSTEM
    assert build_hero_system_prompt("要点", None) == EXPECTED_HERO_SYSTEM
    assert build_choice_system_prompt("要点") not in (EXPECTED_SYSTEM, EXPECTED_HERO_SYSTEM)


CHOICE_CARDS = CardDB(
    [
        {"id": "BG21_HERO_000", "name": "凯瑞尔·罗姆", "type": "HERO"},
        {
            "id": "BG21_HERO_000p",
            "name": "定罪",
            "type": "HERO_POWER",
            "text": "随机使1个友方随从获得+1/+1。被动：在每场战斗后，选择一项提升。",
        },
        {
            "id": "BG21_HERO_000p_t1",
            "name": "结阵姿态",
            "type": "SPELL",
            "text": "在本局对战中，“定罪”额外使一个友方随从获得属性值。",
        },
        {"id": "BG21_HERO_000_Buddyt", "name": "进攻姿态", "type": "SPELL"},
        {"id": "BG30_Trinket_1st", "name": "小型饰品", "type": "BATTLEGROUND_TRINKET"},
        {
            "id": "BG35_MagicItem_890",
            "name": "沉没的船锚",
            "type": "BATTLEGROUND_TRINKET",
            "text": "随机获取2张悬赏令。",
        },
        {"id": "BG30_MagicItem_420", "name": "麦迪文之书", "type": "BATTLEGROUND_TRINKET"},
        {"id": "BG36_MidGameEffect_010", "name": "酒馆战棋黑暗之赐[DNT]", "type": "SPELL"},
        {
            "id": "BG36_312",
            "name": "摧心招募者",
            "type": "MINION",
            "text": "发动（0）：弃一张牌以随机获取一张畸变怪。",
        },
        {"id": "BG36_356", "name": "泰瑞尔", "type": "MINION", "text": "发动（1）：…"},
        {
            "id": "BG36_MidGameEffect_000t13",
            "name": "鹰身人之爪",
            "type": "SPELL",
            "text": "圣盾。风怒",
        },
        {"id": "EDR_100t13e", "name": "鹰身人之爪", "type": "ENCHANTMENT", "text": "圣盾，风怒"},
        {"id": "TB_BaconShop_Triples_01", "name": "三连奖励", "type": "SPELL"},
        {"id": "BG23_016", "name": "海礁探险者", "type": "MINION"},
        {"id": "BGS_061", "name": "海盗无赖", "type": "MINION"},
        {"id": "BGFYM_011", "dbfId": 132532, "name": "亚煞极", "type": "MINION", "text": "神明。"},
    ]
)


def choice_message(choice, memory=None, **state_kwargs):
    from factory import choice_state

    from lscs.llm.prompt import build_choice_message

    kwargs = dict(
        gold=3,
        tier=6,
        health=19,
        hero="BG21_HERO_000",
        hero_power="BG21_HERO_000p",
        tribes=frozenset({"ABERRATION", "MURLOC"}),
    )
    kwargs.update(state_kwargs)
    return build_choice_message(choice_state(choice, **kwargs), CHOICE_CARDS, memory)


def test_choice_message_for_a_dark_gift():
    from factory import choice_option, make_choice

    choice = make_choice(
        [
            choice_option(
                6931,
                "BG36_312",
                0,
                6,
                2,
                tier=4,
                races=("ABERRATION",),
                gifts=("BG36_MidGameEffect_000t13",),
            ),
            choice_option(6932, "BG36_356", 1, 32, 32, tier=6, gifts=("EDR_100t13e",)),
            choice_option(6933, "BGS_061", 2, 3, 1, tier=1, races=("PIRATE",), keywords=("TAUNT",)),
        ]
    )
    text = choice_message(
        choice,
        board=[minion(5805, "BGS_061", 11, 11, races=("PIRATE",))],
        global_dbfs=(132532,),
        trinkets=("BG35_MagicItem_890",),
    )
    assert text.splitlines() == [
        "选择：黑暗之赐（3 选 1）",
        "第 9 回合（招募）| 英雄：凯瑞尔·罗姆 | 血量 19+0 | 铸币 3 | 酒馆 6 本",
        "本局种族：畸变、鱼人",
        "本局机制：亚煞极（神明。）",
        "饰品：沉没的船锚（随机获取2张悬赏令。）",
        "场面（已用 1/7）：",
        "  [#5805] 海盗无赖 11/11 | 1 本 | 海盗",
        "手牌：",
        "  （空）",
        "选项：",
        # 赐福优先用法术的说明；卡牌数据里只有附魔时用附魔的
        "  [#6931] 左 1 摧心招募者 6/2 | 4 本 | 畸变 | 发动（0）：弃一张牌以随机获取一张畸变怪。"
        " | 黑暗之赐：鹰身人之爪（圣盾。风怒）",
        "  [#6932] 左 2 泰瑞尔 32/32 | 6 本 | 中立 | 发动（1）：… | 黑暗之赐：鹰身人之爪（圣盾，风怒）",
        "  [#6933] 左 3 海盗无赖 3/1 | 1 本 | 海盗 | 嘲讽",
    ]


def test_choice_message_for_trinkets_mentions_the_gold():
    from factory import choice_option, make_choice

    choice = make_choice(
        [
            choice_option(3027, "BG35_MagicItem_890", 0, card_type="BATTLEGROUND_TRINKET", cost=4),
            choice_option(3028, "BG30_MagicItem_420", 1, card_type="BATTLEGROUND_TRINKET", cost=1),
            choice_option(3029, "UNKNOWN_TRINKET", 2, card_type="BATTLEGROUND_TRINKET"),
        ],
        choice_id=6,
        kind="trinket",
        source="BG30_Trinket_1st",
    )
    lines = choice_message(choice, gold=10, tribes=frozenset()).splitlines()
    assert lines[0] == "选择：小型饰品（3 选 1）"
    assert lines[2] == "本局种族：未知（推断中）"
    assert lines[-4:] == [
        "  [#3027] 左 1 沉没的船锚（4 金）：随机获取2张悬赏令。",
        "  [#3028] 左 2 麦迪文之书（1 金）",
        "  [#3029] 左 3 UNKNOWN_TRINKET",
        "铸币 10；饰品的铸币价会从这回合的铸币里扣",
    ]


def test_choice_message_for_hero_power_stances_explains_the_hero_power():
    from factory import choice_option, make_choice

    choice = make_choice(
        [
            choice_option(677, "BG21_HERO_000p_t1", 0, card_type="SPELL"),
            choice_option(678, "BG21_HERO_000_Buddyt", 1, card_type="SPELL"),
        ],
        choice_id=2,
        kind="hero_power",
        source="BG21_HERO_000p",
    )
    lines = choice_message(choice).splitlines()
    assert lines[0] == "选择：定罪的升级选项（2 选 1）"
    assert "英雄技能：定罪：随机使1个友方随从获得+1/+1。被动：在每场战斗后，选择一项提升。" in lines
    assert lines[-2:] == [
        "  [#677] 左 1 结阵姿态：在本局对战中，“定罪”额外使一个友方随从获得属性值。",
        "  [#678] 左 2 进攻姿态",
    ]


def test_choice_message_titles_and_memory():
    from factory import choice_option, make_choice

    options = [
        choice_option(9723, "BGS_061", 0, 4, 5, tier=2),
        choice_option(9725, "BGS_061", 1, 6, 8, tier=2),
    ]
    triple = make_choice(options, kind="triple", source="TB_BaconShop_Triples_01")
    assert choice_message(triple).splitlines()[0] == "选择：三连奖励（2 选 1）"
    discover = make_choice(options, kind="discover", source="BG23_016")
    memory = AIMemory(8, False, "畸变", ("泰瑞尔",), "")
    lines = choice_message(discover, memory).splitlines()
    assert lines[0] == "选择：发现（2 选 1，来源：海礁探险者）"
    assert lines[-1] == "你之前的判断（第 8 回合）：方向 畸变；要找：泰瑞尔"
    assert "英雄技能：" not in "\n".join(lines)  # 只有技能升级选项才写英雄技能


def test_repair_text_for_plans_keeps_the_thoughts_and_rechecks_each_step():
    for lane in ("fast", "deep"):
        text = repair_text(lane, "第 2 步：铸币不够")
        assert text == (
            "你刚才的方案不合法：第 2 步：铸币不够。\n"
            "请按同样的规则改正后，重新输出完整的 json：analysis、summary、direction、look_for "
            "保持不变，只改 actions；每一步都重新核对 gold_after 和 minions_after。"
        )


def test_repair_text_for_picks():
    for lane in ("hero", "choice"):
        assert repair_text(lane, "推荐的选项不在选项里") == (
            "你刚才的回答不合法：推荐的选项不在选项里。请按同样的规则改正后，重新输出完整的 json。"
        )


def test_opponents_block_sits_between_the_pool_and_the_turn(mini_lines):
    text = full_message(recruit_state(mini_lines, 2))
    pool, opponents, situation = text.split("\n\n", 2)
    assert pool == POOL_TURN_2 and opponents == OPPONENTS_TURN_2
    assert situation.startswith("第 2 回合（招募）")


def test_same_turn_messages_share_the_pool_and_opponents_prefix(mini_lines):
    # 同一回合里刷新、买卖之后：卡池块和对手块逐字相同，前缀缓存能多命中一段
    state = recruit_state(mini_lines, 2)
    later = state.model_copy(
        update={
            "seq": state.seq + 5,
            "shop": (shop_minion(60, "BGS_061", 3, 1, races=["PIRATE"]),),
            "me": state.me.model_copy(update={"gold": 1}),
        }
    )
    first, second = full_message(state), full_message(later)
    assert first != second
    assert first.split("\n\n")[:2] == second.split("\n\n")[:2]


OPP_CARDS = CardDB(
    [
        {"id": "H2", "name": "洛卡拉", "type": "HERO"},
        {"id": "H5", "name": "詹迪斯", "type": "HERO"},
        {"id": "H6", "name": "范克里夫", "type": "HERO"},
        {"id": "H7", "name": "布莱恩", "type": "HERO"},
        {"id": "P", "name": "夺金健将", "type": "MINION"},
        {"id": "E", "name": "电音合成师", "type": "MINION"},
        {"id": "W", "name": "飘荡祭品", "type": "MINION"},
    ]
)


def opponents_state(next_id):
    def opp(player_id, health, place, tier, board=(), seen=None, armor=0):
        return OpponentView(
            player_id=player_id,
            hero_card_id=f"H{player_id}",
            health=health,
            armor=armor,
            place=place,
            tavern_tier=tier,
            last_board=tuple(board),
            last_seen_turn=seen,
        )

    ghost = [
        minion(1, "P", 7, 7, races=["PIRATE"], golden=True),
        minion(2, "E", 3, 5, tier=2, races=["DRAGON"]),
    ]
    aberrations = [minion(3, "W", 10, 9, tier=3, races=["ABERRATION"]) for _ in range(2)]
    state = make_state(turn=10, tribes={"ABERRATION", "PIRATE", "DRAGON"})
    return state.model_copy(
        update={
            "opponents": {
                2: opp(2, 30, 1, 5, aberrations, seen=9, armor=9),
                5: opp(5, 0, 8, 2, ghost, seen=4),
                6: opp(6, -9, 7, 5, [minion(4, "E", 3, 4)], seen=5),
                7: opp(7, 28, 2, None),
            },
            "next_opponent_id": next_id,
        }
    )


def test_opponents_block_with_a_ghost_and_eliminated_players():
    _, opponents, _ = message_parts(opponents_state(next_id=5), cards=OPP_CARDS)
    assert opponents.splitlines()[1:] == [
        "主打种族：畸变 1 家、未知 1 家",
        "★ 第 1 名 洛卡拉 | 血量 30+9 | 酒馆 5 本 | 威胁 96 | 第 9 回合见到 2 个随从，畸变为主（2 个），"
        "总身材 38，主力：飘荡祭品、飘荡祭品",
        "  第 2 名 布莱恩 | 血量 28+0 | 酒馆 ? 本 | 威胁 14 | 还没见过阵容",
        # 下一个对手已经淘汰：这回合打它的残影，列出那套阵容
        "▶ 第 8 名 詹迪斯 | 已淘汰，这回合打它的残影 | 第 4 回合见到的阵容：夺金健将(金) 7/7、电音合成师 3/5",
        "  已淘汰：范克里夫",  # 其他淘汰的合成一行
    ]
    _, opponents, _ = message_parts(opponents_state(next_id=2), cards=OPP_CARDS)
    assert opponents.splitlines()[-1] == "  已淘汰：范克里夫、詹迪斯"


def test_only_the_next_opponent_when_all_opponents_is_off():
    state = opponents_state(next_id=2)
    _, opponents, _ = message_parts(state, cards=OPP_CARDS, all_opponents=False)
    assert opponents.splitlines() == [
        OPPONENTS_TITLE,
        "▶★ 第 1 名 洛卡拉 | 血量 30+9 | 酒馆 5 本 | 威胁 96 | 第 9 回合见到 2 个随从，畸变为主（2 个），"
        "总身材 38，阵容：飘荡祭品 10/9、飘荡祭品 10/9",
    ]
    nobody = state.model_copy(update={"opponents": {}, "next_opponent_id": None})
    _, opponents, _ = message_parts(nobody, cards=OPP_CARDS)
    assert opponents.splitlines() == [OPPONENTS_TITLE, "（还没有对手的信息）"]


def pool_line(state, memory=None):
    from lscs.advisor.pool import PoolEstimator

    view = PoolEstimator(CARDS, {1: 16, 2: 15, 3: 13}).view(state)
    lines = message_for(state, memory, pool=view).splitlines()
    found = [line for line in lines if line.startswith("卡池估计")]
    assert len(found) <= 1
    if found:  # 紧跟在手牌段后面
        after_hand = lines.index("手牌：") + 1
        while lines[after_hand].startswith("  "):
            after_hand += 1
        assert lines[after_hand] == found[0]
    return found[0] if found else None


def test_pool_estimate_line_lists_pairs_near_pairs_and_memory():
    brain = minion(10, "BG36_099", 3, 4, tier=2, races=["ABERRATION"])
    spawn = minion(12, "BG36_116", 2, 2, tier=2, races=["ABERRATION"])
    state = make_state(
        board=[brain, brain.model_copy(update={"entity_id": 11}), spawn],
        shop=[shop_minion(20, "BG36_116", 2, 2, tier=2, races=["ABERRATION"])],
        tribes={"ABERRATION", "DEMON"},
    )
    memory = AIMemory(turn=1, deep=False, look_for=("飘荡祭品", "没有这张"))
    assert pool_line(state, memory) == (
        "卡池估计（还能刷到的张数）：蚀脑怪 还剩约 13 张（你有 2 张）；"
        "孢林卵怪 还剩约 14 张（你有 1 张，商店里有 1 张）；飘荡祭品 还剩约 13 张"
    )


def test_no_pool_estimate_line_without_cards_or_notes():
    state = make_state(board=[minion(10, "BG36_099", 3, 4, tier=2)])
    assert pool_line(state) is None  # 没有对子、没有要找的牌
    assert not any(line.startswith("卡池估计") for line in message_for(state).splitlines())


def test_an_empty_captured_board_in_the_opponents_block():
    empty = OpponentView(
        player_id=7,
        hero_card_id="H7",
        health=20,
        armor=0,
        place=2,
        tavern_tier=3,
        last_board=(),
        last_seen_turn=5,
    )
    ghost = empty.model_copy(update={"player_id": 8, "health": 0, "place": 8})
    state = make_state(turn=6).model_copy(
        update={"opponents": {7: empty, 8: ghost}, "next_opponent_id": 8}
    )
    _, opponents, _ = message_parts(state, cards=OPP_CARDS)
    assert opponents.splitlines()[2:] == [
        "★ 第 2 名 布莱恩 | 血量 20+0 | 酒馆 3 本 | 威胁 35 | 第 5 回合见到空场",
        "▶ 第 8 名 布莱恩 | 已淘汰，这回合打它的残影 | 第 5 回合见到空场",
    ]
