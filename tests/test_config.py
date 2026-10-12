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
        80,
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


def test_hero_advice_can_be_switched_off(tmp_path):
    assert load_config(write(tmp_path, "[llm]\nhero_advice = false\n")).llm.hero_advice is False


def test_example_file_matches_defaults():
    assert load_config(EXAMPLE) == AppConfig()
    assert "\nhero_advice = true " in EXAMPLE.read_text(encoding="utf-8")


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


def test_overlay_values_are_read(tmp_path):
    path = write(
        tmp_path, '[overlay]\nenabled = false\nposition = "top"\nopacity = 0.5\nfont_size = 18\n'
    )
    cfg = load_config(path).overlay
    assert (cfg.enabled, cfg.position, cfg.opacity, cfg.font_size) == (False, "top", 0.5, 18)


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
