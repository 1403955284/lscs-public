import json

from factory import HEROES, hero_select_state, make_state, minion, offered_event, shop_minion
from helpers import FIXTURES

from lscs.advisor.context import build_context
from lscs.cards.db import CardDB
from lscs.config import LLMConfig
from lscs.llm.provider import FakeProvider, LLMError
from lscs.llm.record import Recorder
from lscs.llm.scheduler import HERO_DEBOUNCE, AINotice, AIResult, LLMScheduler
from lscs.state.models import Event, EventType, Phase

CARDS = CardDB.from_json_file(FIXTURES / "cards_mini.json")
SECRET = "sk-secret-0123456789"
GOOD = json.dumps({"actions": [{"type": "buy", "entity_id": 20, "reason": "好"}], "summary": "买"})


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Deferred:
    """把后台任务先存起来，测试里手动执行，模拟乱序完成。"""

    def __init__(self):
        self.jobs = []

    def __call__(self, job):
        self.jobs.append(job)


def scheduler(
    responses, *, clock=None, run_async=None, recorder=None, deep=False, hero_debounce=0.0, **cfg
):
    """deep=False：只有快速一路（大部分测试只关心快速建议的时机）。
    hero_debounce=0：选英雄这一路不等（等待只在专门的测试里看）。"""
    provider = FakeProvider(responses)
    sched = LLMScheduler(
        provider,
        LLMConfig(api_key=SECRET, deep_analysis=deep, **cfg),
        CARDS,
        guide="要点",
        recorder=recorder,
        clock=clock or Clock(),
        run_async=run_async or (lambda job: job()),
        hero_debounce=hero_debounce,
    )
    return sched, provider


def state(shop_ids=(20, 21), turn=3, seq=1, gold=5, board=()):
    shop = [shop_minion(i, "BGS_061", 3, 1, races=["PIRATE"]) for i in shop_ids]
    return make_state(turn=turn, seq=seq, gold=gold, tier=2, upgrade_cost=7, shop=shop, board=board)


def observe(sched, st, *types):
    events = [Event(type=t, turn=st.turn) for t in types]
    sched.observe(st, events, build_context(st, 3))


def kinds(events):
    return [type(e).__name__ for e in events]


def situation(user):
    """局面消息去掉开头的本局卡池。"""
    return user.split("\n\n", 1)[1]


def test_recruit_start_calls_immediately():
    sched, provider = scheduler([GOOD])
    observe(sched, state(), EventType.RECRUIT_STARTED)
    events = sched.tick()
    assert kinds(events) == ["AIStarted", "AIResult"]
    result = events[1]
    assert result.error is None and result.plan.actions[0].entity_id == 20
    assert len(provider.calls) == 1
    system, user, timeout, thinking, max_tokens = provider.calls[0]
    assert "要点" in system and "[#20] 海盗无赖" in user and timeout == 20
    assert (thinking, max_tokens) == ("disabled", 1500)
    assert (sched.calls, sched.input_tokens, sched.output_tokens) == (1, 100, 20)


def test_refresh_waits_for_quiet_shop():
    clock = Clock()
    sched, provider = scheduler([GOOD, GOOD], clock=clock)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)  # 刷新
    clock.now += 0.5
    assert sched.tick() == []
    clock.now += 0.6
    events = sched.tick()
    assert kinds(events) == ["AIStarted", "AIResult"]
    assert "[#30]" in provider.calls[1][1]


def test_rapid_refreshes_call_only_for_the_last_one():
    clock = Clock()
    sched, provider = scheduler([GOOD, GOOD, GOOD], clock=clock)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)
    clock.now += 0.5
    observe(sched, state(shop_ids=(40, 41), seq=3), EventType.SHOP_CHANGED)
    clock.now += 0.6
    assert sched.tick() == []  # 第二次刷新重新计时
    clock.now += 0.5
    sched.tick()
    assert len(provider.calls) == 2 and "[#40]" in provider.calls[1][1]


def test_buying_after_refresh_updates_pending_state():
    clock = Clock()
    sched, provider = scheduler([GOOD, json.dumps({"actions": []})], clock=clock)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)
    observe(sched, state(shop_ids=(31,), seq=3, gold=2), EventType.SHOP_CHANGED)  # 买走了 #30
    clock.now += 1.0
    sched.tick()
    user = provider.calls[1][1]
    assert "[#30]" not in user and "铸币 2" in user


def test_new_request_supersedes_old_one():
    deferred = Deferred()
    sched, _ = scheduler([GOOD, GOOD], run_async=deferred)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)
    sched.tick()
    assert len(deferred.jobs) == 2
    for job in deferred.jobs:
        job()
    results = [e for e in sched.tick() if isinstance(e, AIResult)]
    assert [r.request.request_id for r in results] == [2]


def test_reset_game_drops_result_from_previous_game():
    deferred = Deferred()
    sched, provider = scheduler(["", GOOD], run_async=deferred)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    sched.reset_game()
    deferred.jobs[0]()  # 上一局的请求这时才返回（空内容）
    assert [e for e in sched.tick() if isinstance(e, AIResult)] == []
    assert len(provider.calls) == 1 and sched.calls == 0  # 不重试，也不占新一局的次数


def test_refresh_after_buying_out_the_shop():
    clock = Clock()
    sched, provider = scheduler([GOOD, GOOD], clock=clock)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(), seq=2, gold=1), EventType.SHOP_CHANGED)  # 买空了商店
    observe(sched, state(shop_ids=(30, 31), seq=3, gold=1), EventType.SHOP_CHANGED)  # 刷新
    clock.now += 1.0
    sched.tick()
    assert len(provider.calls) == 2 and "[#30]" in provider.calls[1][1]


def test_budget_notice_only_once():
    sched, provider = scheduler([GOOD], max_calls_per_game=1)
    events = []
    for turn in (3, 4, 5):
        observe(sched, state(turn=turn, seq=turn), EventType.RECRUIT_STARTED)
        events += sched.tick()
    assert kinds(events) == ["AIStarted", "AIResult", "AINotice"]
    assert events[2] == AINotice("本局 AI 预算已用完，之后只显示规则建议")
    assert len(provider.calls) == 1
    sched.reset_game()
    observe(sched, state(turn=1, seq=4), EventType.RECRUIT_STARTED)
    assert kinds(sched.tick()) == ["AIStarted", "AIResult"]


def test_empty_answer_is_retried_once():
    sched, provider = scheduler(["", GOOD])
    observe(sched, state(), EventType.RECRUIT_STARTED)
    result = sched.tick()[-1]
    assert result.plan is not None and len(provider.calls) == 2 and sched.calls == 2

    sched, provider = scheduler(["", ""])
    observe(sched, state(), EventType.RECRUIT_STARTED)
    assert sched.tick()[-1].error == "AI 返回了空内容"
    assert len(provider.calls) == 2


def test_invalid_answers_are_dropped():
    for answer in ["不是json", json.dumps({"actions": [{"type": "buy", "entity_id": 99}]})]:
        sched, _ = scheduler([answer])
        observe(sched, state(), EventType.RECRUIT_STARTED)
        assert sched.tick()[-1].error == "AI 建议无效，已忽略"


def test_provider_errors_are_reported():
    sched, _ = scheduler([LLMError("timeout", "AI 超时")])
    observe(sched, state(), EventType.RECRUIT_STARTED)
    assert sched.tick()[-1].error == "AI 超时"


def test_three_auth_failures_disable_ai():
    auth = LLMError("auth", "API Key 无效")
    sched, provider = scheduler([auth, auth, auth, GOOD])
    events = []
    for turn in (1, 2, 3, 4):
        observe(sched, state(turn=turn, seq=turn), EventType.RECRUIT_STARTED)
        events += sched.tick()
    assert AINotice("API Key 连续 3 次无效，已关闭 AI") in events
    assert sched.disabled_reason == "API Key 连续 3 次无效，已关闭 AI"
    assert len(provider.calls) == 3


def test_auth_failure_counter_resets_after_success():
    auth = LLMError("auth", "API Key 无效")
    sched, _ = scheduler([auth, auth, GOOD, auth, auth])
    for turn in range(1, 6):
        observe(sched, state(turn=turn, seq=turn), EventType.RECRUIT_STARTED)
        sched.tick()
    assert sched.disabled_reason is None


def test_not_recruit_clears_pending_refresh():
    clock = Clock()
    sched, provider = scheduler([GOOD, GOOD], clock=clock)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)
    combat = make_state(phase=Phase.COMBAT, turn=3, seq=3)
    sched.observe(combat, [Event(type=EventType.COMBAT_STARTED, turn=3)], None)
    clock.now += 2
    assert sched.tick() == []
    assert len(provider.calls) == 1


def test_record_never_contains_the_key(tmp_path):
    recorder = Recorder(tmp_path)
    auth = LLMError("auth", "API Key 无效")
    sched, _ = scheduler([GOOD, "坏", auth], recorder=recorder)
    for turn in (1, 2, 3):
        observe(
            sched,
            state(turn=turn, seq=turn, board=[minion(10, "BG21_029", 1, 1)]),
            EventType.RECRUIT_STARTED,
        )
        sched.tick()
    files = list(tmp_path.glob("llm-*.jsonl"))
    text = files[0].read_text(encoding="utf-8")
    assert SECRET not in text
    outcomes = [json.loads(line)["outcome"] for line in text.splitlines()]
    assert outcomes == ["合法", "无效：不是合法的 JSON", "失败：API Key 无效"]


def test_observe_only_queues_so_a_batch_calls_once_for_the_latest_turn():
    # watch 启动时从头读 Power.log：一次轮询里重放了好几个招募开始，只为最后的局面调用一次
    sched, provider = scheduler([GOOD])
    for turn in (3, 4, 5):
        observe(sched, state(turn=turn, seq=turn), EventType.RECRUIT_STARTED)
    assert provider.calls == []
    assert kinds(sched.tick()) == ["AIStarted", "AIResult"]
    assert len(provider.calls) == 1 and situation(provider.calls[0].user).startswith("第 5 回合")
    assert sched.tick() == []


def test_finished_turns_in_a_batch_call_nothing():
    sched, provider = scheduler([GOOD])
    observe(sched, state(), EventType.RECRUIT_STARTED)
    combat = make_state(phase=Phase.COMBAT, turn=3, seq=2)
    sched.observe(combat, [Event(type=EventType.COMBAT_STARTED, turn=3)], None)
    assert sched.tick() == []
    assert provider.calls == [] and sched.calls == 0


def test_recruit_update_without_context_never_becomes_a_request():
    clock = Clock()
    buy_30 = json.dumps({"actions": [{"type": "buy", "entity_id": 30, "reason": "好"}]})
    sched, provider = scheduler([GOOD, buy_30], clock=clock)
    st = state()
    sched.observe(st, [Event(type=EventType.RECRUIT_STARTED, turn=3)], None)
    assert sched.tick() == [] and sched.calls == 0
    observe(sched, st, EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)  # 刷新，等 1 秒
    sched.observe(state(shop_ids=(30, 31), seq=3), [], None)  # 没有 ctx：不覆盖待发的请求
    clock.now += 1.0
    events = sched.tick()
    assert kinds(events) == ["AIStarted", "AIResult"] and events[1].error is None
    assert events[1].request.state.seq == 2 and "[#30]" in provider.calls[1][1]


def test_message_failure_costs_nothing(monkeypatch):
    import lscs.llm.scheduler

    def boom(*_args):
        raise RuntimeError("坏了")

    sched, provider = scheduler([GOOD])
    monkeypatch.setattr(lscs.llm.scheduler, "build_user_message", boom)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    assert sched.tick() == [AINotice("生成 AI 局面时出错，本次跳过")]
    assert (sched.calls, provider.calls) == (0, [])
    assert sched.tick() == []  # 不会每次轮询都重试


def test_refresh_drops_the_answer_still_in_flight():
    clock = Clock()
    deferred = Deferred()
    sched, _ = scheduler([GOOD, GOOD], clock=clock, run_async=deferred)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    assert kinds(sched.tick()) == ["AIStarted"]
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)  # 刷新
    deferred.jobs[0]()  # 刷新前那次请求这时才返回：方案里的牌已经不在商店了
    assert sched.tick() == []
    clock.now += 1.0
    assert kinds(sched.tick()) == ["AIStarted"]
    deferred.jobs[1]()
    results = sched.tick()
    assert [r.request.request_id for r in results] == [2]


def test_request_without_answer_times_out_once():
    clock = Clock()
    deferred = Deferred()
    sched, _ = scheduler([GOOD], clock=clock, run_async=deferred, timeout_seconds=20)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    assert kinds(sched.tick()) == ["AIStarted"]
    clock.now += 22.0
    assert sched.tick() == []  # 还在 timeout_seconds + 2 秒以内
    clock.now += 0.5
    events = sched.tick()
    assert len(events) == 1 and isinstance(events[0], AIResult)
    assert (events[0].error, events[0].elapsed) == ("AI 超时", 22.5)
    clock.now += 10
    assert sched.tick() == []  # 只提示一次
    deferred.jobs[0]()  # 很晚才返回的回答直接丢弃
    assert sched.tick() == []


# ---- 快速 + 深度两路 ----


def answer(entity=20, **extra):
    return json.dumps({"actions": [{"type": "buy", "entity_id": entity, "reason": "好"}], **extra})


def lanes(events):
    return [(type(e).__name__, e.lane) for e in events if not isinstance(e, AINotice)]


def test_recruit_start_sends_fast_and_deep_requests():
    sched, provider = scheduler([GOOD, GOOD], deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    events = sched.tick()
    assert lanes(events) == [
        ("AIStarted", "fast"),
        ("AIResult", "fast"),
        ("AIStarted", "deep"),
        ("AIResult", "deep"),
    ]
    assert all(e.error is None for e in events if isinstance(e, AIResult))
    fast, deep = provider.calls
    assert (fast.thinking, fast.max_tokens, fast.timeout) == ("disabled", 1500, 20)
    assert (deep.thinking, deep.max_tokens, deep.timeout) == ("low", 16000, 75)
    assert fast.system == deep.system and fast.user == deep.user  # 同一个前缀，方便缓存
    assert sched.calls == 2
    assert [e.request_id for e in events if type(e).__name__ == "AIStarted"] == [1, 2]


def test_deep_lane_uses_its_own_settings():
    sched, provider = scheduler(
        [GOOD, GOOD],
        deep=True,
        deep_thinking="high",
        deep_max_output_tokens=9000,
        deep_timeout_seconds=40,
    )
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    assert (provider.calls[1].thinking, provider.calls[1].max_tokens) == ("high", 9000)
    assert provider.calls[1].timeout == 40


def test_deep_analysis_off_sends_only_fast():
    sched, provider = scheduler([GOOD, GOOD], deep=False)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    assert lanes(sched.tick()) == [("AIStarted", "fast"), ("AIResult", "fast")]
    assert len(provider.calls) == 1


def test_refresh_only_calls_fast():
    clock = Clock()
    sched, provider = scheduler([GOOD, GOOD, GOOD], clock=clock, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)  # 刷新
    clock.now += 1.0
    assert lanes(sched.tick()) == [("AIStarted", "fast"), ("AIResult", "fast")]
    assert [c.thinking for c in provider.calls] == ["disabled", "low", "disabled"]


def test_deep_result_after_refresh_is_delivered_and_marked():
    clock = Clock()
    deferred = Deferred()
    sched, _ = scheduler([GOOD, GOOD], clock=clock, run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    assert lanes(sched.tick()) == [("AIStarted", "fast"), ("AIStarted", "deep")]
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)  # 刷新
    deferred.jobs[0]()  # 快速：刷新前的方案作废
    deferred.jobs[1]()  # 深度：操作过期，但分析照样交付
    results = [e for e in sched.tick() if isinstance(e, AIResult)]
    assert [(r.lane, r.expired) for r in results] == [("deep", "refreshed")]
    assert results[0].plan is not None and results[0].error is None


def test_new_turn_drops_the_deep_request():
    deferred = Deferred()
    sched, _ = scheduler([GOOD] * 4, run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)
    sched.tick()
    deferred.jobs[1]()  # 上回合的深度分析这时才返回
    deferred.jobs[0]()
    assert [e for e in sched.tick() if isinstance(e, AIResult)] == []
    deferred.jobs[3]()
    assert [(e.lane, e.request.state.turn) for e in sched.tick()] == [("deep", 4)]


def test_new_turn_drops_deep_request_even_when_no_new_request_starts():
    deferred = Deferred()
    sched, _ = scheduler([GOOD, GOOD], run_async=deferred, deep=True, max_calls_per_game=2)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)
    assert kinds(sched.tick()) == ["AINotice"]  # 预算用完，第 4 回合不再调用
    deferred.jobs[1]()
    assert sched.tick() == []
    assert sched.memory is None  # 作废的结果也不进记忆


def test_lane_timeouts_are_independent():
    clock = Clock()
    deferred = Deferred()
    sched, _ = scheduler([GOOD, GOOD], clock=clock, run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    clock.now += 22.5  # 快速：超过 20 + 2 秒
    events = sched.tick()
    assert [(e.lane, e.error) for e in events] == [("fast", "AI 超时")]
    clock.now += 10
    deferred.jobs[1]()  # 深度分析 32.5 秒后返回，不受快速超时影响
    events = sched.tick()
    assert [(e.lane, e.error, e.expired) for e in events] == [("deep", None, None)]
    deferred.jobs[0]()  # 很晚才返回的快速回答直接丢弃
    assert sched.tick() == []


def test_deep_times_out_on_its_own_deadline():
    clock = Clock()
    deferred = Deferred()
    sched, _ = scheduler([GOOD, GOOD], clock=clock, run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    deferred.jobs[0]()
    assert [e.lane for e in sched.tick()] == ["fast"]
    clock.now += 77.0
    assert sched.tick() == []  # 还在 75 + 2 秒以内
    clock.now += 0.5
    events = sched.tick()
    assert [(e.lane, e.error, e.elapsed) for e in events] == [("deep", "AI 超时", 77.5)]
    assert sched.tick() == []


def test_budget_is_shared_by_both_lanes():
    sched, provider = scheduler([GOOD] * 5, deep=True, max_calls_per_game=3)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)
    events = sched.tick()
    assert kinds(events) == ["AIStarted", "AIResult", "AINotice"]
    assert events[0].lane == "fast"
    assert len(provider.calls) == 3 and sched.calls == 3


def test_deep_empty_answer_is_not_retried():
    sched, provider = scheduler([GOOD, "", GOOD], deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    events = sched.tick()
    assert [(e.lane, e.error) for e in events if isinstance(e, AIResult)] == [
        ("fast", None),
        ("deep", "AI 返回了空内容"),
    ]
    assert len(provider.calls) == 2


def test_deep_invalid_actions_keep_the_analysis():
    bad = answer(99, analysis="畸变核心都在 5 本，提前升本。", look_for=["神明"])
    sched, _ = scheduler([GOOD, bad], deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    deep = [e for e in sched.tick() if isinstance(e, AIResult)][1]
    assert (deep.error, deep.expired) == (None, "invalid")
    assert deep.plan.actions == () and deep.plan.analysis == "畸变核心都在 5 本，提前升本。"
    sched2, _ = scheduler([GOOD, answer(99)], deep=True)  # 没有分析的无效回答照旧丢弃
    observe(sched2, state(), EventType.RECRUIT_STARTED)
    assert [e for e in sched2.tick() if isinstance(e, AIResult)][1].error == "AI 建议无效，已忽略"


def last_line(call):
    return call.user.splitlines()[-1]


def test_memory_follows_valid_results_and_prefers_deep():
    clock = Clock()
    fast3 = answer(20, direction="海盗", look_for=["船长"], analysis="快速判断")
    deep3 = answer(20, direction="畸变", look_for=["神明", "祭品"], analysis="深度判断")
    fast3b = answer(30, direction="野兽", analysis="刷新后的快速判断")
    sched, provider = scheduler([fast3, deep3, fast3b, GOOD, GOOD], clock=clock, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    assert "你之前的判断" not in provider.calls[0].user  # 本局第一次调用没有记忆
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)
    clock.now += 1.0
    sched.tick()
    assert (
        last_line(provider.calls[2])
        == "本回合深度分析：方向 畸变；要找：神明、祭品；分析：深度判断"
    )
    observe(sched, state(turn=4, seq=3), EventType.RECRUIT_STARTED)
    sched.tick()  # 本回合快速结果（野兽）没有覆盖深度分析
    assert last_line(provider.calls[3]) == (
        "你之前的判断（第 3 回合）：方向 畸变；要找：神明、祭品；分析：深度判断"
    )
    assert provider.calls[3].user == provider.calls[4].user
    sched.reset_game()
    assert sched.memory is None
    observe(sched, state(turn=1, seq=4), EventType.RECRUIT_STARTED)
    sched.tick()
    assert "你之前的判断" not in provider.calls[5].user


def test_memory_takes_a_newer_fast_result_and_skips_invalid_or_empty_ones():
    fast3 = answer(20, direction="海盗", analysis="第 3 回合")
    fast4 = answer(20, direction="野兽", look_for=["狼王"])
    bad_deep4 = answer(99, direction="元素", analysis="不合法的深度分析")
    sched, provider = scheduler([fast3, "", fast4, bad_deep4, GOOD, GOOD], deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)
    sched.tick()
    assert (
        last_line(provider.calls[2])
        == "你之前的判断（第 3 回合）：方向 海盗；要找：无；分析：第 3 回合"
    )
    observe(sched, state(turn=5, seq=3), EventType.RECRUIT_STARTED)
    sched.tick()  # GOOD 没有方向、要找和分析：不覆盖记忆
    assert last_line(provider.calls[4]) == "你之前的判断（第 4 回合）：方向 野兽；要找：狼王"


def test_deep_result_after_refresh_still_updates_memory():
    clock = Clock()
    deferred = Deferred()
    deep3 = answer(20, direction="畸变", analysis="深度判断")
    sched, provider = scheduler([GOOD, deep3, GOOD], clock=clock, run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)  # 刷新
    deferred.jobs[0]()
    deferred.jobs[1]()
    sched.tick()
    clock.now += 1.0
    sched.tick()
    deferred.jobs[2]()
    assert last_line(provider.calls[2]) == "本回合深度分析：方向 畸变；要找：无；分析：深度判断"


def test_record_has_the_lane(tmp_path):
    recorder = Recorder(tmp_path)
    sched, _ = scheduler([GOOD, GOOD], recorder=recorder, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    text = next(tmp_path.glob("llm-*.jsonl")).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    assert [(r["lane"], r["thinking"], r["request_id"]) for r in rows] == [
        ("fast", "disabled", 1),
        ("deep", "low", 2),
    ]
    assert SECRET not in text


def test_catch_up_starts_at_most_one_request_per_lane():
    deferred = Deferred()
    sched, provider = scheduler([GOOD, GOOD], run_async=deferred, deep=True)
    for turn in (3, 4, 5):
        observe(sched, state(turn=turn, seq=turn), EventType.RECRUIT_STARTED)
    assert lanes(sched.tick()) == [("AIStarted", "fast"), ("AIStarted", "deep")]
    for job in deferred.jobs:
        job()
    assert len(provider.calls) == 2
    assert all(situation(c.user).startswith("第 5 回合") for c in provider.calls)


def test_refresh_before_the_deep_request_starts_updates_its_state():
    sched, provider = scheduler([answer(30)], deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)  # 同一批里刷新了
    events = sched.tick()
    assert lanes(events) == [("AIStarted", "deep"), ("AIResult", "deep")]
    assert "[#30]" in provider.calls[0].user and "[#20]" not in provider.calls[0].user
    assert (events[1].error, events[1].expired) == (None, None)  # 用的就是刷新后的商店


# ---- 修复：深度分析晚到、记忆先于新请求更新 ----


def test_late_deep_result_from_the_previous_turn_is_remembered_but_not_shown():
    deferred = Deferred()
    deep3 = answer(20, direction="畸变", analysis="第 3 回合深度")
    sched, _ = scheduler([GOOD, deep3], run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    deferred.jobs[0]()
    sched.tick()
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)
    sched.tick()
    deferred.jobs[1]()  # 第 3 回合的深度分析在第 4 回合开始后才返回
    assert [e for e in sched.tick() if isinstance(e, AIResult)] == []  # 不显示
    memory = sched.memory
    assert (memory.turn, memory.deep, memory.direction) == (3, True, "畸变")


def test_older_or_fast_or_previous_game_results_are_not_remembered_late():
    deferred = Deferred()
    fast3 = answer(20, direction="海盗")
    deep3 = answer(20, direction="畸变")
    sched, _ = scheduler([fast3, deep3], run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)
    observe(sched, state(turn=5, seq=3), EventType.RECRUIT_STARTED)
    deferred.jobs[0]()  # 快速建议晚到：不记
    deferred.jobs[1]()  # 两回合以前的深度分析：不记
    sched.tick()
    assert sched.memory is None

    deferred = Deferred()
    sched, _ = scheduler([deep3, deep3], run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    sched.reset_game()
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)  # 新的一局
    deferred.jobs[1]()  # 上一局的深度分析
    sched.tick()
    assert sched.memory is None


def test_results_are_remembered_before_new_requests_start():
    clock = Clock()
    deferred = Deferred()
    deep3 = answer(20, direction="畸变", analysis="深度判断")
    sched, provider = scheduler([deep3, GOOD], clock=clock, run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)  # 刷新
    clock.now += 1.0
    deferred.jobs[1]()  # 深度分析在同一次轮询里返回，刷新后的快速请求也在这次发出
    sched.tick()
    deferred.jobs[2]()
    assert last_line(provider.calls[-1]) == "本回合深度分析：方向 畸变；要找：无；分析：深度判断"


def test_reset_game_drops_the_deep_request_in_flight():
    deferred = Deferred()
    sched, _ = scheduler([answer(20, direction="畸变")], run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    sched.reset_game()
    deferred.jobs[1]()
    assert sched.tick() == []
    assert sched.memory is None


def test_deep_result_arriving_in_combat_is_delivered_and_remembered():
    deferred = Deferred()
    sched, _ = scheduler([answer(20, direction="畸变")], run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    combat = make_state(phase=Phase.COMBAT, turn=3, seq=2)
    sched.observe(combat, [Event(type=EventType.COMBAT_STARTED, turn=3)], None)
    deferred.jobs[1]()
    assert [(e.lane, e.error) for e in sched.tick()] == [("deep", None)]
    assert (sched.memory.turn, sched.memory.deep) == (3, True)


def test_late_deep_result_never_replaces_a_newer_deep_one():
    deferred = Deferred()
    deep3 = answer(20, direction="畸变")
    fast4 = answer(20, direction="海盗")
    deep4 = answer(20, direction="野兽")
    sched, _ = scheduler([fast4, deep4, deep3], run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()  # 第 3 回合：jobs 0、1
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)
    sched.tick()  # 第 4 回合：jobs 2、3
    deferred.jobs[2]()  # 第 4 回合的快速建议先回来
    sched.tick()
    assert sched.memory.direction == "海盗"
    deferred.jobs[3]()
    sched.tick()
    assert (sched.memory.turn, sched.memory.direction) == (4, "野兽")
    deferred.jobs[1]()  # 第 3 回合的深度分析最后才回来：不覆盖第 4 回合的深度分析
    sched.tick()
    assert (sched.memory.turn, sched.memory.direction) == (4, "野兽")


def test_late_deep_result_replaces_a_newer_fast_judgement():
    deferred = Deferred()
    fast4 = answer(20, direction="海盗")
    deep3 = answer(20, direction="畸变")
    sched, _ = scheduler([fast4, deep3], run_async=deferred, deep=True)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(turn=4, seq=2), EventType.RECRUIT_STARTED)
    sched.tick()
    deferred.jobs[2]()
    sched.tick()
    deferred.jobs[1]()  # 按返回的先后记最近的一次；深度分析比快速建议更有参考价值
    sched.tick()
    assert (sched.memory.turn, sched.memory.deep, sched.memory.direction) == (3, True, "畸变")


# ---- 选英雄（第三路 hero）----

HERO = json.dumps(
    {"pick": 89, "reason": "护甲最少", "heroes": [{"entity_id": 90, "comment": "前期慢"}]},
    ensure_ascii=False,
)
REROLLED = ((89, "BG21_HERO_000", 10, 73941), (95, "BG22_HERO_003", 12, 80248))


def offer(sched, st, changed=0):
    sched.observe(st, [offered_event(st, changed)], None)


def chosen(seq=2):
    st = hero_select_state(seq=seq, chosen="BG21_HERO_000")
    return st, [Event(type=EventType.HERO_CHOSEN, turn=0, data={"hero": "BG21_HERO_000"})]


def test_heroes_offered_calls_with_the_fast_settings():
    sched, provider = scheduler([HERO], thinking="low", max_output_tokens=900)
    st = hero_select_state()
    offer(sched, st)
    events = sched.tick()
    assert lanes(events) == [("AIStarted", "hero"), ("AIResult", "hero")]
    result = events[1]
    assert (result.error, result.plan) == (None, None)
    assert (result.hero.source, result.hero.pick) == ("llm", 89)
    assert result.hero.signature == result.request.signature == st.hero_select.signature()
    assert result.request.ctx is None and result.request.state is st
    system, user, timeout, thinking, max_tokens = provider.calls[0]
    assert system.startswith(
        "你是《炉石传说》酒馆战棋的高分教练，正在帮一位 6000 分以下的新手在开局"
    )
    assert "【攻略要点】\n要点" in system and "本月还没有可靠的英雄强度资料" in system
    assert (
        user.startswith("选英雄（4 选 1）\n本局种族：未知")
        and "[#89] BG21_HERO_000 | 护甲 10" in user
    )
    assert (timeout, thinking, max_tokens) == (20, "low", 900)
    assert sched.calls == 1


def test_hero_lane_waits_so_a_half_read_shuffle_calls_once():
    # 开局洗牌时 watch 可能读到半组英雄，0.2 秒后又是完整的一组：等 0.5 秒，只为后一组调用一次
    clock = Clock()
    provider = FakeProvider([HERO, HERO])
    sched = LLMScheduler(
        provider,
        LLMConfig(api_key=SECRET, deep_analysis=False),
        CARDS,
        guide="要点",
        clock=clock,
        run_async=lambda job: job(),
    )
    assert HERO_DEBOUNCE == 0.5
    offer(sched, hero_select_state(HEROES[:3]))
    assert sched.tick() == []
    clock.now += 0.2
    full = hero_select_state(seq=2)
    offer(sched, full, changed=1)
    clock.now += 0.4  # 离第一组 0.6 秒，离第二组 0.4 秒：重新计时，还在等
    assert sched.tick() == []
    clock.now += 0.2
    events = sched.tick()
    assert lanes(events) == [("AIStarted", "hero"), ("AIResult", "hero")]
    assert len(provider.calls) == 1
    assert events[1].request.signature == full.hero_select.signature()
    # 被替换的那组还没发出去：不算重新随机
    assert provider.calls[0].user.startswith("选英雄（4 选 1）\n")
    clock.now += 5
    assert sched.tick() == [] and len(provider.calls) == 1


def test_hero_lane_waiting_ends_if_the_hero_is_chosen_meanwhile():
    clock = Clock()
    sched, provider = scheduler([HERO], clock=clock, hero_debounce=0.5)
    offer(sched, hero_select_state())
    sched.observe(*chosen(), None)  # 0.5 秒内就选好了
    clock.now += 1
    assert sched.tick() == [] and provider.calls == []


def test_heroes_note_goes_into_the_hero_prompt_only():
    provider = FakeProvider([HERO, GOOD])
    sched = LLMScheduler(
        provider,
        LLMConfig(api_key=SECRET, deep_analysis=False),
        CARDS,
        guide="要点",
        heroes_note="2026-10-20 英雄榜",
        run_async=lambda job: job(),
        hero_debounce=0.0,
    )
    offer(sched, hero_select_state())
    sched.tick()
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    hero_system, recruit_system = provider.calls[0].system, provider.calls[1].system
    assert hero_system.endswith("【本月英雄强度资料】\n2026-10-20 英雄榜")
    assert "英雄榜" not in recruit_system


def test_hero_advice_off_never_calls():
    sched, provider = scheduler([HERO], hero_advice=False)
    offer(sched, hero_select_state())
    assert sched.tick() == [] and provider.calls == []


def test_other_updates_while_choosing_do_not_call_again():
    sched, provider = scheduler([HERO, HERO])
    st = hero_select_state()
    offer(sched, st)
    sched.tick()
    sched.observe(st.model_copy(update={"seq": 2}), [], None)  # 其他玩家的英雄陆续出现
    assert sched.tick() == [] and len(provider.calls) == 1


def test_result_after_the_choice_is_dropped():
    deferred = Deferred()
    sched, _ = scheduler([HERO], run_async=deferred)
    offer(sched, hero_select_state())
    assert lanes(sched.tick()) == [("AIStarted", "hero")]
    sched.observe(*chosen(), None)  # 选完了还要等 20 秒才到第 1 回合，仍是选英雄阶段
    deferred.jobs[0]()
    assert sched.tick() == []


def test_reroll_drops_the_old_request_and_asks_again():
    deferred = Deferred()
    sched, provider = scheduler([HERO, HERO], run_async=deferred)
    offer(sched, hero_select_state())
    sched.tick()
    second = hero_select_state(REROLLED, seq=2)
    offer(sched, second, changed=1)
    assert lanes(sched.tick()) == [("AIStarted", "hero")]
    for job in deferred.jobs:
        job()
    results = [e for e in sched.tick() if isinstance(e, AIResult)]
    assert [r.request.request_id for r in results] == [2]
    assert results[0].hero.signature == second.hero_select.signature()
    assert provider.calls[1].user.startswith("选英雄（2 选 1，已重新随机）")


def test_hero_answers_are_checked_and_empty_ones_retried():
    sched, provider = scheduler(["", json.dumps({"pick": 93, "reason": "不在备选里"})])
    offer(sched, hero_select_state())
    result = sched.tick()[-1]
    assert (result.lane, result.error, result.hero) == ("hero", "AI 建议无效，已忽略", None)
    assert len(provider.calls) == 2 and sched.calls == 2


def test_budget_is_shared_with_the_hero_lane():
    sched, provider = scheduler([HERO, GOOD], max_calls_per_game=1)
    offer(sched, hero_select_state())
    sched.tick()
    observe(sched, state(turn=1), EventType.RECRUIT_STARTED)
    assert sched.tick() == [AINotice("本局 AI 预算已用完，之后只显示规则建议")]
    assert len(provider.calls) == 1 and sched.calls == 1


def test_hero_request_times_out_on_its_own():
    clock, deferred = Clock(), Deferred()
    sched, _ = scheduler([HERO], clock=clock, run_async=deferred, timeout_seconds=10)
    offer(sched, hero_select_state())
    sched.tick()
    clock.now += 12.5
    events = sched.tick()
    assert [(e.lane, e.error) for e in events] == [("hero", "AI 超时")]
    deferred.jobs[0]()
    assert sched.tick() == []  # 超时之后才回来的结果不再显示


def test_catch_up_through_the_choice_calls_no_hero():
    # watch 启动时一次读完：备选 → 选择 → 第 1 回合招募，最后才 tick
    deferred = Deferred()
    sched, _ = scheduler([], run_async=deferred, deep=True)
    offer(sched, hero_select_state())
    sched.observe(*chosen(), None)
    observe(sched, state(turn=1, seq=3), EventType.RECRUIT_STARTED)
    assert lanes(sched.tick()) == [("AIStarted", "fast"), ("AIStarted", "deep")]


def test_catch_up_ending_while_choosing_calls_the_hero_lane_once():
    deferred = Deferred()
    sched, _ = scheduler([], run_async=deferred, deep=True)
    st = hero_select_state()
    offer(sched, st)
    sched.observe(st.model_copy(update={"seq": 2}), [], None)
    assert lanes(sched.tick()) == [("AIStarted", "hero")]
    assert len(deferred.jobs) == 1


def test_reset_game_drops_the_hero_request():
    sched, provider = scheduler([HERO])
    offer(sched, hero_select_state())
    sched.reset_game()
    assert sched.tick() == [] and provider.calls == []


def test_record_has_the_hero_lane(tmp_path):
    recorder = Recorder(tmp_path)
    sched, _ = scheduler([HERO, GOOD], recorder=recorder)
    offer(sched, hero_select_state())
    sched.tick()
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    text = next(tmp_path.glob("llm-*.jsonl")).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    assert [(r["lane"], r["turn"], r["thinking"], r["outcome"]) for r in rows] == [
        ("hero", 0, "disabled", "合法"),
        ("fast", 3, "disabled", "合法"),
    ]
    assert rows[0]["system_sha1"] != rows[1]["system_sha1"]  # 选英雄有自己的系统提示
    assert rows[0]["user"].startswith("选英雄")
    assert SECRET not in text


# ---- 选项（第四路 choice） ----

PICK = json.dumps(
    {"pick": 6933, "reason": "32/32 直接成主力", "confidence": 0.8}, ensure_ascii=False
)


def offer_choice(sched, st=None):
    from factory import choice_event, choice_state

    st = st or choice_state()
    sched.observe_choice(st, [choice_event(st)])
    return st


def resolve_choice(sched, choice_id=12, seq=5, picked=6933, turn=9):
    from factory import resolved_event

    done = make_state(turn=turn, seq=seq)
    sched.observe_choice(done, [resolved_event(choice_id, turn=turn, picked=picked)])
    return done


def test_choice_offered_calls_immediately_with_the_fast_settings(tmp_path):
    recorder = Recorder(tmp_path)
    sched, provider = scheduler([PICK], recorder=recorder, timeout_seconds=15)
    offer_choice(sched)
    events = sched.tick()  # 不等：三连奖励几秒钟内就会选掉
    assert kinds(events) == ["AIStarted", "AIResult"]
    assert events[0].lane == "choice"
    result = events[1]
    assert (result.error, result.plan, result.request.choice_id) == (None, None, 12)
    assert (result.choice.pick, result.choice.reason, result.choice.choice_id) == (
        6933,
        "32/32 直接成主力",
        12,
    )
    system, user, timeout, thinking, max_tokens = provider.calls[0]
    assert system.startswith(
        "你是《炉石传说》酒馆战棋的高分教练，正在帮一位 6000 分以下的新手在游戏弹出"
    )
    assert system.endswith("【攻略要点】\n要点")
    assert user.startswith("选择：黑暗之赐（3 选 1）\n第 9 回合（招募）")
    assert (timeout, thinking, max_tokens) == (15, "disabled", 1500)
    record = json.loads(next(tmp_path.glob("llm-*.jsonl")).read_text(encoding="utf-8"))
    assert (record["lane"], record["outcome"], record["turn"]) == ("choice", "合法", 9)
    from lscs.llm.scheduler import _sha1

    assert record["system_sha1"] == _sha1(system) != sched._system_hash


def test_choice_advice_can_be_switched_off():
    sched, provider = scheduler([PICK], choice_advice=False)
    offer_choice(sched)
    assert sched.tick() == [] and provider.calls == [] and sched.calls == 0


def test_result_after_the_choice_was_made_is_dropped():
    deferred = Deferred()
    sched, _ = scheduler([PICK], run_async=deferred)
    offer_choice(sched)
    assert kinds(sched.tick()) == ["AIStarted"]
    resolve_choice(sched)  # 玩家三秒就选完了
    deferred.jobs[0]()
    assert sched.tick() == []


def test_new_group_replaces_the_old_request():
    from factory import choice_option, choice_state, make_choice

    deferred = Deferred()
    other = json.dumps({"pick": 81, "reason": "新的一组"}, ensure_ascii=False)
    sched, provider = scheduler([PICK, other], run_async=deferred)
    offer_choice(sched)
    sched.tick()
    second = make_choice(
        [choice_option(80, "BGS_061", 0, 3, 1), choice_option(81, "BG21_029", 1, 1, 1)],
        choice_id=13,
        kind="discover",
        source="BG23_016",
    )
    offer_choice(sched, choice_state(second, seq=2))
    assert kinds(sched.tick()) == ["AIStarted"]
    for job in deferred.jobs:
        job()
    results = [e for e in sched.tick() if isinstance(e, AIResult)]
    assert [(r.request.choice_id, r.choice.pick) for r in results] == [(13, 81)]
    assert "[#80] 左 1 海盗无赖" in provider.calls[1].user


def test_combat_or_a_new_game_drops_the_choice_request():
    deferred = Deferred()
    sched, _ = scheduler([PICK, PICK], run_async=deferred)
    offer_choice(sched)
    sched.tick()
    combat = make_state(phase=Phase.COMBAT, turn=9, seq=3)
    sched.observe(combat, [Event(type=EventType.COMBAT_STARTED, turn=9)], None)
    deferred.jobs[0]()
    assert sched.tick() == []
    offer_choice(sched)  # 还没发出就进了新的一局
    sched.reset_game()
    assert sched.tick() == [] and len(deferred.jobs) == 1


def test_choice_calls_share_the_budget():
    sched, provider = scheduler([GOOD, PICK], max_calls_per_game=1)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    offer_choice(sched)
    assert sched.tick() == [AINotice("本局 AI 预算已用完，之后只显示规则建议")]
    assert len(provider.calls) == 1


def test_choice_request_times_out_on_its_own():
    clock = Clock()
    deferred = Deferred()
    sched, _ = scheduler([GOOD, PICK], clock=clock, run_async=deferred, timeout_seconds=10)
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    clock.now += 5
    offer_choice(sched)
    sched.tick()
    clock.now += 7.5  # 快速请求已经等了 12.5 秒，选项请求只等了 7.5 秒
    events = sched.tick()
    assert [(e.lane, e.error) for e in events] == [("fast", "AI 超时")]
    clock.now += 5
    assert [(e.lane, e.error) for e in sched.tick()] == [("choice", "AI 超时")]


def test_catch_up_calls_only_for_a_choice_still_open():
    from factory import choice_state

    sched, provider = scheduler([PICK])
    offer_choice(sched)
    resolve_choice(sched)
    observe(sched, state(turn=10, seq=6), EventType.RECRUIT_STARTED)  # 后面还有招募
    sched.observe(make_state(phase=Phase.COMBAT, turn=10, seq=7), [], None)
    assert sched.tick() == [] and provider.calls == []  # 读完时没有还在选的：一次都不调用
    offer_choice(sched, choice_state(seq=8, turn=11))
    resolve_choice(sched, seq=9, turn=11)
    offer_choice(sched, choice_state(seq=10, turn=12))  # 读完时还在选
    assert kinds(sched.tick()) == ["AIStarted", "AIResult"]
    assert len(provider.calls) == 1 and "第 12 回合" in provider.calls[0].user


def test_choice_results_do_not_change_the_memory():
    answer = json.dumps(
        {"actions": [{"type": "buy", "entity_id": 20}], "direction": "海盗", "look_for": ["船长"]},
        ensure_ascii=False,
    )
    sched, provider = scheduler([answer, PICK])
    observe(sched, state(), EventType.RECRUIT_STARTED)
    sched.tick()
    memory = sched.memory
    offer_choice(sched)
    sched.tick()
    assert sched.memory == memory
    assert provider.calls[1].user.endswith("你之前的判断（第 3 回合）：方向 海盗；要找：船长")


def test_invalid_choice_answer_and_message_errors(monkeypatch):
    import lscs.llm.scheduler

    sched, _ = scheduler([json.dumps({"pick": 1})])
    offer_choice(sched)
    assert sched.tick()[-1].error == "AI 建议无效，已忽略"

    def boom(*_args):
        raise RuntimeError("坏了")

    monkeypatch.setattr(lscs.llm.scheduler, "build_choice_message", boom)
    sched, provider = scheduler([PICK])
    offer_choice(sched)
    assert sched.tick() == [AINotice("生成 AI 局面时出错，本次跳过", "choice")]
    assert (sched.calls, provider.calls) == (0, [])
