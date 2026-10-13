"""复盘的输出：终端块、Markdown、文件名、history.jsonl 和习惯（对局复盘设计文档第 7 节）。"""

import gzip
import json
import re
from dataclasses import replace
from datetime import datetime

import pytest
from helpers import NEEDS_USER_LOG, USER_LOG, replay_log
from test_review_flags import RULES, advice, combat, curve_turns, name, record

from lscs.review.flags import Flag
from lscs.review.record import ChoicePick, RecordBuilder
from lscs.review.report import (
    Habits,
    append_history,
    final_focus,
    habits,
    history_entry,
    markdown,
    read_history,
    review_game,
    review_path,
    save_review,
    terminal_lines,
)

WHEN = datetime(2026, 10, 11, 23, 20)


def game(turns=None, combats=(), **kw):
    turns = turns or curve_turns(
        {
            5: dict(tier_start=3, tier_end=4, buys=(), board_end=curve_turns()[2].board_end),
            11: dict(tier_start=6, tier_end=6),
        }
    )
    return record(turns, combats, **kw)


def test_terminal_block_lists_moments_focus_data_and_path(tmp_path):
    rec = game(combats=[combat(5, dealt=12)])
    review = save_review(review_game(rec, RULES, name, WHEN), tmp_path / "reviews", name, tmp_path)
    lines = terminal_lines(review)
    assert lines[0] == "  📝 复盘：H 第 4 名（10 回合）"
    assert (
        lines[1]
        == "     ① 第 5 回合 升本不补场面：升 4 本花光 3 金，没买随从，场上只有 3 个（这时该有 4 个）"
    )
    assert lines[2].startswith("        下次：升本的回合剩下的铸币也要买一张随从；")
    assert lines[3] == "     ② 第 2 回合 做得好：升 2 本的同时还买了 1 张随从"
    assert "     下局重点：升本的回合剩下的铸币也要买一张随从" in lines
    assert any(
        line.startswith("     数据：升本 第 2/4/5/6/8/10 回合（第 10 回合 6 本） · 刷新 0 次")
        for line in lines
    )
    assert lines[-1] == "     已保存 " + str(review.path.relative_to(tmp_path))
    assert review.path.name == "2026-10-11_2320_H_第4名.md"


def test_same_fix_twice_says_same_as_above():
    flags = (
        Flag(
            kind="upgrade_without_board", turns=(7, 8), severity=3, facts="a", fix="买随从；补场面"
        ),
        Flag(kind="upgrade_without_board", turns=(5,), severity=2, facts="b", fix="买随从；补场面"),
    )
    review = replace(review_game(game(), RULES, name, WHEN), moments=flags, save_error="磁盘满了")
    lines = terminal_lines(review)
    assert lines[1:5] == [
        "     ① 第 7、8 回合 升本不补场面：a",
        "        下次：买随从；补场面",
        "     ② 第 5 回合 升本不补场面：b",
        "        下次：（同上）",
    ]
    assert lines[-1] == "     复盘没能保存：磁盘满了"


def test_no_flags_and_uncertain_game():
    rec = game(curve_turns()).model_copy(update={"uncertain": True})
    review = review_game(rec.model_copy(update={"turns": rec.turns[:1]}), RULES, name, WHEN)
    lines = terminal_lines(review)
    assert "     ⚠ 日志解析出错过，复盘可能不准" in lines
    assert "     这局没有触发提醒" in lines
    assert "> ⚠ 日志解析出错过，复盘可能不准" in markdown(review, name)


def test_data_line_shows_advice_followed_only_with_advice():
    turns = curve_turns(
        {
            3: dict(advice=advice(advised=4, followed=3)),
            4: dict(advice=advice(advised=0, followed=0)),
        }
    )
    review = review_game(game(turns, [combat(3, lost=2), combat(4, lost=5)]), RULES, name, WHEN)
    data = next(line for line in terminal_lines(review) if "数据：" in line)
    assert data.endswith("掉血 2 场 共 7 · 建议采纳 3/4")
    text = markdown(review, name)
    assert "| 3 | 2 | 3→0 | 买 蚀脑怪 | 3 个 · 身材 12 | 负 −2（H） | 3/4 |" in text
    assert "| 4 | 2→3 | 3→0 | 升本 0 金；买 蚀脑怪 | 4 个 · 身材 16 | 负 −5（H） | — |" in text


def test_markdown_sections():
    turns = curve_turns(
        {
            6: dict(
                choices=(ChoicePick(id=3, kind="trinket", card_id="X", cost=4, ai_card_id="X"),)
            ),
            9: dict(
                choices=(
                    ChoicePick(id=5, kind="dark_gift", card_id="Y", cost=None, ai_card_id="B"),
                    ChoicePick(id=6, kind="discover", card_id=None, cost=None),
                ),
                rerolls=2,
                gold_gained=3,
                sells=("A",),
                spells=("S",),
                froze=True,
                hero_power=2,
                triples=("A",),
            ),
        }
    )
    combats = [combat(9, dealt=12), combat(10, lost=18, after=-3)]
    review = review_game(game(turns, combats), RULES, name, WHEN)
    text = markdown(review, name)
    assert text.startswith(
        "# H · 第 4 名 · 2026-10-11 23:20\n\n本局种族：畸变、恶魔 · 10 个回合 · 最后一场：第 10 回合打H掉 18 血被淘汰\n"
    )
    for section in ("## 关键时刻", "**下局重点**：", "## 逐回合", "## 提醒（规则）", "## 选项"):
        assert section in text
    assert "## 最近" not in text
    assert (
        "| 9 | 5 | 3→0（+3） | 刷新 2 次；买 蚀脑怪、S（法术）；卖 蚀脑怪；三连 蚀脑怪；冻结；技能 2 次 |"
        in text
    )
    assert (
        "| 10 | 5→6 | 3→0 | 升本 0 金；买 蚀脑怪 | 7 个 · 身材 28 | 负 −18，淘汰（H） | — |" in text
    )
    assert "- 第 6 回合 饰品：选 飘荡祭品（4 金）（AI 也选它）" in text
    assert "- 第 9 回合 黑暗之赐：选 泰瑞尔（AI 推荐 虚悯者）" in text
    assert "- 第 9 回合 发现：没看到选了哪个" in text
    assert "- 第 9 回合 做得好：凑出金色蚀脑怪，打出 12 血" in text
    assert text.endswith("<!-- lscs review v1 -->\n")


def test_review_path_names_and_duplicates(tmp_path):
    first = review_path(tmp_path, WHEN, "凯瑞尔·罗姆", 3)
    assert first.name == "2026-10-11_2320_凯瑞尔·罗姆_第3名.md"
    first.write_text("x", encoding="utf-8")
    assert (
        review_path(tmp_path, WHEN, "凯瑞尔·罗姆", 3).name
        == "2026-10-11_2320_凯瑞尔·罗姆_第3名_2.md"
    )
    (tmp_path / "2026-10-11_2320_凯瑞尔·罗姆_第3名_2.md").write_text("x", encoding="utf-8")
    assert review_path(tmp_path, WHEN, "凯瑞尔·罗姆", 3).name.endswith("_第3名_3.md")
    assert (
        review_path(tmp_path, WHEN, 'A/B: "C"?', None).name == "2026-10-11_2320_A_B___C___未完.md"
    )


def test_save_failure_does_not_raise(tmp_path):
    blocker = tmp_path / "reviews"
    blocker.write_text("不是目录", encoding="utf-8")
    review = save_review(review_game(game(), RULES, name, WHEN), blocker, name, tmp_path)
    assert review.path is None and review.save_error
    assert terminal_lines(review)[-1].startswith("     复盘没能保存：")


def test_history_entry_fields(tmp_path):
    turns = curve_turns({4: dict(rerolls=3, gold_end=2), 6: dict(rerolls=1, triples=("A",))})
    combats = [combat(7, lost=15, after=25), combat(8, lost=5, after=20), combat(9, dealt=3)]
    review = save_review(
        review_game(game(turns, combats), RULES, name, WHEN), tmp_path / "r", name, tmp_path
    )
    entry = history_entry(review)
    assert entry == {
        "time": "2026-10-11T23:20:00",
        "hero": "H",
        "hero_name": "H",
        "place": 4,
        "turns": 10,
        "tier_at": {"4": 3, "6": 4, "8": 5, "10": 6},
        "upgrade_turns": [2, 4, 6, 8, 10],
        "rerolls": 4,
        "max_rerolls": 3,
        "gold_unspent": 2,
        "hp_lost": 20,
        "loss_combats": 2,
        "board_at": {"3": 3, "5": 5, "7": 7, "9": 7},
        "triples": 1,
        "advised": 0,
        "followed": 0,
        "flags": ["hp_streak"],
        "file": str(review.path.relative_to(tmp_path)),
    }
    path = tmp_path / "r" / "history.jsonl"
    append_history(path, entry)
    append_history(path, {**entry, "place": 1})
    assert [e["place"] for e in read_history(path)] == [4, 1]


def test_history_flags_count_a_merged_streak():
    flag = Flag(
        kind="upgrade_without_board",
        turns=(7, 8),
        severity=3,
        facts="",
        fix="",
        streak_turns=(8, 9),
    )
    review = replace(review_game(game(), RULES, name, WHEN), flags=(flag,))
    assert history_entry(review)["flags"] == ["upgrade_without_board", "hp_streak"]


def entry(place, *flags):
    return {"place": place, "flags": list(flags)}


def test_habits_need_three_games_and_a_third_of_them():
    assert habits([entry(3, "reroll_heavy")] * 2, 10) is None
    games = [
        entry(3, "upgrade_without_board", "good_turn"),
        entry(5, "upgrade_without_board", "reroll_heavy", "choice_disagree"),
        entry(2, "upgrade_without_board", "choice_disagree"),
        entry(4, "reroll_heavy", "tier_behind", "choice_disagree"),
        entry(None, "gold_unspent", "upgrade_without_board", "reroll_heavy"),
    ]
    found = habits(games, 10)
    # 刷新太多 3 局：至少 3 局、也占了 5 局的 1/3 以上；选项和 AI 不同不算习惯
    assert found == Habits(
        games=5, avg_place=3.5, kinds=(("upgrade_without_board", 4), ("reroll_heavy", 3))
    )
    assert found.line() == "最近 5 局：平均第 3.5 名 · 升本不补场面 4 局 · 刷新太多 3 局"
    assert found.sentence() == "平均第 3.5 名；升本不补场面 4 局、刷新太多 3 局。"
    # 只出现 2 局的不算
    recent = habits(games[:4], 4)
    assert recent.kinds == (("upgrade_without_board", 3),)
    # 10 局里 3 局不到 1/3：不算
    ten = [entry(4, "gold_unspent")] * 3 + [entry(4)] * 7
    assert habits(ten, 10).kinds == ()
    nine = [entry(4, "gold_unspent")] * 3 + [entry(4)] * 6
    assert habits(nine, 10).kinds == (("gold_unspent", 3),)
    # 同一局里出现两次只算一局
    assert habits([entry(1, "gold_unspent", "gold_unspent")] * 3 + [entry(1)], 10).kinds == (
        ("gold_unspent", 3),
    )
    many = [entry(2, "tier_behind", "reroll_heavy", "gold_unspent", "no_direction")] * 3
    assert len(habits(many, 10).kinds) == 3


def test_bad_history_lines_are_skipped(tmp_path, caplog):
    path = tmp_path / "history.jsonl"
    path.write_text('{"place": 2}\n不是 json\n[1, 2]\n\n{"place": 4}\n', encoding="utf-8")
    assert read_history(path) == [{"place": 2}, {"place": 4}]
    assert "第 2 行坏了" in caplog.text
    assert read_history(tmp_path / "missing.jsonl") == []


def test_focus_follows_the_most_common_habit():
    flags = (
        Flag(
            kind="gold_unspent",
            turns=(3,),
            severity=1,
            facts="",
            fix="铸币不会留到下回合：剩 3 金买随从，剩 1~2 金刷新",
        ),
        Flag(
            kind="upgrade_without_board",
            turns=(5,),
            severity=2,
            facts="",
            fix="升本也买；补场面",
            streak_turns=(5, 6),
        ),
    )
    habit = Habits(games=5, avg_place=4.0, kinds=(("gold_unspent", 3),))
    assert (
        final_focus("升本也买", flags, habit)
        == "铸币不会留到下回合：剩 3 金买随从，剩 1~2 金刷新（最近 5 局里 3 局）"
    )
    streak = Habits(games=6, avg_place=4.0, kinds=(("hp_streak", 4),))
    assert final_focus("x", flags, streak).endswith("（最近 6 局里 4 局）")
    assert (
        final_focus("x", flags, Habits(games=5, avg_place=4.0, kinds=(("reroll_heavy", 3),))) == "x"
    )
    assert final_focus("x", flags, None) == "x"


def test_review_game_counts_this_game_in_the_habits():
    history = [entry(5, "upgrade_without_board")] * 2
    review = review_game(game(), RULES, name, WHEN, history, 10)
    assert review.habits == Habits(games=3, avg_place=4.7, kinds=(("upgrade_without_board", 3),))
    assert review.focus.endswith("（最近 3 局里 3 局）")
    text = markdown(review, name)
    assert "## 最近 3 局\n平均第 4.7 名；升本不补场面 3 局。\n" in text


def battle_tags():
    """日志里玩家实体的名字（不写进测试源码）。"""
    with gzip.open(USER_LOG, "rt", encoding="utf-8", errors="replace") as f:
        text = f.read()
    tags = set(re.findall(r"Entity=([^ =\[\]]+#\d+) ", text))
    tags |= set(re.findall(r"PlayerName=(\S+)", text))
    return tags


@NEEDS_USER_LOG
def test_user_review_file_has_no_player_names(tmp_path):
    from lscs.cli import DEFAULT_POOL
    from lscs.state.pool import load_pool_config

    builder = RecordBuilder()
    for state, events in replay_log(USER_LOG, load_pool_config(DEFAULT_POOL)):
        builder.on_update(state, events)
    review = save_review(review_game(builder.finish(), RULES, name, WHEN), tmp_path, name)
    text = review.path.read_text(encoding="utf-8") + "\n".join(terminal_lines(review))
    text += json.dumps(history_entry(review), ensure_ascii=False)
    assert not re.search(r"#\d{4,}", text)
    tags = battle_tags()
    assert tags  # 日志里确实有名字
    for tag in tags:
        assert tag not in text
        base = tag.split("#")[0]
        if len(base) >= 3:
            assert base not in text


@pytest.mark.parametrize("place", [None, 1])
def test_place_text_in_title(place):
    review = review_game(game().model_copy(update={"place": place}), RULES, name, WHEN)
    expected = "名次未知" if place is None else "第 1 名"
    assert terminal_lines(review)[0] == f"  📝 复盘：H {expected}（10 回合）"


def test_habits_ignore_unknown_kinds_and_history_lines():
    from lscs.review.report import history_line

    games = [entry(2, "mystery", 3, "reroll_heavy")] * 3
    assert habits(games, 10).kinds == (("reroll_heavy", 3),)
    line = {
        "time": "2026-10-11T23:20:00",
        "hero_name": "凯瑞尔·罗姆",
        "place": 3,
        "flags": ["upgrade_without_board", "hp_streak", "good_turn", "upgrade_without_board"],
    }
    assert history_line(line) == "2026-10-11 23:20 凯瑞尔·罗姆 第 3 名 · 升本不补场面、连续掉血"
    assert history_line({"hero": "H", "flags": "坏"}) == "时间未知 H 名次未知 · 没有提醒"
