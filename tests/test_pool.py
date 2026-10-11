from dataclasses import dataclass

from lscs.state.pool import base_card_id, estimate_pool, load_pool_config


@dataclass
class Card:
    card_id: str
    tier: int | None
    golden: bool = False


def test_load_pool_config(tmp_path):
    path = tmp_path / "pool.toml"
    path.write_text("tribe_count = 5\n[copies]\n1 = 16\n2 = 15\n", encoding="utf-8")
    config = load_pool_config(path)
    assert config.tribe_count == 5
    assert config.copies == {1: 16, 2: 15}


def test_base_card_id():
    assert base_card_id("BG21_029_G") == "BG21_029"
    assert base_card_id("BG21_029") == "BG21_029"


def test_held_copies_are_subtracted_golden_counts_three():
    pool = estimate_pool(
        {1: 16, 2: 15},
        held=[Card("BG21_029", 1), Card("BG21_029_G", 1, golden=True), Card("BGS_061", 1)],
    )
    assert pool == {"BG21_029": 12, "BGS_061": 15}


def test_seen_cards_are_listed_without_subtraction():
    pool = estimate_pool({1: 16, 2: 15}, held=[], seen=[Card("BG23_009", 2)])
    assert pool == {"BG23_009": 15}


def test_unknown_tier_and_unconfigured_tier_are_skipped_and_floor_is_zero():
    held = [Card("X", None), Card("Y", 7)] + [Card("Z", 1)] * 20
    assert estimate_pool({1: 16}, held=held) == {"Z": 0}
