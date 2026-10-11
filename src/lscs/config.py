"""读取项目目录下的 config.toml（不进仓库）。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

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
    max_calls_per_game: int = Field(default=80, ge=0)  # 快速和深度两路共用
    max_output_tokens: int = Field(default=1500, gt=0)  # 800 不够写 analysis
    # 深度分析：只在招募开始时和快速建议同时发出，开思考，约 20 秒
    deep_analysis: bool = True
    deep_thinking: Literal["low", "high"] = "low"
    deep_max_output_tokens: int = Field(default=16000, gt=0)
    deep_timeout_seconds: float = Field(default=75, gt=0)

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


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    llm: LLMConfig = LLMConfig()
    overlay: OverlayConfig = OverlayConfig()


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
