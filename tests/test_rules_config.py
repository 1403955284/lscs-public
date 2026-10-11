from pathlib import Path

from lscs.advisor.rules_config import base_hero_id, load_rules_config

RULES = Path(__file__).resolve().parents[1] / "strategy" / "current" / "rules.toml"


def test_load_real_rules_file():
    cfg = load_rules_config(RULES)
    assert cfg.curve == {2: 2, 4: 3, 6: 4, 8: 5, 10: 6}
    assert (cfg.greedy_at, cfg.safe_below, cfg.buy_cost_default) == (30, 15, 3)
    assert (cfg.keyword_score, cfg.tribe_score, cfg.spell_score) == (2, 2, 1)


def test_target_tier_follows_curve_and_greedy_health():
    cfg = load_rules_config(RULES)
    assert [cfg.target_tier(t, 20) for t in range(1, 12)] == [1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6]
    # 有效血量 ≥ 30 时提前一回合
    assert [cfg.target_tier(t, 30) for t in (1, 3, 5)] == [2, 3, 4]


def test_hero_buy_cost_override_ignores_skins():
    cfg = load_rules_config(RULES)
    assert cfg.buy_cost_heroes == {"TB_BaconShop_HERO_49": 2}
    assert cfg.buy_cost_for("TB_BaconShop_HERO_49_SKIN_A") == 2
    assert cfg.buy_cost_for("BG21_HERO_000") == 3
    assert cfg.buy_cost_for(None) == 3


def test_base_hero_id():
    assert base_hero_id("TB_BaconShop_HERO_49_SKIN_A") == "TB_BaconShop_HERO_49"
    assert base_hero_id("BG21_HERO_000") == "BG21_HERO_000"
