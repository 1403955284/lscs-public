"""读取项目目录下的 config.toml（不进仓库）。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"


class ConfigError(Exception):
    pass


class LLMConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    provider: Literal["deepseek"] = "deepseek"
    model: str = "deepseek-flash"
    base_url: str = ""  # 留空表示 https://api.deepseek.com
    api_key: str = Field(default="", repr=False)  # 不出现在 repr / 日志里
    # 快速建议：招募开始和每次刷新后各调用一次，不开思考，2~3 秒出结果
    thinking: Literal["disabled", "low", "high"] = "disabled"
    timeout_seconds: float = Field(default=20, gt=0)
    # 快速、深度、选英雄和选项四路共用；每回合都有选项的英雄（凯瑞尔）一局大约 60 次
    max_calls_per_game: int = Field(default=100, ge=0)
    max_output_tokens: int = Field(default=1500, gt=0)  # 800 不够写 analysis
    # 深度分析：只在招募开始时和快速建议同时发出，开思考，约 20 秒
    deep_analysis: bool = True
    deep_thinking: Literal["low", "high"] = "low"
    deep_max_output_tokens: int = Field(default=16000, gt=0)
    deep_timeout_seconds: float = Field(default=75, gt=0)
    # 选英雄：看到备选英雄时让快速模型推荐一个（每局 1 次，重随时再 1 次）；
    # 思考模式、输出上限、超时沿用快速建议的
    hero_advice: bool = True
    # 选项：游戏弹出"选一个"（三连发现、黑暗之赐、饰品、技能升级）时让快速模型选一个，立即调用；
    # 参数同上
    choice_advice: bool = True
    # AI 的方案或回答不合法时，把原因发回去让它改正一次（四路都适用，每个请求最多一次，
    # 占一次调用）
    repair_invalid: bool = True

    @field_validator("api_key")
    @classmethod
    def _strip_key(cls, value: str) -> str:
        return value.strip()  # 粘贴时可能多带空格或换行


class OverlayConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    position: Literal["right", "left", "top"] = "right"
    opacity: float = Field(default=0.85, ge=0.3, le=1.0)
    font_size: int = 0  # 像素；0 表示按炉石窗口高度自动
    click_through: bool = True

    @field_validator("font_size")
    @classmethod
    def _check_font_size(cls, value: int) -> int:
        if value != 0 and not 10 <= value <= 40:
            raise ValueError("应为 0（自动）或 10~40")
        return value


Ratio = Annotated[float, Field(ge=0, le=2)]  # 内容区宽度或高度的比例
Radius = Annotated[float, Field(ge=0, le=0.5)]
ButtonPlace = tuple[Ratio, Ratio, Radius]  # [x, y, radius]


class HighlightConfig(BaseModel):
    """游戏画面上的高亮（选项建议设计文档第 8、10 节）。位置默认按 16:9 估计，用 highlight-test
    对照截图校准；x 是内容区宽度的比例，y 和尺寸是内容区高度的比例。默认值和
    overlay.layout.HighlightLayout 的一样。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    color: str = Field(default="#ffd34d", pattern=r"^#[0-9a-fA-F]{6}$")
    aspect: float = Field(default=16 / 9, gt=0.5, le=4)
    shop_y: Ratio = 0.367
    shop_pitch: Ratio = 0.128
    shop_width: Ratio = 0.11
    shop_height: Ratio = 0.15
    board_y: Ratio = 0.55
    board_pitch: Ratio = 0.128
    board_width: Ratio = 0.11
    board_height: Ratio = 0.15
    choice_y: Ratio = 0.50
    choice_pitch: Ratio = 0.42
    choice_width: Ratio = 0.33
    choice_height: Ratio = 0.50
    choice4_y: Ratio = 0.50
    choice4_pitch: Ratio = 0.34
    choice4_width: Ratio = 0.28
    choice4_height: Ratio = 0.44
    hero_y: Ratio = 0.47
    hero_pitch: Ratio = 0.36
    hero_width: Ratio = 0.27
    hero_height: Ratio = 0.46
    # 按钮：[x, y, 半径]；半径 0 表示还没校准，不画框
    upgrade: ButtonPlace = (0.412, 0.175, 0.05)
    refresh: ButtonPlace = (0.588, 0.175, 0.05)
    freeze: ButtonPlace = (0.646, 0.175, 0.05)
    hero_power: ButtonPlace = (0.593, 0.78, 0.055)


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    llm: LLMConfig = LLMConfig()
    overlay: OverlayConfig = OverlayConfig()
    highlight: HighlightConfig = HighlightConfig()


def load_config(path: Path) -> AppConfig:
    """文件不存在时返回默认值（此时没有 Key，AI 关闭）。格式或字段有误时抛出 ConfigError。"""
    if not path.exists():
        return AppConfig()
    try:
        text = path.read_text(encoding="utf-8-sig")  # Windows 记事本可能写入 BOM
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"{path.name} 无法读取，请确认它是 UTF-8 编码的文本文件") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} 不是合法的 TOML：{exc}") from None
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        # 只报字段位置和原因，不带输入值，避免把 Key 打印出来
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ConfigError(f"{path.name} 中 {where} 有误：{first['msg']}") from None


def mask_key(key: str) -> str:
    if not key:
        return "（未填写）"
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
