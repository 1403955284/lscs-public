from types import SimpleNamespace

import httpx2
import openai
import pytest

from lscs.config import LLMConfig
from lscs.llm.provider import FakeProvider, LLMError, OpenAICompatProvider

REQUEST = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")


class StubClient:
    def __init__(self, result=None, error=None):
        self.kwargs = None
        self._result, self._error = result, error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._result


def response(text, prompt=1200, cached=800, completion=150):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(
            prompt_tokens=prompt, prompt_cache_hit_tokens=cached, completion_tokens=completion
        ),
    )


def ticking_clock(*values):
    it = iter(values)
    return lambda: next(it)


def test_request_without_thinking():
    client = StubClient(result=response('{"actions": []}'))
    provider = OpenAICompatProvider(
        LLMConfig(api_key="k"), client=client, clock=ticking_clock(10.0, 13.5)
    )
    result = provider.complete_json("系统", "局面", timeout=20)
    assert client.kwargs == {
        "model": "deepseek-flash",
        "messages": [{"role": "system", "content": "系统"}, {"role": "user", "content": "局面"}],
        "response_format": {"type": "json_object"},
        "max_tokens": 1500,
        "timeout": 20,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert result.text == '{"actions": []}'
    assert (result.elapsed, result.input_tokens, result.cached_tokens, result.output_tokens) == (
        3.5,
        1200,
        800,
        150,
    )


def test_request_with_thinking_effort():
    client = StubClient(result=response("{}"))
    OpenAICompatProvider(LLMConfig(api_key="k", thinking="low"), client=client).complete_json(
        "s", "u", timeout=60
    )
    assert client.kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert client.kwargs["reasoning_effort"] == "low"


def test_per_call_thinking_and_max_tokens_override_the_config():
    client = StubClient(result=response("{}"))
    provider = OpenAICompatProvider(LLMConfig(api_key="k"), client=client)
    provider.complete_json("s", "u", timeout=75, thinking="low", max_tokens=16000)
    assert client.kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert client.kwargs["reasoning_effort"] == "low"
    assert client.kwargs["max_tokens"] == 16000
    thinking = OpenAICompatProvider(LLMConfig(api_key="k", thinking="high"), client=client)
    thinking.complete_json("s", "u", timeout=20, thinking="disabled", max_tokens=1500)
    assert client.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in client.kwargs
    assert client.kwargs["max_tokens"] == 1500


def test_empty_content_becomes_empty_text():
    client = StubClient(result=response(None))
    assert (
        OpenAICompatProvider(LLMConfig(api_key="k"), client=client).complete_json("s", "u", 5).text
        == ""
    )


def test_malformed_response_becomes_llm_error():
    client = StubClient(result=SimpleNamespace(choices=[], usage=None))
    with pytest.raises(LLMError) as exc:
        OpenAICompatProvider(LLMConfig(api_key="k"), client=client).complete_json("s", "u", 5)
    assert exc.value.kind == "other"


@pytest.mark.parametrize(
    ("error", "kind", "message"),
    [
        (
            openai.AuthenticationError(
                "bad key", response=httpx2.Response(401, request=REQUEST), body=None
            ),
            "auth",
            "API Key 无效",
        ),
        (openai.APITimeoutError(request=REQUEST), "timeout", "AI 超时"),
        (
            openai.RateLimitError(
                "slow down", response=httpx2.Response(429, request=REQUEST), body=None
            ),
            "other",
            "请求太频繁，稍后再试",
        ),
        (
            openai.APIStatusError(
                "Insufficient Balance", response=httpx2.Response(402, request=REQUEST), body=None
            ),
            "other",
            "DeepSeek 余额不足",
        ),
        (
            openai.InternalServerError(
                "oops", response=httpx2.Response(500, request=REQUEST), body=None
            ),
            "other",
            "AI 调用失败（HTTP 500）",
        ),
        (openai.APIConnectionError(request=REQUEST), "other", "AI 调用失败（APIConnectionError）"),
    ],
)
def test_errors_are_mapped(error, kind, message):
    provider = OpenAICompatProvider(LLMConfig(api_key="k"), client=StubClient(error=error))
    with pytest.raises(LLMError) as exc:
        provider.complete_json("s", "u", 5)
    assert (exc.value.kind, str(exc.value)) == (kind, message)


def test_fake_provider_replays_script():
    fake = FakeProvider(['{"a": 1}', LLMError("timeout", "AI 超时")])
    assert fake.complete_json("s", "u", 3).text == '{"a": 1}'
    with pytest.raises(LLMError):
        fake.complete_json("s", "u", 3)
    assert fake.complete_json("s", "u", 3, thinking="low", max_tokens=9).text == ""  # 脚本用完
    assert [c[2] for c in fake.calls] == [3, 3, 3]
    assert [(c.thinking, c.max_tokens) for c in fake.calls] == [(None, None)] * 2 + [("low", 9)]
    assert fake.calls[0] == ("s", "u", 3, None, None, ())


def test_turns_follow_the_system_and_user_messages():
    client = StubClient(result=response("{}"))
    provider = OpenAICompatProvider(LLMConfig(api_key="k"), client=client)
    turns = (("assistant", '{"actions": []}'), ("user", "你刚才的方案不合法：第 1 步：铸币不够。"))
    provider.complete_json("系统", "局面", timeout=20, turns=turns)
    assert client.kwargs["messages"] == [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "局面"},
        {"role": "assistant", "content": '{"actions": []}'},
        {"role": "user", "content": "你刚才的方案不合法：第 1 步：铸币不够。"},
    ]
    provider.complete_json("系统", "局面", timeout=20)  # 不传时只有两条，和以前一样
    assert [m["role"] for m in client.kwargs["messages"]] == ["system", "user"]


def test_fake_provider_records_the_turns():
    fake = FakeProvider(["{}", "{}"])
    fake.complete_json("s", "u", 3)
    fake.complete_json("s", "u", 3, turns=[("assistant", "坏"), ("user", "改正")])
    assert [c.turns for c in fake.calls] == [(), (("assistant", "坏"), ("user", "改正"))]
