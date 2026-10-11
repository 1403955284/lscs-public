import json

from factory import make_state, minion, shop_minion
from helpers import FIXTURES

from lscs.advisor.context import build_context
from lscs.cards.db import CardDB
from lscs.config import LLMConfig
from lscs.llm.provider import FakeProvider, LLMError
from lscs.llm.record import Recorder
from lscs.llm.scheduler import AINotice, AIResult, LLMScheduler
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


def scheduler(responses, *, clock=None, run_async=None, recorder=None, deep=False, **cfg):
    """deep=False：只有快速一路（大部分测试只关心快速建议的时机）。"""
    provider = FakeProvider(responses)
    sched = LLMScheduler(
        provider,
        LLMConfig(api_key=SECRET, deep_analysis=deep, **cfg),
        CARDS,
        guide="要点",
        recorder=recorder,
        clock=clock or Clock(),
        run_async=run_async or (lambda job: job()),
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
