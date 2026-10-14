from pathlib import Path

import pytest

from lscs.config import AppConfig, ConfigError, load_config, mask_key

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"
SECRET = "sk-test-0123456789abcdef"


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_gives_defaults(tmp_path):
    cfg = load_config(tmp_path / "config.toml")
    assert cfg == AppConfig()
    llm = cfg.llm
    assert (llm.enabled, llm.provider, llm.model, llm.base_url, llm.api_key) == (
        True,
        "deepseek",
        "deepseek-flash",
        "",
        "",
    )
    assert (llm.thinking, llm.timeout_seconds, llm.max_calls_per_game, llm.max_output_tokens) == (
        "disabled",
        20,
        100,
        1500,
    )
    deep = (
        llm.deep_analysis,
        llm.deep_thinking,
        llm.deep_max_output_tokens,
        llm.deep_timeout_seconds,
    )
    assert deep == (True, "low", 16000, 75)
    assert llm.hero_advice is True
    assert llm.choice_advice is True
    assert llm.repair_invalid is True


def test_hero_advice_can_be_switched_off(tmp_path):
    assert load_config(write(tmp_path, "[llm]\nhero_advice = false\n")).llm.hero_advice is False


def test_choice_advice_can_be_switched_off(tmp_path):
    cfg = load_config(write(tmp_path, "[llm]\nchoice_advice = false\n"))
    assert cfg.llm.choice_advice is False


def test_repair_invalid_can_be_switched_off(tmp_path):
    cfg = load_config(write(tmp_path, "[llm]\nrepair_invalid = false\n"))
    assert cfg.llm.repair_invalid is False


def test_example_file_matches_defaults():
    assert load_config(EXAMPLE) == AppConfig()
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "\nhero_advice = true " in text
    assert "\nchoice_advice = true " in text
    assert "\nrepair_invalid = true " in text
    assert "\ndrag_handle = true " in text
    assert "\nenabled = false" in text  # [stats]：本版本统计默认关闭


def test_values_are_read(tmp_path):
    path = write(tmp_path, f'[llm]\napi_key = "{SECRET}"\nthinking = "low"\ntimeout_seconds = 45\n')
    llm = load_config(path).llm
    assert (llm.api_key, llm.thinking, llm.timeout_seconds) == (SECRET, "low", 45)


def test_deep_analysis_values_are_read(tmp_path):
    path = write(
        tmp_path,
        '[llm]\ndeep_analysis = false\ndeep_thinking = "high"\n'
        "deep_max_output_tokens = 8000\ndeep_timeout_seconds = 40\n",
    )
    llm = load_config(path).llm
    assert (llm.deep_analysis, llm.deep_thinking) == (False, "high")
    assert (llm.deep_max_output_tokens, llm.deep_timeout_seconds) == (8000, 40)


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ('deep_thinking = "disabled"', "llm.deep_thinking"),
        ("deep_max_output_tokens = 0", "llm.deep_max_output_tokens"),
        ("deep_timeout_seconds = 0", "llm.deep_timeout_seconds"),
    ],
)
def test_deep_analysis_invalid_values_are_rejected(tmp_path, line, field):
    with pytest.raises(ConfigError, match=field):
        load_config(write(tmp_path, f"[llm]\n{line}\n"))


def test_wrong_type_names_the_field_without_echoing_values(tmp_path):
    path = write(tmp_path, f'[llm]\napi_key = "{SECRET}"\ntimeout_seconds = "很久"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "llm.timeout_seconds" in str(exc.value)
    assert "很久" not in str(exc.value)
    assert SECRET not in str(exc.value)


def test_unknown_key_is_rejected(tmp_path):
    path = write(tmp_path, '[llm]\napi_key_env = "DEEPSEEK_API_KEY"\n')
    with pytest.raises(ConfigError, match="llm.api_key_env"):
        load_config(path)


def test_invalid_toml(tmp_path):
    with pytest.raises(ConfigError, match="不是合法的 TOML"):
        load_config(write(tmp_path, "[llm\n"))


def test_utf8_bom_is_accepted(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf" + f'[llm]\napi_key = "{SECRET}"\n'.encode())
    assert load_config(path).llm.api_key == SECRET


def test_non_utf8_file_raises_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes('[llm]\nmodel = "中文"\n'.encode("gbk"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path)


def test_api_key_whitespace_is_stripped(tmp_path):
    path = write(tmp_path, f'[llm]\napi_key = "  {SECRET}\\n"\n')
    assert load_config(path).llm.api_key == SECRET


def test_key_never_in_repr(tmp_path):
    cfg = load_config(write(tmp_path, f'[llm]\napi_key = "{SECRET}"\n'))
    assert SECRET not in repr(cfg) and SECRET not in str(cfg)


def test_mask_key():
    assert mask_key(SECRET) == "sk-t…cdef"
    assert mask_key("short") == "****"
    assert mask_key("") == "（未填写）"


def test_overlay_defaults():
    from lscs.config import OverlayConfig

    cfg = AppConfig().overlay
    assert cfg == OverlayConfig()
    assert (cfg.enabled, cfg.position, cfg.opacity, cfg.font_size, cfg.click_through) == (
        True,
        "right",
        0.85,
        0,
        True,
    )
    assert cfg.drag_handle is True  # 面板右上角的拖动标签


def test_overlay_values_are_read(tmp_path):
    path = write(
        tmp_path, '[overlay]\nenabled = false\nposition = "top"\nopacity = 0.5\nfont_size = 18\n'
    )
    cfg = load_config(path).overlay
    assert (cfg.enabled, cfg.position, cfg.opacity, cfg.font_size) == (False, "top", 0.5, 18)
    assert (
        load_config(write(tmp_path, "[overlay]\ndrag_handle = false\n")).overlay.drag_handle
        is False
    )


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ('position = "bottom"', "overlay.position"),
        ("opacity = 0.1", "overlay.opacity"),
        ("opacity = 1.5", "overlay.opacity"),
        ("font_size = 5", "overlay.font_size"),
        ("font_size = 41", "overlay.font_size"),
        ("size = 3", "overlay.size"),
    ],
)
def test_overlay_invalid_values_are_rejected(tmp_path, line, field):
    with pytest.raises(ConfigError, match=field):
        load_config(write(tmp_path, f"[overlay]\n{line}\n"))


def test_highlight_defaults_and_values():
    from lscs.config import HighlightConfig

    cfg = AppConfig().highlight
    assert cfg == HighlightConfig()
    assert (cfg.enabled, cfg.color, cfg.aspect) == (True, "#f2d98a", 16 / 9)  # 浅金
    assert (cfg.style, cfg.pulse) == ("corners", True)  # 四角括号，新目标出现时呼吸
    assert (cfg.shop_y, cfg.shop_pitch, cfg.board_y) == (0.367, 0.128, 0.55)
    assert (cfg.upgrade, cfg.refresh) == ((0.412, 0.175, 0.05), (0.588, 0.175, 0.05))
    assert (cfg.freeze, cfg.hero_power) == ((0.646, 0.175, 0.05), (0.593, 0.78, 0.055))


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("shop_y = 2.5", "highlight.shop_y"),
        ("board_pitch = -0.1", "highlight.board_pitch"),
        ("upgrade = [0.2, 0.3, 0.6]", "highlight.upgrade"),
        ("upgrade = [0.2, 0.3]", "highlight.upgrade"),
        ('color = "gold"', "highlight.color"),
        ('style = "glow"', "highlight.style"),
        ("aspect = 0", "highlight.aspect"),
        ("shop_size = 3", "highlight.shop_size"),
    ],
)
def test_highlight_invalid_values_are_rejected(tmp_path, line, field):
    with pytest.raises(ConfigError, match=field):
        load_config(write(tmp_path, f"[highlight]\n{line}\n"))


def test_highlight_values_are_read(tmp_path):
    text = "[highlight]\nenabled = false\nshop_y = 0.3\nhero_power = [0.6, 0.8, 0.05]\n"
    cfg = load_config(write(tmp_path, text)).highlight
    assert (cfg.enabled, cfg.shop_y, cfg.hero_power) == (False, 0.3, (0.6, 0.8, 0.05))


def test_highlight_style_values_are_read(tmp_path):
    text = '[highlight]\nstyle = "frame"\npulse = false\ncolor = "#ffd34d"\n'
    cfg = load_config(write(tmp_path, text)).highlight
    assert (cfg.style, cfg.pulse, cfg.color) == ("frame", False, "#ffd34d")
    example = EXAMPLE.read_text(encoding="utf-8")
    assert '\ncolor = "#f2d98a" ' in example
    assert '\nstyle = "corners" ' in example and "\npulse = true " in example


# 老版本 config.example.toml 的 [highlight]（校准、换样式之前）里和现在不一样的行
OLD_HIGHLIGHT_LINES = [
    'color = "#ffd34d"',
    "shop_y = 0.31",
    "shop_pitch = 0.143",
    "shop_width = 0.125",
    "shop_height = 0.175",
    "board_y = 0.585",
    "board_pitch = 0.143",
    "board_width = 0.125",
    "board_height = 0.175",
    "upgrade = [0.19, 0.3, 0.05]",
    "refresh = [0.19, 0.43, 0.045]",
    "freeze = [0.81, 0.43, 0.045]",
    "hero_power = [0.585, 0.82, 0.06]",
]


def test_old_highlight_template_values_are_found(tmp_path):
    from lscs.config import HighlightConfig, old_highlight_values

    assert old_highlight_values(HighlightConfig()) == []
    assert old_highlight_values(load_config(EXAMPLE).highlight) == []
    text = "[highlight]\n" + "\n".join(OLD_HIGHLIGHT_LINES) + "\nchoice_y = 0.5\n"
    assert old_highlight_values(load_config(write(tmp_path, text)).highlight) == OLD_HIGHLIGHT_LINES


@pytest.mark.parametrize(
    ("values", "found"),
    [
        ({"color": "#FFD34D"}, ['color = "#ffd34d"']),  # 颜色不分大小写
        ({"color": "#ffd34e"}, []),
        ({"shop_y": 0.3100000001}, ["shop_y = 0.31"]),  # 小数允许一点误差
        ({"shop_y": 0.311}, []),
        ({"upgrade": (0.19, 0.30000001, 0.05)}, ["upgrade = [0.19, 0.3, 0.05]"]),
        ({"upgrade": (0.19, 0.30, 0.0)}, []),  # 三个数都一样才算
        (
            {"refresh": (0.19, 0.43, 0.045), "shop_y": 0.31},
            ["shop_y = 0.31", "refresh = [0.19, 0.43, 0.045]"],
        ),
    ],
)
def test_old_highlight_values_compare_with_a_tolerance(values, found):
    from lscs.config import HighlightConfig, old_highlight_values

    assert old_highlight_values(HighlightConfig(**values)) == found


def test_all_opponents_defaults_on_and_can_be_switched_off(tmp_path):
    assert load_config(tmp_path / "config.toml").llm.all_opponents is True
    cfg = load_config(write(tmp_path, "[llm]\nall_opponents = false\n"))
    assert cfg.llm.all_opponents is False
    assert "\nall_opponents = true " in EXAMPLE.read_text(encoding="utf-8")


def test_overlay_opponents_defaults_on_and_can_be_switched_off(tmp_path):
    assert load_config(tmp_path / "config.toml").overlay.opponents is True
    cfg = load_config(write(tmp_path, "[overlay]\nopponents = false\n"))
    assert cfg.overlay.opponents is False
    assert "\nopponents = true " in EXAMPLE.read_text(encoding="utf-8")


def test_review_defaults_and_values(tmp_path):
    from lscs.config import ReviewConfig

    cfg = AppConfig().review
    assert cfg == ReviewConfig()
    assert (cfg.enabled, cfg.ai, cfg.dir, cfg.history_games) == (True, True, "reviews", 10)
    path = write(
        tmp_path, '[review]\nenabled = false\nai = false\ndir = " D:/复盘 "\nhistory_games = 3\n'
    )
    cfg = load_config(path).review
    assert (cfg.enabled, cfg.ai, cfg.dir, cfg.history_games) == (False, False, "D:/复盘", 3)
    text = EXAMPLE.read_text(encoding="utf-8")
    assert text.index("[review]") < text.index("[highlight]")
    assert '\ndir = "reviews" ' in text and "\nhistory_games = 10 " in text


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("history_games = 2", "review.history_games"),
        ("history_games = 51", "review.history_games"),
        ('dir = ""', "review.dir"),
        ('dir = "  "', "review.dir"),
        ("save = true", "review.save"),
    ],
)
def test_review_invalid_values_are_rejected(tmp_path, line, field):
    with pytest.raises(ConfigError, match=field):
        load_config(write(tmp_path, f"[review]\n{line}\n"))


def test_positioning_defaults_on_and_can_be_switched_off(tmp_path):
    from lscs.config import PositioningConfig

    assert AppConfig().positioning == PositioningConfig() and PositioningConfig().enabled is True
    cfg = load_config(write(tmp_path, "[positioning]\nenabled = false\n"))
    assert cfg.positioning.enabled is False
    text = EXAMPLE.read_text(encoding="utf-8")
    assert text.index("[review]") < text.index("[positioning]") < text.index("[highlight]")
    section = text[text.index("[positioning]") : text.index("[highlight]")]
    assert "\nenabled = true " in section and "不调用 AI" in section


def test_positioning_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="positioning.moves"):
        load_config(write(tmp_path, "[positioning]\nmoves = 3\n"))


def test_stats_defaults_off(tmp_path):
    from lscs.config import StatsConfig

    cfg = load_config(tmp_path / "config.toml").stats
    assert cfg == StatsConfig() == AppConfig().stats
    assert (cfg.enabled, cfg.mmr_percentile, cfg.period, cfg.cards) == (
        False,
        100,
        "last-patch",
        True,
    )


def test_stats_values_are_read(tmp_path):
    text = '[stats]\nenabled = true\nmmr_percentile = 50\nperiod = "past-seven"\ncards = false\n'
    cfg = load_config(write(tmp_path, text)).stats
    assert (cfg.enabled, cfg.mmr_percentile, cfg.period, cfg.cards) == (
        True,
        50,
        "past-seven",
        False,
    )


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("mmr_percentile = 30", "stats.mmr_percentile"),
        ('period = "past-three"', "stats.period"),
        ("enabled = 1.5", "stats.enabled"),
        ("url = 1", "stats.url"),
    ],
)
def test_stats_invalid_values_are_rejected(tmp_path, line, field):
    with pytest.raises(ConfigError, match=field):
        load_config(write(tmp_path, f"[stats]\n{line}\n"))


def test_stats_section_in_the_example_file():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert text.index("[positioning]") < text.index("[stats]") < text.index("[highlight]")
    section = text[text.index("[stats]") : text.index("[highlight]")]
    assert "\nenabled = false" in section and "Firestone" in section
    assert "\nmmr_percentile = 100 " in section and '\nperiod = "last-patch" ' in section
    assert "\ncards = true " in section


def test_old_trinket_row_values_are_reported():
    from lscs.config import HighlightConfig, old_highlight_values

    old = HighlightConfig(
        choice4_y=0.5, choice4_pitch=0.34, choice4_width=0.28, choice4_height=0.44
    )
    assert old_highlight_values(old) == [
        "choice4_y = 0.5",
        "choice4_pitch = 0.34",
        "choice4_width = 0.28",
        "choice4_height = 0.44",
    ]
    assert old_highlight_values(HighlightConfig()) == []


def test_combat_result_defaults_on_and_can_be_switched_off(tmp_path):
    from lscs.config import CombatResultConfig

    assert AppConfig().combat_result == CombatResultConfig()
    assert CombatResultConfig().enabled is True
    cfg = load_config(write(tmp_path, "[combat_result]\nenabled = false\n"))
    assert cfg.combat_result.enabled is False
    text = EXAMPLE.read_text(encoding="utf-8")
    assert text.index("[positioning]") < text.index("[combat_result]") < text.index("[stats]")
    section = text[text.index("[combat_result]") : text.index("[stats]")]
    assert "\nenabled = true" in section and "不是预测" in section and "棋盘顶部" in section


def test_combat_result_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="combat_result.banner"):
        load_config(write(tmp_path, "[combat_result]\nbanner = true\n"))
