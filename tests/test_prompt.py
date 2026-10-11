from pathlib import Path

from factory import hand_minion, make_state, minion, power, shop_minion
from helpers import FIXTURES, TEST_POOL

from lscs.advisor.context import build_context
from lscs.advisor.rules import rule_plans
from lscs.advisor.rules_config import load_rules_config
from lscs.cards.db import CardDB
from lscs.llm.prompt import build_system_prompt, build_user_message, load_guide
from lscs.pipeline import Pipeline
from lscs.state.models import EventType, OpponentView
from lscs.state.tracker import StateTracker

ROOT = Path(__file__).resolve().parents[1]
CARDS = CardDB.from_json_file(FIXTURES / "cards_mini.json")
RULES = load_rules_config(ROOT / "strategy" / "current" / "rules.toml")

# 这份合成日志没有选项列表，所以英雄技能是"还不确定"（真实日志见 test_hero_power_line_*）
EXPECTED_TURN_2 = """第 2 回合（招募）| 英雄：米尔豪斯·法力风暴 | 血量 27+0 | 铸币 4 | 酒馆 1 本
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
  （空）
下一个对手：费林大使 血量 30+0；还没见过阵容
其他对手：拉卡尼休 血量 30 第 2 名
规则建议（仅供参考）：
  A. 升本（第 2 回合按节奏该升 2 本了）
  B. 买 #50 熔岩潜伏者（商店里身材最好）；刷新（剩 2 金，刷新找更好的牌）"""


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


def message_for(state):
    ctx = build_context(state, RULES.buy_cost_for(state.me.hero_card_id))
    return build_user_message(state, ctx, CARDS, rule_plans(ctx, RULES, CARDS.name))


def test_user_message_snapshot(mini_lines):
    assert message_for(recruit_state(mini_lines, 2)) == EXPECTED_TURN_2


def test_opponent_board_seen_in_combat(mini_lines):
    state = recruit_state(mini_lines, 2)
    state = state.model_copy(update={"next_opponent_id": 3})
    assert (
        "下一个对手：拉卡尼休 血量 30+0；第 1 回合见到的阵容：粗鄙小鬼(金) 2/2、刷新畸体 1/4"
        in (message_for(state))
    )


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


def test_system_prompt_has_json_example_and_guide():
    system = build_system_prompt(load_guide(ROOT / "strategy" / "current" / "guide.md"))
    assert "json" in system
    assert '"entity_id": 1689' in system
    assert "酒馆战棋新手要点" in system


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
    lines = build_user_message(state, ctx, CARDS, []).splitlines()
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
    text = message_for(state)
    assert "其他对手：NOPE2 血量 15 第 2 名；NOPE 血量 20" in text
    assert "None" not in text


def test_system_prompt_asks_for_running_totals():
    system = build_system_prompt("要点")
    assert '"gold_after": 4, "minions_after": 5' in system
    assert "refresh 要放在最后，不要在 refresh 后面再冻结" in system
    assert "7. 每个动作都要填 gold_after" in system
    assert "8. 每个动作都要填 minions_after" in system


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


def test_rule_plans_mention_the_hero_power():
    state = make_state(gold=0, hero_power_state=power())
    assert "  A. 使用英雄技能（免费技能，每回合都用掉）" in message_for(state)


def test_system_prompt_allows_hero_power():
    system = build_system_prompt("要点")
    assert "不要建议使用英雄技能" not in system
    assert "不要建议发现选择或调整站位" in system
    assert "最多 5 个动作" in system
    assert "买来的随从在手牌里，要先打出去，给场上随从加属性的英雄技能才会作用到它" in system
    assert "hero_power（使用英雄技能）" in system
    assert "需要目标时 entity_id 填目标编号" in system
    assert "英雄技能按它标注的费用" in system
