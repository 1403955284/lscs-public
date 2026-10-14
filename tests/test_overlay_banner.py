"""棋盘顶部的战斗结果横幅：什么时候显示、写什么（overlay.banner，不开窗口）。"""

import pytest
from factory import hero_select_state, make_state
from helpers import NEEDS_USER_LOG

from lscs.overlay.banner import BANNER_LINGER_SECONDS, Banner, BannerTracker
from lscs.state.models import CombatResult, Event, EventType, Phase

NAMES = {"H2": "洛卡拉", "H_SAURFANG": "萨鲁法尔大王", "H_KELTHUZAD": "克尔苏加德"}


def name(card_id):
    return NAMES.get(card_id, card_id)


def combat_result(turn=10, **overrides):
    values = dict(
        turn=turn,
        opponent_id=2,
        opponent_hero="H2",
        outcome="win",
        ghost=False,
        lost=0,
        dealt=5,
        my_health=30,
        my_armor=6,
        opponent_health=30,
        opponent_armor=9,
        me_dead=False,
        opponent_dead=False,
    )
    values.update(overrides)
    return CombatResult(**values)


def settled(result):
    data = {"opponent": result.opponent_id, "outcome": result.outcome}
    return [Event(type=EventType.COMBAT_RESULT, turn=result.turn, data=data)]


def state(result, turn=10, phase=Phase.COMBAT, **update):
    return make_state(turn=turn, phase=phase).model_copy(update={"last_combat": result, **update})


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def shown(tracker, snapshot, events=()):
    tracker.on_update(list(events))
    return tracker.current(snapshot, name)


BANNERS = [
    (combat_result(), "本场已定：赢 · 洛卡拉掉 5 血"),
    (
        combat_result(
            opponent_hero="H_SAURFANG",
            dealt=15,
            opponent_health=-8,
            opponent_armor=0,
            opponent_dead=True,
        ),
        "本场已定：赢 · 萨鲁法尔大王掉 15 血 · 对手淘汰",
    ),
    (
        combat_result(opponent_hero="H_KELTHUZAD", ghost=True, dealt=0, opponent_health=0),
        "本场已定：赢 · 残影，不掉血",
    ),
    (
        combat_result(opponent_id=None, opponent_hero=None, dealt=0, opponent_health=None),
        "本场已定：赢",
    ),
    (
        combat_result(outcome="loss", lost=2, dealt=0, my_health=30, my_armor=8),
        "本场已定：输 · 你掉 2 血，剩 30+8",
    ),
    (
        combat_result(outcome="loss", lost=18, dealt=0, my_health=-14, my_armor=0, me_dead=True),
        "本场已定：输 · 你掉 18 血 · 你被淘汰",
    ),
    (combat_result(outcome="tie", dealt=0), "本场已定：平 · 都没掉血"),
]


@pytest.mark.parametrize(
    ("result", "text"),
    BANNERS,
    ids=["win", "win-eliminated", "ghost", "unknown", "loss", "loss-eliminated", "tie"],
)
def test_banner_text_for_each_kind_of_result(result, text):
    banner = shown(BannerTracker(), state(result), settled(result))
    assert banner == Banner(text, result.outcome)


def test_banner_lasts_until_the_next_recruit_really_starts():
    tracker = BannerTracker()
    last = combat_result(turn=9)  # 上一场的结果，招募阶段里还留在快照里
    started = [Event(type=EventType.COMBAT_STARTED, turn=10)]
    assert shown(tracker, state(last), started) is None
    this = combat_result(turn=10)
    assert shown(tracker, state(this), settled(this)) is not None
    # 日志里战斗一写完（不到 2 秒）就换了回合，快照已经是第 11 回合的招募，但还没到 MAIN_ACTION：
    # 画面上战斗动画还要播 20~70 秒，横幅留着
    before = state(this, turn=11, phase=Phase.RECRUIT, main_action=False)
    assert shown(tracker, before) is not None
    recruit = before.model_copy(update={"main_action": True})
    assert shown(tracker, recruit, [Event(type=EventType.RECRUIT_STARTED, turn=11)]) is None
    # 第 11 回合招募结束（STEP 变成 MAIN_END，还没换回合）：也不会再冒出来
    assert shown(tracker, before) is None
    selecting = hero_select_state().model_copy(update={"last_combat": this})
    assert shown(tracker, selecting) is None


def test_banner_goes_with_main_action_even_without_the_event():
    this = combat_result(turn=10)
    tracker = BannerTracker()
    shown(tracker, state(this), settled(this))
    later = state(this, turn=11, phase=Phase.RECRUIT, main_action=True)
    assert shown(tracker, later) is None
    two_turns = state(this, turn=12, phase=Phase.RECRUIT, main_action=False)
    assert shown(tracker, two_turns) is None  # 只留到紧接着的那个回合


def test_a_new_game_forgets_the_last_recruit():
    tracker = BannerTracker()
    shown(
        tracker,
        state(None, turn=2, phase=Phase.RECRUIT),
        [Event(type=EventType.RECRUIT_STARTED, turn=2)],
    )
    shown(
        tracker,
        state(None, turn=0, phase=Phase.HERO_SELECT),
        [Event(type=EventType.GAME_STARTED, turn=0)],
    )
    first = combat_result(turn=1)
    shown(tracker, state(first, turn=1), settled(first))
    animating = state(first, turn=2, phase=Phase.RECRUIT, main_action=False)
    assert shown(tracker, animating) is not None  # 上一局第 2 回合的招募开始不算数


def test_no_banner_when_switched_off():
    result = combat_result()
    assert shown(BannerTracker(enabled=False), state(result), settled(result)) is None


def test_banner_lingers_twenty_seconds_after_i_am_eliminated():
    assert BANNER_LINGER_SECONDS == 20
    clock = FakeClock(100.0)
    tracker = BannerTracker(clock=clock)
    dead = combat_result(
        turn=13, outcome="loss", lost=18, dealt=0, my_health=-14, my_armor=0, me_dead=True
    )
    over = state(dead, turn=13, phase=Phase.GAME_OVER, final_place=3)
    # 我被淘汰：结果和对局结束在同一次更新里
    events = [*settled(dead), Event(type=EventType.GAME_ENDED, turn=13)]
    text = "本场已定：输 · 你掉 18 血 · 你被淘汰"
    assert shown(tracker, over, events) == Banner(text, "loss", expires_at=120.0)
    clock.now = 119.0
    assert shown(tracker, over).expires_at == 120.0
    clock.now = 120.0
    assert shown(tracker, over) is None


def test_banner_when_the_last_opponent_dies_gets_a_deadline_once_the_game_ends():
    clock = FakeClock(50.0)
    tracker = BannerTracker(clock=clock)
    won = combat_result(
        turn=16,
        dealt=9,
        opponent_health=-2,
        opponent_armor=0,
        opponent_dead=True,
    )
    text = "本场已定：赢 · 洛卡拉掉 9 血 · 对手淘汰"
    assert shown(tracker, state(won, turn=16), settled(won)) == Banner(text, "win")
    clock.now = 50.4
    over = state(won, turn=16, phase=Phase.GAME_OVER, final_place=1)
    ended = [Event(type=EventType.GAME_ENDED, turn=16)]
    assert shown(tracker, over, ended) == Banner(text, "win", expires_at=70.0)  # 从结果到达算起


def banner_spans(updates):
    """把整份录像推过横幅：[(回合, 出现时的事件, 消失时的事件), ...]。"""
    tracker = BannerTracker()
    spans, current = [], None
    for snapshot, events in updates:
        banner = shown(tracker, snapshot, events)
        types = [e.type for e in events]
        if banner is not None and current is None:
            current = [snapshot.last_combat.turn, types]
        elif banner is None and current is not None:
            spans.append((*current, types))
            current = None
    if current is not None:
        spans.append((*current, None))
    return spans


def assert_banner_lasts_until_the_next_recruit(spans, turns):
    assert [turn for turn, *_ in spans] == turns
    for _turn, appeared, gone in spans:
        assert EventType.COMBAT_RESULT in appeared
        assert gone is None or EventType.RECRUIT_STARTED in gone


@NEEDS_USER_LOG
def test_user_recording_banner_lasts_through_each_combat_animation():
    from helpers import USER_LOG, replay_log

    from lscs.cli import DEFAULT_POOL
    from lscs.state.pool import load_pool_config

    spans = banner_spans(replay_log(USER_LOG, load_pool_config(DEFAULT_POOL)))
    # 最后一场我被淘汰：对局结束后还留着（20 秒后由窗口清掉）
    assert_banner_lasts_until_the_next_recruit(spans, list(range(1, 14)))
    assert spans[-1][2] is None


def test_cc0_banner_lasts_until_the_next_recruit_starts():
    # 2022 年的客户端战斗一写完（0.3~0.6 秒后）就写下一回合的 MAIN_ACTION，横幅在画面上一闪而过；
    # 现在的客户端（用户录像）要等动画播完、招募真正开始才写，横幅留 20~70 秒
    from helpers import CC0_POOL, REAL_LOG, replay_log

    spans = banner_spans(replay_log(REAL_LOG, CC0_POOL))
    assert_banner_lasts_until_the_next_recruit(spans, list(range(1, 17)))
