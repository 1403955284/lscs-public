"""大模型提供商。目前只有 DeepSeek（OpenAI 兼容格式）；以后加 Claude 只需新增一个实现。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from lscs.config import LLMConfig

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
STATUS_MESSAGES = {402: "DeepSeek 余额不足", 429: "请求太频繁，稍后再试"}


@dataclass(frozen=True)
class LLMResult:
    text: str
    elapsed: float  # 秒
    input_tokens: int = 0
    cached_tokens: int = 0  # 命中 DeepSeek 上下文缓存的输入 token
    output_tokens: int = 0


ErrorKind = Literal["auth", "timeout", "other"]


class LLMError(Exception):
    """调用失败。message 是给用户看的中文摘要，不包含 Key 或请求内容。"""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class LLMProvider(Protocol):
    def complete_json(self, system: str, user: str, timeout: float) -> LLMResult: ...


class OpenAICompatProvider:
    def __init__(self, cfg: LLMConfig, client=None, clock: Callable[[], float] = time.monotonic):
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=cfg.api_key, base_url=cfg.base_url or DEEPSEEK_BASE_URL, max_retries=0
            )
        self._cfg = cfg
        self._client = client
        self._clock = clock

    def request_kwargs(self, system: str, user: str, timeout: float) -> dict:
        kwargs: dict = {
            "model": self._cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self._cfg.max_output_tokens,
            # 单次连接/读取超时，不是总时长；过期结果由调度器按编号丢弃
            "timeout": timeout,
        }
        if self._cfg.thinking == "disabled":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = self._cfg.thinking
        return kwargs

    def complete_json(self, system: str, user: str, timeout: float) -> LLMResult:
        import openai

        start = self._clock()
        try:
            response = self._client.chat.completions.create(
                **self.request_kwargs(system, user, timeout)
            )
        except openai.AuthenticationError:
            raise LLMError("auth", "API Key 无效") from None
        except openai.APITimeoutError:
            raise LLMError("timeout", "AI 超时") from None
        except openai.APIStatusError as exc:
            message = STATUS_MESSAGES.get(exc.status_code, f"AI 调用失败（HTTP {exc.status_code}）")
            raise LLMError("other", message) from None
        except openai.APIError as exc:
            raise LLMError("other", f"AI 调用失败（{type(exc).__name__}）") from None
        try:
            text = response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            raise LLMError("other", "AI 返回格式异常") from None
        usage = response.usage
        return LLMResult(
            text=text,
            elapsed=self._clock() - start,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            cached_tokens=getattr(usage, "prompt_cache_hit_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


class FakeProvider:
    """测试用：按顺序返回预设的回答文本，或抛出预设的异常。"""

    def __init__(self, responses: list[str | Exception], elapsed: float = 1.0) -> None:
        self._responses = list(responses)
        self._elapsed = elapsed
        self.calls: list[tuple[str, str, float]] = []

    def complete_json(self, system: str, user: str, timeout: float) -> LLMResult:
        self.calls.append((system, user, timeout))
        item = self._responses.pop(0) if self._responses else ""
        if isinstance(item, Exception):
            raise item
        return LLMResult(text=item, elapsed=self._elapsed, input_tokens=100, output_tokens=20)
