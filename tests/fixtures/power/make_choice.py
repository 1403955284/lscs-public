"""生成 choice.log：人工构造的、带几种"选一个"的极简单人酒馆战棋（仿 mini_bg.log）。

运行：uv run python tests/fixtures/power/make_choice.py

剧情（实体 ID）：
- 1 游戏实体；2 我（PlayerID=1）；3 Bob（PlayerID=2，AI）
- 14 我的英雄（凯瑞尔），15 英雄技能"定罪"；11 Bob 的英雄；12 排行榜上的对手
- 第 1 回合开始（STEP=MAIN_START_TRIGGERS）：定罪的升级选项 id=2，60/61/62 三个姿态，
  选 61 → 三个都进 GRAVEYARD → MAIN_ACTION（招募开始）；商店 30、31
- 招募中：三连奖励 70 发现 id=3（选项顺序 72、71、73，不按编号）；没选完又来一组：
  海礁探险者 74 的发现 id=4（75、76、77）
- 没有 SendChoices，但 76 进了手牌：视为选完
- id=5：CountMax=2 的一组（不产生 choice）
- 黑暗之赐 80 的发现 id=6：81 带"鹰身人之爪"（附魔 83 的 CardID 后来才公开，创建者是赐福法术 84），
  82 带附魔 85（创建者 86 不是赐福法术：记附魔自己的卡牌 ID）；选 82
- id=7：招募中的一组 MULLIGAN（不产生 choice）
- id=8：一组 2 选 1 还没选，新的一局（CREATE_GAME）开始
"""

from pathlib import Path

POWER = "GameState.DebugPrintPower() - "
GAME = "GameState.DebugPrintGame() - "
CHOICES = "GameState.DebugPrintEntityChoices() - "
SEND = "GameState.SendChoices() - "
CHOSEN = "GameState.DebugPrintEntitiesChosen() - "
ME = "Me#1234"


def entity(entity_id: int, card_id: str, **tags: object) -> list[str]:
    lines = [f"FULL_ENTITY - Creating ID={entity_id} CardID={card_id}"]
    lines += [f"    tag={tag} value={value}" for tag, value in tags.items()]
    return lines


def minion(entity_id, card_id, atk, health, tier, race, controller=1, zone="SETASIDE", **extra):
    return entity(
        entity_id,
        card_id,
        CONTROLLER=controller,
        CARDTYPE="MINION",
        ZONE=zone,
        ATK=atk,
        HEALTH=health,
        TECH_LEVEL=tier,
        CARDRACE=race,
        **extra,
    )


def ref(entity_id: int, card_id: str, zone: str = "SETASIDE") -> str:
    return f"[entityName=X id={entity_id} zone={zone} zonePos=0 cardId={card_id} player=1]"


def offer(choice_id, source, options, most=1, kind="GENERAL"):
    """source：(实体编号, 卡牌 ID, 区域)，None 表示 GameEntity；options：[(实体编号, 卡牌 ID)]。"""
    lines = [
        CHOICES
        + f"id={choice_id} Player={ME} TaskList={choice_id + 100} ChoiceType={kind}"
        + f" CountMin=1 CountMax={most}",
        CHOICES + "  Source=" + ("GameEntity" if source is None else ref(*source)),
    ]
    lines += [CHOICES + f"  Entities[{i}]=" + ref(*option) for i, option in enumerate(options)]
    return lines


def send(choice_id, entity_id, card_id):
    return [
        SEND + f"id={choice_id} ChoiceType=GENERAL",
        SEND + "  m_chosenEntities[0]=" + ref(entity_id, card_id),
    ]


def chosen(choice_id, entity_id, card_id):
    return [
        CHOSEN + f"id={choice_id} Player={ME} EntitiesCount=1",
        CHOSEN + "  Entities[0]=" + ref(entity_id, card_id),
    ]


def header(game_entity: int) -> list[str]:
    g, me, bob = game_entity, game_entity + 1, game_entity + 2
    return [
        "CREATE_GAME",
        f"    GameEntity EntityID={g}",
        "        tag=CARDTYPE value=GAME",
        "        tag=ZONE value=PLAY",
        "        tag=TURN value=0",
        f"    Player EntityID={me} PlayerID=1 GameAccountId=[hi=1 lo=1]",
        "        tag=PLAYER_ID value=1",
        "        tag=CONTROLLER value=1",
        "        tag=CARDTYPE value=PLAYER",
        "        tag=HERO_ENTITY value=14",
        "        tag=PLAYER_TECH_LEVEL value=1",
        f"    Player EntityID={bob} PlayerID=2 GameAccountId=[hi=0 lo=0]",
        "        tag=PLAYER_ID value=2",
        "        tag=CONTROLLER value=2",
        "        tag=CARDTYPE value=PLAYER",
        "        tag=HERO_ENTITY value=11",
    ]


STANCES = [(60, "BG21_HERO_000p_t1"), (61, "BG21_HERO_000_Buddyt"), (62, "BG21_HERO_000_Buddyt2")]

GROUPS: dict[str, list[str]] = {
    "10:00:00.0000000": [
        *header(1),
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
            14,
            "BG21_HERO_000",
            CONTROLLER=1,
            CARDTYPE="HERO",
            ZONE="PLAY",
            HEALTH=30,
            PLAYER_ID=1,
            PLAYER_LEADERBOARD_PLACE=1,
        ),
        *entity(15, "BG21_HERO_000p", CONTROLLER=1, CARDTYPE="HERO_POWER", ZONE="PLAY"),
        GAME + "BuildNumber=999999",
        GAME + "GameType=GT_BATTLEGROUNDS",
        GAME + "FormatType=FT_WILD",
        GAME + f"PlayerID=1, PlayerName={ME}",
        GAME + "PlayerID=2, PlayerName=The Innkeeper",
    ],
    "10:00:10.0000000": [  # 第 1 回合开始，还没到 MAIN_ACTION
        "TAG_CHANGE Entity=GameEntity tag=TURN value=1",
        "TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_START_TRIGGERS",
        "TAG_CHANGE Entity=2 tag=RESOURCES value=3",
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
        *minion(30, "BG21_029", 1, 1, 1, "DEMON", controller=2, zone="PLAY", ZONE_POSITION=1),
        *minion(31, "BGS_061", 3, 1, 1, "PIRATE", controller=2, zone="PLAY", ZONE_POSITION=2),
    ],
    "10:00:10.1000000": [  # 定罪的升级选项
        *(
            line
            for entity_id, card_id in STANCES
            for line in entity(
                entity_id, card_id, CONTROLLER=1, CARDTYPE="SPELL", ZONE="SETASIDE", CREATOR=15
            )
        ),
        *offer(2, (15, "BG21_HERO_000p", "PLAY"), STANCES),
    ],
    "10:00:15.0000000": send(2, 61, "BG21_HERO_000_Buddyt"),
    "10:00:15.3000000": [
        *chosen(2, 61, "BG21_HERO_000_Buddyt"),
        *(f"TAG_CHANGE Entity={entity_id} tag=ZONE value=GRAVEYARD" for entity_id, _ in STANCES),
    ],
    "10:00:16.0000000": ["TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION"],
    "10:00:20.0000000": [  # 三连奖励的发现
        *entity(70, "TB_BaconShop_Triples_01", CONTROLLER=1, CARDTYPE="SPELL", ZONE="PLAY"),
        *minion(71, "BGS_116", 1, 4, 2, "ELEMENTAL", CREATOR=70),
        *minion(72, "BG23_009", 2, 5, 2, "NAGA", CREATOR=70, TAUNT=1),
        *minion(73, "BGS_061", 3, 1, 2, "PIRATE", CREATOR=70),
        *offer(
            3,
            (70, "TB_BaconShop_Triples_01", "PLAY"),
            [(72, "BG23_009"), (71, "BGS_116"), (73, "BGS_061")],
        ),
    ],
    "10:00:22.0000000": [  # 没选完又来一组：海礁探险者的发现
        *minion(74, "BG23_016", 2, 2, 2, "MURLOC", zone="PLAY", ZONE_POSITION=1),
        *minion(75, "BG21_029", 1, 1, 1, "DEMON", CREATOR=74),
        *minion(76, "BGS_061", 3, 1, 1, "PIRATE", CREATOR=74),
        *minion(77, "BGS_116", 1, 4, 1, "ELEMENTAL", CREATOR=74),
        *offer(4, (74, "BG23_016", "PLAY"), [(75, "BG21_029"), (76, "BGS_061"), (77, "BGS_116")]),
    ],
    "10:00:25.0000000": [  # 没有 SendChoices（比如重连），但 76 进了手牌
        "TAG_CHANGE Entity=76 tag=ZONE value=HAND",
        "TAG_CHANGE Entity=76 tag=ZONE_POSITION value=1",
    ],
    "10:00:27.0000000": [  # 可以选两个的一组
        *minion(78, "BG21_029", 1, 1, 1, "DEMON", CREATOR=74),
        *minion(79, "BGS_061", 3, 1, 1, "PIRATE", CREATOR=74),
        *offer(5, (74, "BG23_016", "PLAY"), [(78, "BG21_029"), (79, "BGS_061")], most=2),
    ],
    "10:00:28.0000000": [
        *send(5, 78, "BG21_029"),
        "TAG_CHANGE Entity=78 tag=ZONE value=HAND",
    ],
    "10:00:30.0000000": [  # 黑暗之赐
        *entity(80, "BG36_MidGameEffect_010", CONTROLLER=1, CARDTYPE="SPELL", ZONE="SETASIDE"),
        *minion(81, "BG21_029", 1, 1, 1, "DEMON", CREATOR=80, DIVINE_SHIELD=1, WINDFURY=1),
        *minion(82, "BGS_061", 3, 11, 1, "PIRATE", CREATOR=80),
        *entity(84, "BG36_MidGameEffect_000t13", CONTROLLER=1, CARDTYPE="SPELL", ZONE="SETASIDE"),
        *entity(
            83, "", CONTROLLER=1, CARDTYPE="ENCHANTMENT", ZONE="SETASIDE", ATTACHED=81, CREATOR=84
        ),
        "SHOW_ENTITY - Updating Entity=83 CardID=EDR_100t13e",
        "    tag=CARDTYPE value=ENCHANTMENT",
        *entity(86, "BG21_029", CONTROLLER=1, CARDTYPE="MINION", ZONE="SETASIDE"),
        *entity(
            85, "", CONTROLLER=1, CARDTYPE="ENCHANTMENT", ZONE="SETASIDE", ATTACHED=82, CREATOR=86
        ),
        "SHOW_ENTITY - Updating Entity=85 CardID=BG36_MidGameEffect_000t2e2",
        "    tag=CARDTYPE value=ENCHANTMENT",
        *offer(6, (80, "BG36_MidGameEffect_010", "SETASIDE"), [(81, "BG21_029"), (82, "BGS_061")]),
    ],
    "10:00:34.0000000": send(6, 82, "BGS_061"),
    "10:00:34.3000000": [
        *chosen(6, 82, "BGS_061"),
        "TAG_CHANGE Entity=82 tag=ZONE value=HAND",
        "TAG_CHANGE Entity=81 tag=TRANSIENT_ENTITY value=1",
    ],
    "10:00:35.0000000": offer(7, None, [(30, "BG21_029"), (31, "BGS_061")], kind="MULLIGAN"),
    "10:00:40.0000000": [  # 一组还没选，对局就换了
        *minion(90, "BG21_029", 1, 1, 1, "DEMON", CREATOR=74),
        *minion(91, "BGS_061", 3, 1, 1, "PIRATE", CREATOR=74),
        *offer(8, (74, "BG23_016", "PLAY"), [(90, "BG21_029"), (91, "BGS_061")]),
    ],
    "10:01:00.0000000": header(100),
}


def render() -> str:
    out = []
    for ts, lines in GROUPS.items():
        for line in lines:
            body = line if line.startswith(("GameState.",)) else POWER + line
            out.append(f"D {ts} {body}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    target = Path(__file__).with_name("choice.log")
    target.write_text(render(), encoding="utf-8")
    print(f"已生成 {target}")
