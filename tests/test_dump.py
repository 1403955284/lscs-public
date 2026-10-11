from lscs.dump import describe
from lscs.state.models import EventType


def lines_for(updates, verbose=False, name=lambda c: c):
    out = []
    for state, events in updates:
        out.extend(describe(state, events, name, verbose=verbose))
    return out


def test_key_events_timeline(run_log, mini_lines):
    assert lines_for(run_log(mini_lines)) == [
        "[T 0] 对局开始",
        "[T 0] 选择英雄 TB_BaconShop_HERO_49",
        "[T 1] 招募开始 | 铸币 3 | 酒馆 1 本 | 升本 5 | 刷新 1 | 血量 30+0"
        " | 种族 DEMON、ELEMENTAL、PIRATE（推断中）",
        "      商店: BG21_029 1/1、BGS_061 3/1、BGS_116 1/4",
        "      场面: -",
        "      手牌: -",
        "[T 1] 战斗开始 对手 3 TB_BaconShop_HERO_75",
        "[T 1] 战斗结束 我方血量 27 | 对手阵容: BG21_029_G(金) 2/2、BGS_116 1/4",
        "[T 2] 招募开始 | 铸币 4 | 酒馆 1 本 | 升本 4 | 刷新 1 | 血量 27+0"
        " | 种族 DEMON、ELEMENTAL、NAGA、PIRATE（推断中）",
        "      商店: BG23_009 2/5、BG28_500",
        "      场面: BGS_061 3/1",
        "      手牌: -",
        "[T 2] 对局结束 名次 5",
    ]


def test_verbose_includes_diff_events(run_log, mini_lines):
    text = "\n".join(lines_for(run_log(mini_lines), verbose=True))
    assert EventType.SHOP_CHANGED.value in text


def test_names_are_used(run_log, mini_lines):
    names = {"BG21_029": "粗鄙小鬼"}
    text = "\n".join(lines_for(run_log(mini_lines), name=lambda c: names.get(c, c)))
    assert "粗鄙小鬼 1/1" in text


def test_frozen_marker(run_log, mini_lines):
    updates = run_log(mini_lines)
    frozen_state = next(s for s, _ in updates if s.shop and all(i.frozen for i in s.shop))
    from lscs.state.models import Event

    lines = describe(frozen_state, [Event(type=EventType.RECRUIT_STARTED, turn=1)])
    assert lines[1] == "      商店: BG21_029 ❄ 1/1、BGS_116 ❄ 1/4"


def test_uncertain_warning(run_log, mini_lines):
    state, events = run_log(mini_lines)[0]
    lines = describe(state.model_copy(update={"uncertain": True}), events)
    assert lines[-1].endswith("状态可能不准")
