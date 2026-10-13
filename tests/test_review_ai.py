"""AI 复盘：系统提示、事实消息、解析校验、改正和接线（对局复盘设计文档第 6 节）。"""

import json
from dataclasses import replace
from datetime import datetime

import pytest
from helpers import FIXTURES, NEEDS_USER_LOG, USER_LOG, replay_log
from test_review_flags import RULES, advice, combat, curve_turns, record
from test_review_flags import name as card_name

from lscs.cards.db import CardDB
from lscs.config import LLMConfig, ReviewConfig
from lscs.llm.parse import EmptyResponse, ParseError
from lscs.llm.provider import FakeProvider, LLMError
from lscs.llm.record import Recorder
from lscs.review.ai import (
    REVIEW_EXAMPLE,
    AIReview,
    Moment,
    ReviewCall,
    build_review_message,
    build_review_system_prompt,
    parse_review,
    run_review,
    turn_line,
)
from lscs.review.record import ChoicePick, RecordBuilder
from lscs.review.report import Habits, review_game

WHEN = datetime(2026, 10, 11, 23, 20)


@pytest.fixture(scope="module")
def cards():
    return CardDB.from_json_file(FIXTURES / "cards_mini.json")


def answer(*turns, focus="升本也要买随从", good="", **moment):
    moments = [
        {
            "turn": t,
            "title": "升本没补场面",
            "what": "只升本",
            "why": "掉血",
            "next": "买一张",
            **moment,
        }
        for t in turns
    ]
    return json.dumps({"moments": moments, "focus": focus, "good": good}, ensure_ascii=False)


GAME = record(curve_turns())


def test_system_prompt_is_separate_and_has_the_example_and_guide():
    from lscs.llm.prompt import build_system_prompt

    system = build_review_system_prompt("攻略：按节奏升本")
    assert system.startswith(
        "你是《炉石传说》酒馆战棋的教练，正在给一位 6000 分以下的新手写一局打完后的复盘。"
    )
    assert json.dumps(REVIEW_EXAMPLE, ensure_ascii=False) in system
    assert system.endswith("【攻略要点】\n攻略：按节奏升本")
    assert "json" in system  # DeepSeek 的 json 模式要求提示里有"json"
    assert build_system_prompt("x") != system


def test_parse_valid_review_and_cut_long_text(cards):
    seen = GAME.model_copy(update={"seen_cards": frozenset({"BG36_099"})})
    review = parse_review(
        answer(8, 5, good="第 11 回合凑出金色蚀脑怪", why="很长" * 50), seen, cards
    )
    with pytest.raises(ParseError, match="提到了本局没出现过的牌：蚀脑怪"):
        parse_review(answer(8, 5, good="第 11 回合凑出金色蚀脑怪"), GAME, cards)
    assert [m.turn for m in review.moments] == [8, 5]
    # 没有标点可以断开时才硬截（加"…"）
    assert len(review.moments[0].why) == 90 and review.moments[0].why.endswith("…")
    assert review.good == "第 11 回合凑出金色蚀脑怪"
    # 提示词要求 15 字、30 字以内，多写几个字照样保留
    long = parse_review(answer(3, 4, title="一" * 20, focus="二" * 40), GAME)
    assert (len(long.moments[0].title), len(long.focus)) == (20, 40)


def test_long_focus_is_cut_at_a_clause_not_mid_sentence():
    # 实测时 AI 的下局重点 30 多字，被硬截成了半句话
    focus = "升本的回合也要买一张随从，场上不到 6 个先补场面再升本"
    assert parse_review(answer(3, 4, focus=focus), GAME).focus == focus
    too_long = "升本的回合也要买一张随从，场上不到 6 个先补场面再升本，连掉两场血就先补战力，别再急着升 6 本了，先保住血量"
    assert len(too_long) > 50
    assert (
        parse_review(answer(3, 4, focus=too_long), GAME).focus
        == "升本的回合也要买一张随从，场上不到 6 个先补场面再升本，连掉两场血就先补战力"
    )
    title = "连续两回合只升本没买随从；结果连掉三十血，之后名次一直往下掉"
    # 截在上限（22 字）以内最后一个标点之前
    parsed = parse_review(answer(3, 4, title=title), GAME).moments[0].title
    assert parsed == "连续两回合只升本没买随从；结果连掉三十血"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[]", "不是 JSON 对象"),
        ("{", "不是合法的 JSON"),
        (json.dumps({"focus": "x"}), "缺少 moments 列表"),
        (answer(3), "moments 应该是 2~3 条，现在是 1 条"),
        (answer(3, 4, 5, 6), "moments 应该是 2~3 条，现在是 4 条"),
        (answer(3, 11), "第 2 条的 turn 不是本局打过的回合（1~10）"),
        (answer(3, 0), "第 2 条的 turn 不是本局打过的回合（1~10）"),
        (answer(3, 4, why=""), "第 1 条缺少 why"),
        (answer(3, 4, what=" ", next=""), "第 1 条缺少 what、next"),
        (answer(3, 4, focus=""), "缺少 focus"),
        (json.dumps({"moments": [1, 2], "focus": "x"}), "第 1 条 moments 不是对象"),
    ],
)
def test_parse_rejects_bad_reviews(text, problem):
    with pytest.raises(ParseError, match=problem):
        parse_review(text, GAME)


def test_parse_empty_and_short_game():
    with pytest.raises(EmptyResponse):
        parse_review("  ", GAME)
    short = record(curve_turns()[:4])
    assert len(parse_review(answer(2), short).moments) == 1
    with pytest.raises(ParseError, match="1~3 条"):
        parse_review(answer(1, 2, 3, 4), short)


@NEEDS_USER_LOG
def test_parse_rejects_cards_that_never_showed_up(cards, user_record):
    # 熔岩潜伏者：卡牌数据里有、这局没出现过（飘荡祭品这局在商店里出现过，可以提）
    fake = answer(8, 5, what="第 8 回合该买熔岩潜伏者")
    with pytest.raises(ParseError, match="提到了本局没出现过的牌：熔岩潜伏者"):
        parse_review(fake, user_record, cards)
    seen = answer(8, 5, what="第 8 回合该买飘荡祭品和蚀脑怪(金)")
    assert parse_review(seen, user_record, cards).moments[0].what.startswith("第 8 回合")
    # 不查卡牌数据时不校验卡名；英雄名、种族名不算
    assert parse_review(fake, user_record).moments
    assert parse_review(answer(8, 5, why="厄祖玛特的畸变很强"), user_record, cards)


@pytest.fixture(scope="module")
def user_record(cards):
    from lscs.cli import DEFAULT_POOL
    from lscs.state.pool import load_pool_config

    builder = RecordBuilder(RULES.buy_cost_for, cards.normal_id)
    for state, events in replay_log(USER_LOG, load_pool_config(DEFAULT_POOL)):
        builder.on_update(state, events)
    return builder.finish()


@NEEDS_USER_LOG
def test_facts_message_for_the_user_game(cards, user_record):
    review = review_game(user_record, RULES, cards.name, WHEN)
    text = build_review_message(review.record, review.flags, review.moments, cards.name)
    lines = text.splitlines()
    assert lines[:5] == [
        "对局：BG21_HERO_000 | 第 3 名 | 13 个回合 | 本局种族：畸变、恶魔、龙、鱼人、海盗",
        "最后：第 13 回合打BG23_HERO_201掉 18 血被淘汰",
        "",
        "【提醒】（按要紧程度排好；★ 是建议写进复盘的）",
        "★ 第 7、8 回合 升本不补场面（很要紧）：连续升本（5 本、6 本）但没买随从，场上一直只有 5 个"
        "（这时该有 7 个），接着第 8、9 回合连掉 30 血（34 → 4）",
    ]
    assert "★ 第 11 回合 做得好：凑出金色蚀脑怪，打出 10 血" in lines
    assert "  第 12 回合 做得好：打出 15 血" in lines
    t8 = next(line for line in lines if line.startswith("第 8 回合"))
    assert t8 == (
        "第 8 回合 | 5→6 本 | 铸币 10→0 | 升本 10 金；技能 | 选择：英雄技能：选 BG21_HERO_000_Buddyt2"
        " | 场面 5 个（身材 79）：孢林卵怪 7/8、蚀脑怪 8/10、蚀脑怪 6/8、BG36_108 4/6、虚悯者 11/11"
        " | 战斗 负 掉 15（BG23_HERO_201，7 个随从、身材 194）"
    )
    t6 = next(line for line in lines if line.startswith("第 6 回合"))
    assert "| 铸币 4→1（饰品 精神鞭笞者肖像 4 金） |" in t6
    assert "【建议采纳】" not in text and "【最近" not in text


def test_facts_message_with_advice_choices_and_history():
    turns = curve_turns(
        {
            3: dict(
                advice=advice(
                    advised=3,
                    followed=1,
                    missed_buys=("A",),
                    deep_summary="先补 两张\n畸变",
                    rules_actions=(("buy", "A"), ("upgrade", None)),
                ),
                choices=(
                    ChoicePick(id=2, kind="dark_gift", card_id="Y", cost=None, ai_card_id="X"),
                ),
                gold_gained=2,
            )
        }
    )
    rec = record(turns, [combat(3, dealt=4), combat(4, ghost=True)])
    habit = Habits(games=5, avg_place=3.4, kinds=(("upgrade_without_board", 3),))
    text = build_review_message(rec, (), (), card_name, habit)
    line = next(x for x in text.splitlines() if x.startswith("第 3 回合"))
    assert line == (
        "第 3 回合 | 2 本 | 铸币 3→0（回合中 +2） | 买 蚀脑怪 | 选择：黑暗之赐：选 泰瑞尔（AI 推荐 飘荡祭品）"
        " | 场面 3 个（身材 12）：蚀脑怪 2/2、蚀脑怪 2/2、蚀脑怪 2/2 | 战斗 胜 打出 4（H，3 个随从、身材 30）"
        ' | 建议：规则 买蚀脑怪、升本；深度 "先补 两张 畸变"；采纳 1/3'
    )
    assert "战斗 打残影 没掉血（H的残影，3 个随从、身材 30）" in text
    assert "（没有触发提醒）" in text
    assert text.endswith(
        "【建议采纳】规则和 AI 一共建议 3 个动作，做了 1 个；建议买而没买：蚀脑怪\n\n"
        "【最近 5 局】平均第 3.4 名；升本不补场面 3 局。"
    )
    # 没有战斗记录的回合（比如中途开始）不写战斗
    assert turn_line(rec, rec.turns[0], card_name).endswith("| 场面 1 个（身材 4）：蚀脑怪 2/2")


# ---- 调用 ----


def call(provider, recorder=None, timeout=75.0):
    return ReviewCall(
        provider=provider,
        model="deepseek-flash",
        thinking="low",
        timeout=timeout,
        recorder=recorder,
    )


def test_run_review_once(tmp_path):
    provider = FakeProvider([answer(3, 4)])
    recorder = Recorder(tmp_path, now=lambda: WHEN)
    result = run_review(call(provider, recorder), "系统", "事实", GAME, None, lambda: 0.0)
    assert result.review is not None and result.error is None and not result.repaired
    (only,) = provider.calls
    assert (only.system, only.user, only.thinking, only.max_tokens, only.timeout) == (
        "系统",
        "事实",
        "low",
        4000,
        75.0,
    )
    (row,) = [json.loads(x) for x in (tmp_path / "llm-2026-10-11.jsonl").read_text().splitlines()]
    assert (row["lane"], row["outcome"], row["repair"], row["user"]) == (
        "review",
        "合法",
        False,
        "事实",
    )


def test_run_review_repairs_once(tmp_path):
    provider = FakeProvider([answer(3, 99), answer(3, 4)])
    recorder = Recorder(tmp_path, now=lambda: WHEN)
    result = run_review(call(provider, recorder), "系统", "事实", GAME, None, lambda: 0.0)
    assert result.review is not None and result.repaired
    first, second = provider.calls
    assert second.turns == (
        ("assistant", answer(3, 99)),
        (
            "user",
            "你刚才的回答不合法：第 2 条的 turn 不是本局打过的回合（1~10）。请按同样的规则改正后，重新输出完整的 json。",
        ),
    )
    assert second.thinking == "disabled" and second.user == "事实"
    rows = [json.loads(x) for x in (tmp_path / "llm-2026-10-11.jsonl").read_text().splitlines()]
    assert [(r["outcome"][:2], r["repair"]) for r in rows] == [("无效", False), ("合法", True)]


@pytest.mark.parametrize(
    ("responses", "error", "calls"),
    [
        ([answer(99), answer(99)], "AI 改正后仍不合格", 2),
        ([""], "AI 返回了空内容", 1),
        ([LLMError("timeout", "AI 超时")], "AI 超时", 1),
        ([answer(99), LLMError("other", "AI 调用失败（HTTP 500）")], "AI 调用失败（HTTP 500）", 2),
        ([RuntimeError("坏了")], "AI 调用出错", 1),
    ],
)
def test_run_review_falls_back(responses, error, calls):
    provider = FakeProvider(responses)
    result = run_review(call(provider), "s", "u", GAME, None, lambda: 0.0)
    assert (result.review, result.error, len(provider.calls)) == (None, error, calls)


def test_no_repair_when_time_is_up():
    times = iter([0.0, 0.0, 74.0, 74.0])
    provider = FakeProvider([answer(3, 99), answer(3, 4)])
    result = run_review(call(provider), "s", "u", GAME, None, lambda: next(times))
    assert result.error == "回答不合格（第 2 条的 turn 不是本局打过的回合（1~10））"
    assert len(provider.calls) == 1


# ---- 接线 ----


class Sink:
    def __init__(self):
        self.reviews = []

    def show_review(self, state, review):
        self.reviews.append(review)


def reviewer(tmp_path, provider, *, jobs=None, clock=lambda: 0.0, cfg=None, cards=None):
    from test_reviewer import name

    from lscs.review.reviewer import GameReviewer

    sink, out = Sink(), []
    run_async = jobs.append if jobs is not None else (lambda job: job())
    r = GameReviewer(
        cfg or ReviewConfig(),
        RULES,
        name,
        sink,
        cards or CardDB([]),
        provider,
        LLMConfig(api_key="sk-x-123456789") if provider is not None else None,
        "攻略",
        Recorder(tmp_path / "recordings", now=lambda: WHEN),
        directory=tmp_path / "reviews",
        base=tmp_path,
        clock=clock,
        now=lambda: WHEN,
        run_async=run_async,
        out=out.append,
    )
    return r, sink, out


def test_ai_review_is_delivered_by_tick_and_rewrites_the_file(tmp_path):
    from test_reviewer import play_game

    provider = FakeProvider([answer(1, good="第 1 回合买对了", focus="每回合都买")])
    jobs = []
    r, sink, out = reviewer(tmp_path, provider, jobs=jobs)
    play_game(r)
    r.tick()
    (pending,) = sink.reviews
    assert pending.ai_state == "pending" and len(jobs) == 1
    rules_text = pending.path.read_text(encoding="utf-8")
    jobs[0]()  # 工作线程
    assert len(sink.reviews) == 1  # 结果在队列里，tick 时才交付
    r.tick()
    done = sink.reviews[-1]
    assert (done.ai_state, done.ai.focus, done.ai.good) == ("done", "每回合都买", "第 1 回合买对了")
    text = done.path.read_text(encoding="utf-8")
    assert text != rules_text
    assert (
        "1. **第 1 回合 升本没补场面** — 只升本\n   - 为什么要紧：掉血\n   - 下次：买一张" in text
    )
    assert "**做得好**：第 1 回合买对了" in text and "**下局重点**：每回合都买" in text
    assert "## 提醒（规则）" in text and "## 逐回合" in text
    assert out == []
    (system,) = {c.system for c in provider.calls}
    assert system == build_review_system_prompt("攻略")
    assert provider.calls[0].user.startswith("对局：凯瑞尔·罗姆 | 第 4 名 | 1 个回合")
    rows = (tmp_path / "recordings" / "llm-2026-10-11.jsonl").read_text().splitlines()
    assert json.loads(rows[0])["lane"] == "review"


def test_no_provider_or_ai_switched_off_gives_only_the_rules_review(tmp_path):
    from test_reviewer import play_game

    r, sink, _out = reviewer(tmp_path / "a", None)
    play_game(r)
    r.tick()
    assert [x.ai_state for x in sink.reviews] == ["off"] and not r.ai_enabled
    provider = FakeProvider([answer(1)])
    r, sink, _out = reviewer(tmp_path / "b", provider, cfg=ReviewConfig(ai=False))
    play_game(r)
    r.tick()
    assert [x.ai_state for x in sink.reviews] == ["off"] and provider.calls == []


def test_failed_ai_review_keeps_the_rules_file(tmp_path):
    from test_reviewer import play_game

    r, sink, _out = reviewer(tmp_path, FakeProvider([LLMError("timeout", "AI 超时")]))
    play_game(r)
    r.tick()
    pending, failed = sink.reviews
    assert (failed.ai_state, failed.ai_error, failed.ai) == ("failed", "AI 超时", None)
    assert "**下局重点**：" + pending.focus in failed.path.read_text(encoding="utf-8")


def test_ai_review_that_never_comes_back_times_out(tmp_path):
    from test_reviewer import play_game

    now = [0.0]
    jobs = []
    provider = FakeProvider([answer(1)])
    r, sink, _out = reviewer(tmp_path, provider, jobs=jobs, clock=lambda: now[0])
    play_game(r)
    r.tick()
    now[0] = 76.0
    r.tick()
    assert len(sink.reviews) == 1  # 75 + 2 秒以内还在等
    now[0] = 78.0
    r.tick()
    assert (sink.reviews[-1].ai_state, sink.reviews[-1].ai_error) == ("failed", "AI 超时")
    jobs[0]()  # 晚到的结果丢弃
    r.tick()
    assert len(sink.reviews) == 2


def test_display_prints_pending_ai_and_failure_blocks(tmp_path):
    from lscs.advisor.display import AdviceDisplay
    from lscs.state.models import GameState

    base = review_game(GAME, RULES, card_name, WHEN)
    base = replace(base, shown_path="reviews/x.md", path=tmp_path / "x.md")
    out = []
    display = AdviceDisplay(card_name, out.append, deep_thinking="high")
    display.show_review(GameState(), replace(base, ai_state="pending"))
    assert out[-1] == "  🧠 AI 复盘生成中…（约 40 秒）"
    out.clear()
    ai = AIReview(
        moments=(
            Moment(
                turn=8, title="连续升本没补场面", what="只升本", why="掉了 30 血", next="买一张"
            ),
        ),
        focus="每回合至少买一张随从",
        good="第 11 回合凑出金色蚀脑怪",
    )
    display.show_review(GameState(), replace(base, ai=ai, ai_state="done", ai_elapsed=19.4))
    assert out == [
        "  🧠 AI 复盘（19 秒）",
        "     ① 第 8 回合 连续升本没补场面：只升本",
        "        为什么要紧：掉了 30 血",
        "        下次：买一张",
        "     做得好：第 11 回合凑出金色蚀脑怪",
        "     下局重点：每回合至少买一张随从",
        "     已更新 reviews/x.md",
    ]
    out.clear()
    display.show_review(GameState(), replace(base, ai_state="failed", ai_error="AI 超时"))
    assert out == ["  🧠 AI 复盘失败：超时（文件里是规则版）"]
    out.clear()
    display.show_review(GameState(), base)
    assert not any("AI 复盘" in line for line in out)


def test_overlay_review_lines_follow_the_ai_state():
    from lscs.overlay.model import Line, review_lines

    base = replace(review_game(GAME, RULES, card_name, WHEN), shown_path="reviews/x.md")
    pending = review_lines(replace(base, ai_state="pending"))
    assert pending[-1] == Line("AI 复盘生成中…", "muted")
    assert review_lines(replace(base, ai_state="failed", ai_error="x")) == pending[:-1]
    ai = AIReview(
        moments=(
            Moment(turn=8, title="连续升本没补场面", what="a", why="b", next="c"),
            Moment(turn=11, title="金色蚀脑怪", what="a", why="b", next="c"),
        ),
        focus="每回合至少买一张随从",
    )
    assert review_lines(replace(base, ai=ai, ai_state="done")) == (
        Line("① 第 8 回合 连续升本没补场面"),
        Line("② 第 11 回合 金色蚀脑怪"),
        Line("下局重点：每回合至少买一张随从"),
        Line("已更新 reviews/x.md", "muted", role="comment"),
    )


def test_ai_review_on_a_real_worker_thread(tmp_path):
    import threading
    import time

    from test_reviewer import name, play_game

    from lscs.review.reviewer import GameReviewer

    seen_threads = []

    class ThreadProvider(FakeProvider):
        def complete_json(self, *args, **kwargs):
            seen_threads.append(threading.current_thread().name)
            return super().complete_json(*args, **kwargs)

    sink = Sink()
    r = GameReviewer(
        ReviewConfig(),
        RULES,
        name,
        sink,
        CardDB([]),
        ThreadProvider([answer(1)]),
        LLMConfig(api_key="sk-x-123456789"),
        directory=tmp_path / "reviews",
        now=lambda: WHEN,
    )
    play_game(r)
    deadline = time.monotonic() + 5
    while len(sink.reviews) < 2 and time.monotonic() < deadline:
        r.tick()  # 监听线程（这里是测试线程）取回结果
        time.sleep(0.01)
    assert [x.ai_state for x in sink.reviews] == ["pending", "done"]
    assert seen_threads == ["lscs-review"]


def test_invented_card_check_skips_hero_names_and_loose_short_names():
    from lscs.review.ai import unknown_cards

    def minion(card_id, card_name):
        return {
            "id": card_id,
            "name": card_name,
            "type": "MINION",
            "techLevel": 3,
            "isBattlegroundsPoolMinion": True,
        }

    db = CardDB(
        [
            minion("M1", "泰瑞尔"),
            minion("M2", "龙鹰"),
            minion("M3", "熔岩潜伏者"),
            {"id": "HERO_X", "name": "泰瑞尔之影", "type": "HERO"},
        ]
    )
    rec = record(curve_turns()).model_copy(
        update={"combats": (combat(3, lost=2).model_copy(update={"opponent_hero": "HERO_X"}),)}
    )
    # 对手英雄名里带着卡池随从的名字：不算编造
    assert unknown_cards("第 3 回合打泰瑞尔之影掉了 2 血", rec, db) == []
    # 两个字的卡名挨着别的字时不算（可能只是普通的词），单独出现时才算
    assert unknown_cards("该买龙鹰", rec, db) == []
    assert unknown_cards("该买（龙鹰）", rec, db) == ["龙鹰"]
    assert unknown_cards("该买熔岩潜伏者和泰瑞尔", rec, db) == ["泰瑞尔", "熔岩潜伏者"]


def test_no_review_repair_when_repair_is_switched_off(tmp_path):
    from test_reviewer import name, play_game

    from lscs.review.reviewer import GameReviewer

    provider = FakeProvider([answer(99), answer(1)])
    direct = run_review(replace(call(provider), repair=False), "s", "u", GAME, None, lambda: 0.0)
    assert direct.error.startswith("回答不合格（") and len(provider.calls) == 1
    # 接线：[llm] repair_invalid = false 时复盘也不改正
    provider = FakeProvider([answer(99), answer(1)])
    sink = Sink()
    r = GameReviewer(
        ReviewConfig(),
        RULES,
        name,
        sink,
        CardDB([]),
        provider,
        LLMConfig(api_key="sk-x-123456789", repair_invalid=False),
        directory=tmp_path / "reviews",
        now=lambda: WHEN,
        run_async=lambda job: job(),
    )
    play_game(r)
    r.tick()
    assert sink.reviews[-1].ai_state == "failed" and len(provider.calls) == 1


def test_no_ai_review_after_the_scheduler_switched_ai_off(tmp_path):
    from test_reviewer import name, play_game

    from lscs.llm.notices import AUTH_DISABLED
    from lscs.review.reviewer import GameReviewer

    provider = FakeProvider([answer(1)])
    sink = Sink()
    reason = [None]
    r = GameReviewer(
        ReviewConfig(),
        RULES,
        name,
        sink,
        CardDB([]),
        provider,
        LLMConfig(api_key="sk-x-123456789"),
        directory=tmp_path / "reviews",
        now=lambda: WHEN,
        run_async=lambda job: job(),
        ai_blocked=lambda: reason[0],
    )
    reason[0] = AUTH_DISABLED  # Key 连续 3 次无效
    play_game(r)
    r.tick()
    assert [x.ai_state for x in sink.reviews] == ["off"] and provider.calls == []


def test_fit_cuts_at_punctuation_only_when_enough_text_is_left():
    from lscs.review.ai import fit

    assert (
        fit("升本的回合也要买随从，场上先补满再升本，" + "字" * 30, 22)
        == "升本的回合也要买随从，场上先补满再升本"
    )
    # 标点太靠前：截出来只剩一个字，不如硬截
    cut = fit("好，" + "字" * 40, 22)
    assert len(cut) > 10 and cut.endswith("…")
