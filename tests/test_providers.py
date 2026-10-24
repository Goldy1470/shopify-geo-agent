"""Provider contract tests: each official-API provider over MockTransport.

No test talks to a real API. Each provider must:
- report unavailable() without a key (config or env),
- parse its documented payload shape into {text, sources},
- raise ProviderError on transport/HTTP failures,
- label every answer "API-based estimate".
"""

import httpx
import pytest

from app.providers import PROVIDERS, available_providers
from app.providers.anthropic import AnthropicProvider
from app.providers.base import ProviderError, ProviderUnavailable
from app.providers.gemini import GeminiProvider
from app.providers.openai_responses import OpenAIResponsesProvider
from app.providers.perplexity import PerplexityProvider

KEY_VARS = ["OPENAI_API_KEY", "PERPLEXITY_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"]


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for v in KEY_VARS:
        monkeypatch.delenv(v, raising=False)


def test_registry_has_four_official_providers():
    assert set(PROVIDERS) == {"openai", "perplexity", "gemini", "anthropic"}


def test_no_keys_means_no_engines():
    assert available_providers({}) == {}


def test_key_in_config_or_env_enables(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "sk")
    assert "perplexity" in available_providers({})
    monkeypatch.delenv("PERPLEXITY_API_KEY")
    assert "perplexity" in available_providers({"perplexity_api_key": "cfg-value"})


async def test_perplexity_happy_path():
    payload = {
        "choices": [{"message": {"content": "Get the Kestrel for $289."}}],
        "citations": ["https://www.outsidegear.example/reviews", "https://buyshop.example/kestrel"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.perplexity.ai"
        assert request.read().startswith(b'{"model":"sonar"')
        return httpx.Response(200, json=payload)

    prov = PerplexityProvider({"perplexity_api_key": "pk"}, transport=httpx.MockTransport(handler))
    out = await prov.ask("best shell jacket")
    assert out["text"] == "Get the Kestrel for $289."
    assert [s["url"] for s in out["sources"]] == [
        "https://www.outsidegear.example/reviews",
        "https://buyshop.example/kestrel",
    ]
    assert out["engine_label"] == "API-based estimate"


async def test_perplexity_http_error_wraps():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    prov = PerplexityProvider({"perplexity_api_key": "pk"}, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        await prov.ask("q")


def test_perplexity_missing_key_raises():
    import asyncio

    prov = PerplexityProvider({})
    with pytest.raises(ProviderUnavailable):
        asyncio.run(prov.ask("q"))


async def test_openai_responses_happy_path():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "1. Aurora Pack $184 (recommended)",
                        "annotations": [
                            {"type": "url_citation", "url": "https://a.example/1", "title": "A"},
                            {"type": "url_citation", "url": "https://a.example/1", "title": "A-dupe"},
                            {"type": "url_citation", "url": "https://b.example/2", "title": "B"},
                        ],
                    }
                ],
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b"web_search" in body, "must request the official web_search tool"
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json=payload)

    prov = OpenAIResponsesProvider({"openai_api_key": "sk"}, transport=httpx.MockTransport(handler))
    out = await prov.ask("best travel backpack")
    assert out["text"] == "1. Aurora Pack $184 (recommended)"
    urls = [s["url"] for s in out["sources"]]
    assert urls == ["https://a.example/1", "https://b.example/2"], "citations deduped, order kept"


async def test_openai_responses_error_wraps():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    prov = OpenAIResponsesProvider({"openai_api_key": "sk"}, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        await prov.ask("q")


async def test_gemini_grounding_chunks_become_sources():
    payload = {
        "candidates": [
            {
                "content": {"parts": [{"text": "answer"}]},
                "groundingMetadata": {
                    "groundingChunks": [
                        {"web": {"uri": "https://one.example/x", "title": "One"}},
                        {"web": {"uri": "https://one.example/x", "title": "One again"}},
                        {"web": {"uri": "https://two.example/y", "title": "Two"}},
                    ]
                },
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "key=gk" in str(request.url), "API key passed as query param"
        body = request.read()
        assert b"google_search" in body, "grounding tool must be requested"
        return httpx.Response(200, json=payload)

    prov = GeminiProvider({"gemini_api_key": "gk"}, transport=httpx.MockTransport(handler))
    out = await prov.ask("q")
    assert out["text"] == "answer"
    assert [s["url"] for s in out["sources"]] == ["https://one.example/x", "https://two.example/y"]


async def test_anthropic_web_search_results_become_sources():
    payload = {
        "content": [
            {"type": "text", "text": "The Aurora is a great pick."},
            {"type": "web_search_result", "content": [{"url": "https://c.example/a", "title": "CA"}]},
            {"type": "text", "text": " Not much else."},
            {"type": "web_search_result", "content": [{"url": "https://c.example/a", "title": "CA-dupe"}]},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b"web_search_20250306" in body, "official server-side search tool"
        assert request.headers.get("x-api-key") == "ak"
        return httpx.Response(200, json=payload)

    prov = AnthropicProvider({"anthropic_api_key": "ak"}, transport=httpx.MockTransport(handler))
    out = await prov.ask("q")
    assert out["text"] == "The Aurora is a great pick. Not much else."
    assert [s["url"] for s in out["sources"]] == ["https://c.example/a"]
