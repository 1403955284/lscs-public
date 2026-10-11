from pathlib import Path

from factory import hand_minion, make_state, minion, shop_minion
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

EXPECTED_TURN_2 = """第 2 回合（招募）| 英雄：米尔豪斯·法力风暴（技能 法力风暴：购买随从和刷新的消耗改为（2）枚铸币。升级酒馆所需的铸币增加（1）枚。）| 血量 27+0 | 铸币 4 | 酒馆 1 本
升本 4 金 | 刷新 1 金 | 买牌 2 金 | 卖牌 +1 金
预算：铸币 4，只买随从最多 2 张；随从上限 7，现在 1 个（场面 1 + 手牌随从 0），还能再买 6 个随从
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
    assert "英雄：UNKNOWN_HERO（技能 未知：）" in text
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
