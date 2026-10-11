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


def scheduler(responses, *, clock=None, run_async=None, recorder=None, **cfg):
    provider = FakeProvider(responses)
    sched = LLMScheduler(
        provider,
        LLMConfig(api_key=SECRET, **cfg),
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
    sched.observe(st, events, build_context(st, 3), [])


def kinds(events):
    return [type(e).__name__ for e in events]


def test_recruit_start_calls_immediately():
    sched, provider = scheduler([GOOD])
    observe(sched, state(), EventType.RECRUIT_STARTED)
    events = sched.tick()
    assert kinds(events) == ["AIStarted", "AIResult"]
    result = events[1]
    assert result.error is None and result.plan.actions[0].entity_id == 20
    assert len(provider.calls) == 1
    system, user, timeout = provider.calls[0]
    assert "要点" in system and "[#20] 海盗无赖" in user and timeout == 20
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
    sched.observe(combat, [Event(type=EventType.COMBAT_STARTED, turn=3)], None, [])
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
    assert len(provider.calls) == 1 and provider.calls[0][1].startswith("第 5 回合")
    assert sched.tick() == []


def test_finished_turns_in_a_batch_call_nothing():
    sched, provider = scheduler([GOOD])
    observe(sched, state(), EventType.RECRUIT_STARTED)
    combat = make_state(phase=Phase.COMBAT, turn=3, seq=2)
    sched.observe(combat, [Event(type=EventType.COMBAT_STARTED, turn=3)], None, [])
    assert sched.tick() == []
    assert provider.calls == [] and sched.calls == 0


def test_recruit_update_without_context_never_becomes_a_request():
    clock = Clock()
    buy_30 = json.dumps({"actions": [{"type": "buy", "entity_id": 30, "reason": "好"}]})
    sched, provider = scheduler([GOOD, buy_30], clock=clock)
    st = state()
    sched.observe(st, [Event(type=EventType.RECRUIT_STARTED, turn=3)], None, [])
    assert sched.tick() == [] and sched.calls == 0
    observe(sched, st, EventType.RECRUIT_STARTED)
    sched.tick()
    observe(sched, state(shop_ids=(30, 31), seq=2), EventType.SHOP_CHANGED)  # 刷新，等 1 秒
    sched.observe(state(shop_ids=(30, 31), seq=3), [], None, [])  # 没有 ctx：不覆盖待发的请求
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
