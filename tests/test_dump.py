from lscs.dump import describe
from lscs.state.models import EventType


def lines_for(updates, verbose=False, name=lambda c: c, combat_result=True):
    out = []
    for state, events in updates:
        out.extend(describe(state, events, name, verbose=verbose, combat_result=combat_result))
    return out


def test_key_events_timeline(run_log, mini_lines):
    assert lines_for(run_log(mini_lines)) == [
        "[T 0] 对局开始",
        "[T 0] 备选英雄 TB_BaconShop_HERO_49（护甲 未知）",  # 这份合成日志的英雄没有护甲标签
        "[T 0] 选择英雄 TB_BaconShop_HERO_49",
        "[T 1] 招募开始 | 铸币 3 | 酒馆 1 本 | 升本 5 | 刷新 1 | 血量 30+0"
        " | 种族 DEMON、ELEMENTAL、PIRATE（推断中）",
        "      商店: BG21_029 1/1、BGS_061 3/1、BGS_116 1/4",
        "      场面: -",
        "      手牌: -",
        "[T 1] 战斗开始 对手 3 TB_BaconShop_HERO_75",
        "[T 1] 战斗结束 我方血量 27 | 对手阵容: BG21_029_G(金) 2/2、BGS_116 1/4",
        "[T 1] ⚔ 本场已定：输 · 你掉 3 血（剩 27）",
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


def test_heroes_offered_and_rerolled(run_log):
    from helpers import HERO_LOG

    from lscs.replay import read_log_lines

    names = {"BG21_HERO_000": "凯瑞尔·罗姆", "BG22_HERO_003": "范达尔·雷矛"}
    lines = lines_for(run_log(read_log_lines(HERO_LOG)), name=lambda c: names.get(c, c))
    assert lines[:4] == [
        "[T 0] 对局开始",
        "[T 0] 备选英雄 凯瑞尔·罗姆（护甲 10）、BG28_HERO_800（护甲 18）、BG33_HERO_001（护甲 19）"
        "、TB_BaconShop_HERO_91（护甲 17）",
        "[T 0] 重新随机：备选英雄 凯瑞尔·罗姆（护甲 10）、范达尔·雷矛（护甲 12）"
        "、BG33_HERO_001（护甲 19）、TB_BaconShop_HERO_91（护甲 17）",
        "[T 0] 选择英雄 凯瑞尔·罗姆",
    ]


def test_choice_lines(run_log):
    from helpers import FIXTURES

    from lscs.replay import read_log_lines

    names = {
        "BG21_HERO_000p": "定罪",
        "BG21_HERO_000p_t1": "结阵姿态",
        "BG21_HERO_000_Buddyt": "进攻姿态",
        "BG21_HERO_000_Buddyt2": "防御姿态",
        "TB_BaconShop_Triples_01": "三连奖励",
        "BG23_016": "海礁探险者",
        "BG23_009": "熔岩潜伏者",
        "BGS_116": "刷新畸体",
        "BGS_061": "海盗无赖",
        "BG21_029": "粗鄙小鬼",
        "BG36_MidGameEffect_010": "酒馆战棋黑暗之赐[DNT]",
        "BG36_MidGameEffect_000t13": "鹰身人之爪",
    }
    updates = run_log(read_log_lines(FIXTURES / "power" / "choice.log"))
    lines = [line for line in lines_for(updates, name=lambda c: names.get(c, c)) if "选" in line]
    assert lines == [
        "[T 1] 选择 定罪（3 选 1）：结阵姿态、进攻姿态、防御姿态",
        "[T 1] 选择 三连奖励（3 选 1）：熔岩潜伏者 2/5、刷新畸体 1/4、海盗无赖 3/1",
        "[T 1] 选择 海礁探险者（3 选 1）：粗鄙小鬼 1/1、海盗无赖 3/1、刷新畸体 1/4",
        # 黑暗之赐：标题不用卡名；赐福写在随从后面
        "[T 1] 选择 黑暗之赐（2 选 1）：粗鄙小鬼 1/1〔鹰身人之爪〕、海盗无赖 3/11〔BG36_MidGameEffect_000t2e2〕",
        "[T 1] 选择 海礁探险者（2 选 1）：粗鄙小鬼 1/1、海盗无赖 3/1",
    ]
    verbose = [
        line for line in lines_for(updates, verbose=True) if "选择结束" in line or "已选" in line
    ]
    assert verbose == [
        "[T 1] 已选 BG21_HERO_000_Buddyt",
        "[T 1] 选择结束",  # 没选完就换了一组
        "[T 1] 选择结束",  # 没有 SendChoices，选项离开了 SETASIDE
        "[T 1] 已选 BGS_061",
    ]


# ---- 战斗结果（提前显示战斗结果设计文档第 5.1 节）----

HEROES = {"H_ROKARA": "洛卡拉", "H_SAURFANG": "萨鲁法尔大王", "H_KELTHUZAD": "克尔苏加德"}


def combat_result(**overrides):
    from lscs.state.models import CombatResult

    values = dict(
        turn=3,
        opponent_id=2,
        opponent_hero="H_ROKARA",
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


def result_line(result, name=lambda c: HEROES.get(c, c), **kwargs):
    from factory import make_state

    from lscs.state.models import Event, Phase

    state = make_state(turn=result.turn, phase=Phase.COMBAT).model_copy(
        update={"last_combat": result}
    )
    event = Event(type=EventType.COMBAT_RESULT, turn=result.turn, data={"outcome": result.outcome})
    return describe(state, [event], name, **kwargs)


def test_combat_result_lines():
    assert result_line(combat_result()) == ["[T 3] ⚔ 本场已定：赢 · 洛卡拉掉 5 血（剩 30+9）"]
    lost = combat_result(
        turn=1, outcome="loss", lost=2, dealt=0, my_health=30, my_armor=8, opponent_hero="H_X"
    )
    assert result_line(lost) == ["[T 1] ⚔ 本场已定：输 · 你掉 2 血（剩 30+8）"]
    tie = combat_result(turn=7, outcome="tie", dealt=0, opponent_health=22, opponent_armor=0)
    assert result_line(tie) == ["[T 7] ⚔ 本场已定：平 · 双方都没掉血"]
    ghost = combat_result(
        turn=4, opponent_hero="H_KELTHUZAD", ghost=True, dealt=0, opponent_health=0
    )
    assert result_line(ghost) == ["[T 4] ⚔ 本场已定：赢 · 打的是残影，不掉血"]
    killed = combat_result(
        turn=12,
        opponent_hero="H_SAURFANG",
        dealt=15,
        opponent_health=-8,
        opponent_armor=0,
        opponent_dead=True,
    )
    assert result_line(killed) == [
        "[T12] ⚔ 本场已定：赢 · 萨鲁法尔大王掉 15 血（剩 -8） · 对手被淘汰"
    ]
    dead = combat_result(
        turn=13, outcome="loss", lost=18, dealt=0, my_health=-14, my_armor=0, me_dead=True
    )
    assert result_line(dead) == ["[T13] ⚔ 本场已定：输 · 你掉 18 血（剩 -14） · 你被淘汰"]


def test_combat_result_with_unknown_opponent_only_says_win():
    unknown = combat_result(opponent_id=None, opponent_hero=None, dealt=0, opponent_health=None)
    assert result_line(unknown) == ["[T 3] ⚔ 本场已定：赢"]
    # 赢了却量不到对手掉血（不该发生）：也只写"赢"，不写"掉 0 血"
    assert result_line(combat_result(dealt=0)) == ["[T 3] ⚔ 本场已定：赢"]


def test_combat_result_line_can_be_switched_off():
    assert result_line(combat_result(), combat_result=False) == []
    assert result_line(combat_result(), combat_result=False, verbose=True) == []


def test_combat_ended_line_hides_the_health_when_the_result_is_off(run_log, mini_lines):
    """战斗结束那次快照里的血量已经是打完以后的（比画面早 20~70 秒）：关掉 [combat_result] 时
    "战斗结束"那一行不写血量，开着时照旧。"""
    updates = run_log(mini_lines)
    board = "对手阵容: BG21_029_G(金) 2/2、BGS_116 1/4"
    shown = [line for line in lines_for(updates) if "战斗结束" in line]
    assert shown == [f"[T 1] 战斗结束 我方血量 27 | {board}"]
    hidden = lines_for(updates, combat_result=False)
    assert [line for line in hidden if "战斗结束" in line] == [f"[T 1] 战斗结束 | {board}"]
    assert not any("⚔" in line or "我方血量" in line for line in hidden)
    verbose = lines_for(updates, verbose=True, combat_result=False)
    assert not any("我方血量" in line or "⚔" in line for line in verbose)


def test_combat_result_without_card_data_uses_card_ids():
    line = result_line(combat_result(), name=lambda c: c)
    assert line == ["[T 3] ⚔ 本场已定：赢 · H_ROKARA掉 5 血（剩 30+9）"]


def test_hp_text():
    from lscs.advisor.names import hp_text

    assert (hp_text(30, 8), hp_text(19, 0), hp_text(-14, 0)) == ("30+8", "19", "-14")
