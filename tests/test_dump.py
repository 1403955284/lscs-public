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
        "[T 0] 备选英雄 TB_BaconShop_HERO_49（护甲 未知）",  # 这份合成日志的英雄没有护甲标签
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
