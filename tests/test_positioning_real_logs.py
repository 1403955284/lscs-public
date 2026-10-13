"""两份真实对局里的站位建议（站位建议设计文档第 2.3、2.4 节），卡牌库用 cards_mini.json。"""

import pytest
from helpers import FIXTURES, NEEDS_USER_LOG, REAL_LOG, USER_LOG, recruit_at, replay_log

from lscs.advisor.positioning import plan_positions, step_texts, validate_position
from lscs.cards.db import CardDB
from lscs.cli import DEFAULT_POOL
from lscs.state.models import Phase
from lscs.state.pool import load_pool_config

CARDS = CardDB.from_json_file(FIXTURES / "cards_mini.json")
POOL = load_pool_config(DEFAULT_POOL)


def updates_of(path):
    return replay_log(path, POOL)


def recruits(updates, turn=None):
    """招募阶段（招募开始之后）的快照，按顺序。"""
    return [
        s
        for s, _ in updates
        if s.phase == Phase.RECRUIT and s.main_action and (turn is None or s.turn == turn)
    ]


def advise(state):
    advice = plan_positions(state, CARDS, CARDS.name)
    if advice is not None:
        board = tuple(m.entity_id for m in state.me.board)
        assert validate_position(advice, board) is None
    return advice


def moves(state):
    """[(卡牌 ID, 目标槽位, 拖到谁的左边的卡牌 ID)]；场上少于 2 个随从时为 None。"""
    advice = advise(state)
    if advice is None:
        return None
    card = {m.entity_id: m.card_id for m in state.me.board}
    return [(card[m.entity_id], m.index, card.get(m.before_id)) for m in advice.moves]


def number(state):
    """第一步的"左数第几个"。"""
    advice = advise(state)
    card = {m.entity_id: m.card_id for m in state.me.board}
    return step_texts(advice, card, CARDS.name)[0].number


SPAWN = [("BG36_116", 0, None)]  # 把孢林卵怪拖到最左边


@pytest.fixture(scope="module")
def user():
    return updates_of(USER_LOG)


@NEEDS_USER_LOG
def test_user_turn_3_spawn_bought_then_dragged(user):
    states = recruits(user, 3)
    i = next(i for i, s in enumerate(states) if len(s.me.board) == 3)
    bought = states[i]
    assert bought.me.board[2].card_id == "BG36_116"
    assert moves(bought) == SPAWN and number(bought) == 3
    dragged = next(s for s in states[i:] if s.me.board != bought.me.board)  # 用户接着自己拖了
    assert dragged.me.board[0].card_id == "BG36_116"
    assert moves(dragged) == []


@NEEDS_USER_LOG
@pytest.mark.parametrize("turn", [9, 10, 11])
def test_user_tyrael_on_the_left_before_combat(user, turn):
    last = recruits(user, turn)[-1]
    assert moves(last) == SPAWN and number(last) == 2


@NEEDS_USER_LOG
def test_user_turn_12_until_the_spawn_is_sold_and_turn_13(user):
    start = recruit_at(user, 12)
    assert start.me.gold == 10 and len(start.me.board) == 7
    assert moves(start) == SPAWN
    assert moves(recruits(user, 12)[-1]) == []  # 卖掉了孢林卵怪；恩拉其工兵是经济型亡语
    assert all(moves(s) == [] for s in recruits(user, 13))


@NEEDS_USER_LOG
@pytest.mark.parametrize("turn", [4, 5, 6, 7, 8])
def test_user_turns_4_to_8_need_nothing(user, turn):
    assert moves(recruits(user, turn)[-1]) == []


@NEEDS_USER_LOG
def test_user_whole_game_has_one_step_in_five_turns(user):
    turns, most = set(), 0
    for state in recruits(user):
        found = moves(state)
        if found:
            turns.add(state.turn)
            most = max(most, len(found))
    assert turns == {3, 9, 10, 11, 12} and most == 1


@pytest.fixture(scope="module")
def cc0():
    return updates_of(REAL_LOG)


def test_cc0_turn_5_ghoul_then_imp(cc0):
    states = recruits(cc0, 5)
    bought = next(s for s in states if s.me.board[-1].card_id == "FP1_024")
    # 粗鄙小鬼已经在最左，蹒跚的食尸鬼（嘲讽，亡语：对所有随从造成 1 点伤害）排第二
    assert moves(bought) == [("FP1_024", 1, "BGS_001")] and number(bought) == 7
    assert moves(states[-1]) == [("BG21_029", 0, None)]  # 卖了食尸鬼，甲板杂兵被拖到了最左


@pytest.mark.parametrize("turn", [6, 7])
def test_cc0_turns_6_and_7_keep_suggesting_the_imp(cc0, turn):
    assert moves(recruit_at(cc0, turn)) == [("BG21_029", 0, None)]


@pytest.mark.parametrize("turn", range(8, 16))
def test_cc0_turns_8_to_15_need_nothing(cc0, turn):
    assert moves(recruits(cc0, turn)[-1]) == []


def test_cc0_turn_16_selfless_hero(cc0):
    last = recruits(cc0, 16)[-1]
    assert moves(last) == [("OG_221", 0, None)] and number(last) == 3


@pytest.mark.parametrize("turn", [14, 15, 16])
def test_cc0_wildfire_is_not_cleave(cc0, turn):
    state = recruits(cc0, turn)[-1]
    advice = advise(state)
    assert advice.cleave is None
    if turn != 15:  # 第 15 回合打的是残影
        opp = state.opponents[state.next_opponent_id]
        assert "BGS_126" in [m.card_id for m in opp.last_board]


# ---- 经过 AdviceRunner 和终端（买完才提示、机制说明、"已调好"） ----

DEATHRATTLE = "（亡语随从先出手、先倒下，亡语最早生效，后面的随从都能用上）"
INTRO = (
    "  🧭 站位规则：战斗时随从从最左边开始依次出手；亡语召唤的随从出现在原位；"
    "嘲讽会被优先攻击。本局只提示这一次"
)


def terminal_lines(path):
    """[(回合, 🧭 那一行)]，按打印顺序；以及运行器交给显示端的每一条站位建议。"""
    from lscs.advisor.display import AdviceDisplay
    from lscs.advisor.rules_config import load_rules_config
    from lscs.advisor.runner import AdviceRunner
    from lscs.cli import DEFAULT_RULES

    lines, advices, turn = [], [], [0]

    class Display(AdviceDisplay):
        def show_position(self, state, advice):
            advices.append((state, advice))
            super().show_position(state, advice)

    def out(line):
        if "🧭" in line:
            lines.append((turn[0], line))

    runner = AdviceRunner(
        load_rules_config(DEFAULT_RULES), CARDS.name, Display(CARDS.name, out), cards=CARDS
    )
    for state, events in updates_of(path):
        turn[0] = state.turn
        runner.on_update(state, events)
    return lines, advices


@NEEDS_USER_LOG
def test_user_game_in_the_terminal():
    lines, advices = terminal_lines(USER_LOG)
    t9 = f"  🧭 站位：把 左数第 2 个 孢林卵怪 拖到最左边{DEATHRATTLE}"
    assert lines == [
        (3, INTRO),
        (3, f"  🧭 站位：把 左数第 3 个 孢林卵怪 拖到最左边{DEATHRATTLE}"),
        (3, "  🧭 站位已调好"),
        (9, t9),
        (10, t9),
        (11, t9),
        (12, t9),  # 回合开始（铸币 10、场面满、规则不买）；卖掉孢林卵怪之后不再提示
    ]
    shown = [(s, a) for s, a in advices if a is not None and a.moves]
    assert [a.intro for _, a in shown].count(True) == 1 and shown[0][1].intro
    assert {s.turn for s, _ in shown} == {3, 9, 10, 11, 12}
    assert max(len(a.moves) for _, a in shown) == 1
    # 没买完时不提示：第 9 回合打出泰瑞尔后还剩 3 金、规则还要买，是 None
    assert any(a is None and s.turn == 9 and len(s.me.board) == 6 for s, a in advices)


def test_cc0_game_in_the_terminal():
    lines, advices = terminal_lines(REAL_LOG)
    imp = f"  🧭 站位：把 左数第 2 个 粗鄙小鬼 拖到最左边{DEATHRATTLE}"
    assert lines == [
        (5, INTRO),
        (5, f"  🧭 站位：把 左数第 7 个 蹒跚的食尸鬼 拖到 BGS_001 的左边{DEATHRATTLE}"),
        (5, imp),
        (6, imp),
        (7, imp),
        # 第 16 回合打出无私的英雄后被玩家拖到了第 2 个（之后又拖到第 3 个：同一步，不再重复）
        (16, f"  🧭 站位：把 左数第 2 个 无私的英雄 拖到最左边{DEATHRATTLE}"),
    ]
    assert sum(a.intro for _, a in advices if a is not None) == 1
