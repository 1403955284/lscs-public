"""复盘接线：待办、tick、建议收集（对局复盘设计文档第 8 节）。"""

import json
from datetime import datetime

from factory import hand_minion, hero_select_state, make_state, minion, shop_minion
from helpers import NEEDS_USER_LOG, USER_LOG, replay_log
from test_review_flags import RULES

from lscs.advisor.models import Action, Advice, ChoiceAdvice, HeroAdvice, Plan
from lscs.config import ReviewConfig
from lscs.review.reviewer import GameReviewer
from lscs.state.models import Choice, ChoiceKind, ChoiceOption, Event, EventType, Phase

WHEN = datetime(2026, 10, 11, 23, 20)
NAMES = {"A": "蚀脑怪", "B": "虚悯者", "H": "凯瑞尔·罗姆"}


def name(card_id):
    return NAMES.get(card_id, card_id)


class Sink:
    """记下 show_review 收到的复盘。"""

    def __init__(self):
        self.reviews = []

    def show_review(self, state, review):
        self.reviews.append(review)


def ev(event_type, turn=1, **data):
    return Event(type=event_type, turn=turn, data=data)


def reviewer(tmp_path, sink=None, **kw):
    sink = sink or Sink()
    out = []
    r = GameReviewer(
        kw.pop("cfg", ReviewConfig()),
        RULES,
        name,
        sink,
        directory=tmp_path / "reviews",
        base=tmp_path,
        now=lambda: WHEN,
        out=out.append,
        **kw,
    )
    return r, sink, out


SHOP = [shop_minion(10, "A", 2, 2), shop_minion(11, "B", 3, 3)]


def play_game(r, place=4, seq=0, hero="H"):
    """一局：选英雄、第 1 回合买一张、战斗、结束。返回最后的 seq。"""

    def feed(state, *events):
        nonlocal seq
        seq += 1
        state = state.model_copy(update={"seq": seq})
        r.on_update(state, list(events))
        return state

    feed(make_state(phase=Phase.HERO_SELECT, turn=0, hero=hero), ev(EventType.GAME_STARTED, 0))
    start = feed(make_state(gold=3, shop=SHOP, hero=hero), ev(EventType.RECRUIT_STARTED))
    r.tap.show_rules(start, Advice(state_seq=start.seq, turn=1, plan=plan(("buy", 10))))
    feed(make_state(gold=0, shop=SHOP[1:], hand=[hand_minion(10, "A", 2, 2)], hero=hero))
    feed(make_state(phase=Phase.COMBAT, board=[minion(10, "A", 2, 2)], hero=hero))
    over = make_state(phase=Phase.GAME_OVER, health=0, hero=hero).model_copy(
        update={"final_place": place}
    )
    feed(over, ev(EventType.GAME_ENDED, 1, place=place))
    return seq


def plan(*actions, source="rules"):
    return Plan(source=source, actions=tuple(Action(type=t, entity_id=e) for t, e in actions))


def test_review_comes_on_the_first_tick_after_the_game_and_only_once(tmp_path):
    r, sink, out = reviewer(tmp_path)
    play_game(r)
    assert sink.reviews == []  # on_update 只记待办
    r.tick()
    r.tick()
    assert len(sink.reviews) == 1 and out == []
    review = sink.reviews[0]
    assert review.record.place == 4 and review.hero_name == "凯瑞尔·罗姆"
    assert review.record.turns[0].advice.followed == 1  # tap 收到的规则建议进了 TurnAdvice
    assert review.path.name == "2026-10-11_2320_凯瑞尔·罗姆_第4名.md"
    assert review.shown_path == str(review.path.relative_to(tmp_path))
    entries = [json.loads(line) for line in r.history_path.read_text(encoding="utf-8").splitlines()]
    assert [(e["place"], e["advised"], e["followed"]) for e in entries] == [(4, 1, 1)]
    assert r.last_review is review


def test_a_new_game_clears_the_pending_review(tmp_path):
    r, sink, _out = reviewer(tmp_path)
    play_game(r)
    r.on_update(
        make_state(phase=Phase.HERO_SELECT, turn=0, seq=50), [ev(EventType.GAME_STARTED, 0)]
    )
    r.tick()
    assert sink.reviews == []


def test_two_games_in_one_poll_only_review_the_last(tmp_path):
    r, sink, _out = reviewer(tmp_path)
    seq = play_game(r, place=6)
    play_game(r, place=2, seq=seq)
    r.tick()
    assert [review.record.place for review in sink.reviews] == [2]


def test_save_history_off_and_bad_history_lines(tmp_path):
    r, sink, _out = reviewer(tmp_path, save_history=False)
    history = tmp_path / "reviews" / "history.jsonl"
    history.parent.mkdir()
    line = json.dumps({"place": 1, "flags": ["gold_unspent"]})
    history.write_text(f"{line}\n坏行\n{line}\n", encoding="utf-8")
    play_game(r)
    r.tick()
    assert history.read_text(encoding="utf-8").count("\n") == 3  # 没有追加
    assert sink.reviews[0].habits.games == 3  # 两局历史 + 这一局


def test_errors_print_one_line_and_do_not_raise(tmp_path, monkeypatch):
    import lscs.review.reviewer

    r, sink, out = reviewer(tmp_path)
    play_game(r)

    def boom(*_args, **_kw):
        raise RuntimeError("坏了")

    monkeypatch.setattr(lscs.review.reviewer, "review_game", boom)
    r.tick()
    assert out == ["  📝 复盘出错：坏了"] and sink.reviews == []


def test_display_error_does_not_break_the_loop(tmp_path):
    class Broken:
        def show_review(self, state, review):
            raise ValueError("显示坏了")

    r, _sink, out = reviewer(tmp_path, sink=Broken())
    play_game(r)
    r.tick()
    assert out == ["  📝 复盘出错：显示坏了"]


def test_tap_collects_ai_advice_hero_and_choice_picks(tmp_path):
    r, sink, _out = reviewer(tmp_path)
    tap = r.tap
    select = hero_select_state()
    tap.show_hero(select, HeroAdvice(source="rules", signature=(), pick=90, reason=""))
    assert tap.hero_ai_pick is None
    tap.show_hero(select, HeroAdvice(source="llm", signature=(), pick=89, reason=""))
    assert tap.hero_ai_pick == "BG21_HERO_000"
    options = tuple(
        ChoiceOption(entity_id=e, card_id=c, card_type="MINION", index=i, atk=1, health=1, tier=1)
        for i, (e, c) in enumerate([(40, "X"), (41, "Y")])
    )
    state = make_state().model_copy(
        update={
            "choice": Choice(id=7, kind=ChoiceKind.DARK_GIFT, source_card_id="", options=options)
        }
    )
    tap.show_choice(state, None)
    tap.show_choice(state, ChoiceAdvice(choice_id=7, pick=41, reason=""))
    assert tap.ai_choices == {7: "Y"}
    fast = Advice(state_seq=3, turn=2, plan=plan(("upgrade", None), source="llm"))
    tap.show_ai(state, fast, False)
    tap.show_ai(state, fast.model_copy(update={"expired": "changed"}), False)
    deep = fast.model_copy(update={"lane": "deep"})
    tap.show_ai(state, deep, False)
    tap.show_rules(state, Advice(state_seq=2, turn=2, plan=plan(("refresh", None))))
    tap.show_rules(state, Advice(state_seq=4, turn=2, plan=plan(("buy", 10))))  # 不是第一条
    log = tap.logs[2]
    assert log.rules_start[0] == 2 and len(log.fast) == 1 and log.deep == deep
    # 其余方法什么都不做
    tap.ai_pending("deep")
    tap.ai_text("x")
    tap.ai_repairing("fast", "x")
    tap.rules_error()
    tap.ai_summary(1, 2, 3)
    tap.show_opponents(state, None)
    tap.show_review(state, None)
    tap.show_position(state, None)
    from lscs.advisor.models import GamePlan

    game_plan = GamePlan(comp="畸变神明")
    tap.show_plan(state, game_plan, None)
    assert tap.plans == [(state.turn, game_plan)]
    # 新对局清空
    r.on_update(make_state(phase=Phase.HERO_SELECT, turn=0), [ev(EventType.GAME_STARTED, 0)])
    assert (tap.logs, tap.hero_ai_pick, tap.ai_choices, tap.plans) == ({}, None, {}, [])


def test_plans_reach_the_record_and_the_history(tmp_path):
    from lscs.advisor.models import GamePlan

    r, sink, _ = reviewer(tmp_path)
    seq = 0

    def feed(state, *events):
        nonlocal seq
        seq += 1
        state = state.model_copy(update={"seq": seq})
        r.on_update(state, list(events))
        return state

    feed(make_state(phase=Phase.HERO_SELECT, turn=0), ev(EventType.GAME_STARTED, 0))
    start = feed(make_state(gold=3, shop=SHOP), ev(EventType.RECRUIT_STARTED))
    r.tap.show_rules(start, Advice(state_seq=start.seq, turn=1, plan=plan(("buy", 10))))
    r.tap.show_plan(start, GamePlan(comp="畸变神明", stage="前期"), None)  # 第 1 回合定下
    feed(make_state(phase=Phase.COMBAT, board=[minion(10, "A", 2, 2)]))
    over = make_state(phase=Phase.GAME_OVER, health=0).model_copy(update={"final_place": 5})
    feed(over, ev(EventType.GAME_ENDED, 1, place=5))
    r.tick()
    (review,) = sink.reviews
    assert review.record.turns[0].advice.plan_comp == "畸变神明"
    assert (review.record.plan_comps, review.record.plan_changes) == (("畸变神明",), 0)
    entry = json.loads((tmp_path / "reviews" / "history.jsonl").read_text(encoding="utf-8"))
    assert (entry["plan_comps"], entry["plan_changes"]) == (["畸变神明"], 0)


@NEEDS_USER_LOG
def test_user_game_through_the_reviewer(tmp_path):
    from lscs.cli import DEFAULT_POOL
    from lscs.state.pool import load_pool_config

    r, sink, out = reviewer(tmp_path)
    for state, events in replay_log(USER_LOG, load_pool_config(DEFAULT_POOL)):
        r.on_update(state, events)
    r.tick()
    (review,) = sink.reviews
    assert [m.turns for m in review.moments] == [(7, 8), (5,), (11,)]
    assert review.path.exists() and out == []


def test_review_config_history_games_is_used(tmp_path):
    history = tmp_path / "reviews" / "history.jsonl"
    history.parent.mkdir()
    history.write_text(json.dumps({"place": 1, "flags": []}) + "\n", encoding="utf-8")
    r, sink, _out = reviewer(tmp_path, cfg=ReviewConfig(history_games=3))
    play_game(r)
    r.tick()
    assert sink.reviews[0].habits is None  # 一局历史 + 这一局，不足 3 局


def test_restarting_watch_does_not_review_the_same_game_twice(tmp_path):
    first, sink, _out = reviewer(tmp_path)
    play_game(first)
    first.tick()
    (review,) = sink.reviews
    # 重新启动 watch：从头读同一份日志，最后那局又结束了一次
    again, sink2, out = reviewer(tmp_path)
    play_game(again)
    again.tick()
    assert sink2.reviews == []
    assert out == [f"  📝 这局已经复盘过：{review.shown_path}"]
    assert again.already_done and not first.already_done
    assert len(list((tmp_path / "reviews").glob("*.md"))) == 1
    assert r_lines(again) == 1
    # 不一样的一局（名次不同）照常复盘
    play_game(again, place=2)
    again.tick()
    assert len(sink2.reviews) == 1 and r_lines(again) == 2


def test_the_same_game_on_another_day_or_from_a_replay_is_reviewed(tmp_path):
    from datetime import timedelta

    first, _sink, _out = reviewer(tmp_path)
    play_game(first)
    first.tick()
    later = GameReviewer(
        ReviewConfig(),
        RULES,
        name,
        Sink(),
        directory=tmp_path / "reviews",
        base=tmp_path,
        now=lambda: WHEN + timedelta(days=1),
    )
    play_game(later)
    later.tick()
    assert r_lines(later) == 2
    # lscs review / replay（不记历史）总是出复盘
    replayed, sink, _out = reviewer(tmp_path, save_history=False)
    play_game(replayed)
    replayed.tick()
    assert len(sink.reviews) == 1


def r_lines(r):
    return r.history_path.read_text(encoding="utf-8").count("\n")
