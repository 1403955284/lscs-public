"""真实调用 DeepSeek。默认不运行；需要 config.toml 里有 api_key，并用 `uv run pytest -m live` 运行。"""

import json

import pytest

from lscs.config import DEFAULT_CONFIG, load_config
from lscs.llm.provider import OpenAICompatProvider

pytestmark = pytest.mark.live


def test_deepseek_returns_json():
    cfg = load_config(DEFAULT_CONFIG).llm
    if not cfg.api_key:
        pytest.skip("config.toml 里没有 api_key")
    result = OpenAICompatProvider(cfg).complete_json(
        '只输出 json。示例：{"ok": true}', '请原样回复 json：{"ok": true}', timeout=30
    )
    assert json.loads(result.text).get("ok") is True
    assert result.input_tokens > 0 and result.output_tokens > 0
