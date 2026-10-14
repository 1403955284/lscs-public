"""生成 mini_bg.log：一局人工构造的、极简的单人酒馆战棋。

运行：uv run python tests/fixtures/power/make_mini_bg.py

剧情（实体 ID）：
- 1 游戏实体；2 我（PlayerID=1）；3 Bob（PlayerID=2，AI）
- 10 占位英雄 → 选择 14 米尔豪斯；15 英雄技能
- 12/13 排行榜上的两个对手（PlayerID 3、4）
- 第 1 回合：商店 30 小鬼、31 海盗、32 元素（嘲讽）；买下 31 并打出；冻结剩下两张
- 第 1 场战斗：对手 3（复制英雄 40），阵容 41 金色小鬼 + 42 元素；我掉 3 血，
  Bob 的英雄换回来之后 STEP 变成 MAIN_CLEANUP（战斗结果在这时结算）
- 第 2 回合：商店 50 纳迦（2 本）、51 酒馆法术；升本费用降到 4
- 结束：我第 5 名
"""

from pathlib import Path

POWER = "GameState.DebugPrintPower() - "
GAME = "GameState.DebugPrintGame() - "


def entity(entity_id: int, card_id: str, **tags: object) -> list[str]:
    lines = [f"FULL_ENTITY - Creating ID={entity_id} CardID={card_id}"]
    lines += [f"    tag={tag} value={value}" for tag, value in tags.items()]
    return lines


def shop_minion(entity_id, card_id, pos, atk, health, tier, race, **extra):
    return entity(
        entity_id,
        card_id,
        CONTROLLER=2,
        CARDTYPE="MINION",
        ZONE="PLAY",
        ZONE_POSITION=pos,
        ATK=atk,
        HEALTH=health,
        TECH_LEVEL=tier,
        CARDRACE=race,
        **extra,
    )


GROUPS: dict[str, list[str]] = {
    "10:00:00.0000000": [
        "CREATE_GAME",
        "    GameEntity EntityID=1",
        "        tag=CARDTYPE value=GAME",
        "        tag=ZONE value=PLAY",
        "        tag=TURN value=0",
        "    Player EntityID=2 PlayerID=1 GameAccountId=[hi=1 lo=1]",
        "        tag=PLAYER_ID value=1",
        "        tag=CONTROLLER value=1",
        "        tag=CARDTYPE value=PLAYER",
        "        tag=HERO_ENTITY value=10",
        "        tag=PLAYER_TECH_LEVEL value=1",
        "    Player EntityID=3 PlayerID=2 GameAccountId=[hi=0 lo=0]",
        "        tag=PLAYER_ID value=2",
        "        tag=CONTROLLER value=2",
        "        tag=CARDTYPE value=PLAYER",
        "        tag=HERO_ENTITY value=11",
        *entity(10, "TB_BaconShop_HERO_PH", CONTROLLER=1, CARDTYPE="HERO", ZONE="PLAY", HEALTH=30),
        *entity(11, "TB_BaconShopBob", CONTROLLER=2, CARDTYPE="HERO", ZONE="PLAY", HEALTH=60),
        *entity(
            12,
            "TB_BaconShop_HERO_75",
            CONTROLLER=2,
            CARDTYPE="HERO",
            ZONE="SETASIDE",
            HEALTH=30,
            PLAYER_ID=3,
            PLAYER_LEADERBOARD_PLACE=2,
            PLAYER_TECH_LEVEL=1,
        ),
        *entity(
            13,
            "BG22_HERO_201",
            CONTROLLER=2,
            CARDTYPE="HERO",
            ZONE="SETASIDE",
            HEALTH=30,
            PLAYER_ID=4,
            PLAYER_LEADERBOARD_PLACE=3,
            PLAYER_TECH_LEVEL=1,
        ),
        *entity(14, "TB_BaconShop_HERO_49", CONTROLLER=1, CARDTYPE="HERO", ZONE="HAND", HEALTH=30),
        GAME + "BuildNumber=999999",
        GAME + "GameType=GT_BATTLEGROUNDS",
        GAME + "FormatType=FT_WILD",
        GAME + "PlayerID=1, PlayerName=Me#1234",
        GAME + "PlayerID=2, PlayerName=The Innkeeper",
    ],
    "10:00:05.0000000": [
        "TAG_CHANGE Entity=2 tag=HERO_ENTITY value=14",
        "TAG_CHANGE Entity=14 tag=ZONE value=PLAY",
        "TAG_CHANGE Entity=14 tag=PLAYER_ID value=1",
        "TAG_CHANGE Entity=14 tag=PLAYER_LEADERBOARD_PLACE value=1",
        "TAG_CHANGE Entity=10 tag=ZONE value=SETASIDE",
        *entity(15, "TB_BaconShop_HP_054", CONTROLLER=1, CARDTYPE="HERO_POWER", ZONE="PLAY"),
    ],
    "10:00:10.0000000": [
        "TAG_CHANGE Entity=GameEntity tag=TURN value=1",
        "TAG_CHANGE Entity=2 tag=RESOURCES value=3",
        "TAG_CHANGE Entity=2 tag=NEXT_OPPONENT_PLAYER_ID value=3",
        *entity(
            20,
            "TB_BaconShop_8p_Reroll_Button",
            CONTROLLER=1,
            CARDTYPE="GAME_MODE_BUTTON",
            ZONE="PLAY",
            COST=1,
        ),
        *entity(
            21,
            "TB_BaconShopTechUp02_Button",
            CONTROLLER=1,
            CARDTYPE="GAME_MODE_BUTTON",
            ZONE="PLAY",
            COST=5,
        ),
        *shop_minion(30, "BG21_029", 1, 1, 1, 1, "DEMON"),
        *shop_minion(31, "BGS_061", 2, 3, 1, 1, "PIRATE"),
        *shop_minion(32, "BGS_116", 3, 1, 4, 1, "ELEMENTAL", TAUNT=1),
        "TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
    ],
    "10:00:20.0000000": [  # 买下 31
        "TAG_CHANGE Entity=2 tag=RESOURCES_USED value=3",
        "TAG_CHANGE Entity=31 tag=CONTROLLER value=1",
        "TAG_CHANGE Entity=31 tag=ZONE value=HAND",
        "TAG_CHANGE Entity=31 tag=ZONE_POSITION value=1",
    ],
    "10:00:25.0000000": [  # 打出 31
        "TAG_CHANGE Entity=31 tag=ZONE value=PLAY",
        "TAG_CHANGE Entity=31 tag=ZONE_POSITION value=1",
    ],
    "10:00:30.0000000": [  # 冻结
        "TAG_CHANGE Entity=30 tag=FROZEN value=1",
        "TAG_CHANGE Entity=32 tag=FROZEN value=1",
    ],
    "10:00:40.0000000": [  # 进入战斗
        "TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_END",
        "TAG_CHANGE Entity=GameEntity tag=TURN value=2",
        "TAG_CHANGE Entity=30 tag=ZONE value=SETASIDE",
        "TAG_CHANGE Entity=32 tag=ZONE value=SETASIDE",
        *entity(40, "TB_BaconShop_HERO_75", CONTROLLER=2, CARDTYPE="HERO", ZONE="PLAY", HEALTH=30),
        "TAG_CHANGE Entity=3 tag=HERO_ENTITY value=40",
        *shop_minion(41, "BG21_029_G", 1, 2, 2, 1, "DEMON", PREMIUM=1),
        *shop_minion(42, "BGS_116", 2, 1, 4, 1, "ELEMENTAL", TAUNT=1),
    ],
    "10:00:45.0000000": [  # 第一次攻击，金色小鬼阵亡
        "TAG_CHANGE Entity=31 tag=ATTACKING value=1",
        "TAG_CHANGE Entity=41 tag=DAMAGE value=3",
        "TAG_CHANGE Entity=41 tag=ZONE value=GRAVEYARD",
        "TAG_CHANGE Entity=31 tag=ATTACKING value=0",
    ],
    "10:00:50.0000000": [  # 战斗结束
        "TAG_CHANGE Entity=14 tag=DAMAGE value=3",
        "TAG_CHANGE Entity=3 tag=HERO_ENTITY value=11",
        "TAG_CHANGE Entity=40 tag=ZONE value=SETASIDE",
        "TAG_CHANGE Entity=42 tag=ZONE value=SETASIDE",
    ],
    "10:00:51.0000000": [  # 战斗结算完（MAIN_CLEANUP）：结果在这里定下
        "TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_CLEANUP",
    ],
    "10:01:00.0000000": [  # 第 2 回合
        "TAG_CHANGE Entity=GameEntity tag=TURN value=3",
        "TAG_CHANGE Entity=2 tag=RESOURCES value=4",
        "TAG_CHANGE Entity=2 tag=RESOURCES_USED value=0",
        "TAG_CHANGE Entity=2 tag=NEXT_OPPONENT_PLAYER_ID value=4",
        "TAG_CHANGE Entity=21 tag=COST value=4",
        *shop_minion(50, "BG23_009", 1, 2, 5, 2, "NAGA"),
        *entity(
            51,
            "BG28_500",
            CONTROLLER=2,
            CARDTYPE="BATTLEGROUND_SPELL",
            ZONE="PLAY",
            ZONE_POSITION=2,
            TECH_LEVEL=1,
        ),
        "TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
    ],
    "10:01:10.0000000": [  # 对局结束
        "TAG_CHANGE Entity=14 tag=PLAYER_LEADERBOARD_PLACE value=5",
        "TAG_CHANGE Entity=2 tag=PLAYSTATE value=LOST",
        "TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE",
    ],
}


def render() -> str:
    out = []
    for ts, lines in GROUPS.items():
        for line in lines:
            body = line if line.startswith(GAME) else POWER + line
            out.append(f"D {ts} {body}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    target = Path(__file__).with_name("mini_bg.log")
    target.write_text(render(), encoding="utf-8")
    print(f"已生成 {target}")
