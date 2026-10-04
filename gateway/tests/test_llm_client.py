# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lager 1 — LLM-klienten (FEATURE-LLM-ENGINE.md Fas 1).

Säkerhetskontraktet testas hårdast: nyckeln i header (aldrig URL), sanerade
fel utan nyckel, egress-vakt mot länk-lokalt/metadata (men LAN tillåtet —
lokal Ollama är ett huvudfall), inga redirects.
"""

from __future__ import annotations

import json

import httpx
import pytest

from memaix_gateway.llm import LLMClient, LLMError, LLMNotConfigured
from memaix_gateway.llm import client as client_mod


def _capture(response_json, status=200):
    """MockTransport som fångar requesten och svarar med given JSON."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content or b"{}")
        return httpx.Response(status, json=response_json)

    return seen, httpx.MockTransport(handler)


@pytest.fixture()
def post_via(monkeypatch):
    """Styr om httpx.post till en MockTransport och returnera fångsten."""

    def _install(response_json, status=200):
        seen, transport = _capture(response_json, status)

        def fake_post(url, **kw):
            with httpx.Client(transport=transport) as c:
                return c.post(url, json=kw.get("json"), headers=kw.get("headers"))

        monkeypatch.setattr(client_mod.httpx, "post", fake_post)
        return seen

    return _install


def test_anthropic_request_shape(post_via):
    seen = post_via({"content": [{"type": "text", "text": "ok"}]})
    c = LLMClient({"provider": "anthropic", "name": "claude-sonnet-4-5"}, "sk-ant-x")
    out = c.complete([
        {"role": "system", "content": "var kort"},
        {"role": "user", "content": "hej"},
    ])
    assert out["content"] == "ok" and out["tool_calls"] == []
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant-x"
    assert "sk-ant-x" not in seen["url"], "nyckel i header — aldrig i URL"
    assert seen["body"]["system"] == "var kort"
    assert all(m["role"] != "system" for m in seen["body"]["messages"])


@pytest.mark.parametrize(
    "provider,endpoint,expected_url",
    [
        ("openai", "", "https://api.openai.com/v1/chat/completions"),
        ("openrouter", "", "https://openrouter.ai/api/v1/chat/completions"),
        ("mistral", "", "https://api.mistral.ai/v1/chat/completions"),
        # Ollama på LAN, angiven utan /v1 → normaliseras
        ("ollama", "http://192.168.1.20:11434", "http://192.168.1.20:11434/v1/chat/completions"),
        # egen molninstans, redan med /v1 → dubblas inte
        ("vllm", "https://gpu.moln.se:8000/v1", "https://gpu.moln.se:8000/v1/chat/completions"),
    ],
)
def test_openai_compatible_url_join(post_via, provider, endpoint, expected_url):
    seen = post_via({"choices": [{"message": {"content": "svar"}}]})
    c = LLMClient({"provider": provider, "name": "m", "endpoint": endpoint}, "nyckel")
    assert c.complete([{"role": "user", "content": "x"}])["content"] == "svar"
    assert seen["url"] == expected_url
    assert seen["headers"]["authorization"] == "Bearer nyckel"


def test_local_endpoint_without_key_sends_no_auth_header(post_via):
    seen = post_via({"choices": [{"message": {"content": "svar"}}]})
    c = LLMClient({"provider": "ollama", "name": "qwen3", "endpoint": "http://10.0.0.5:11434"}, None)
    c.complete([{"role": "user", "content": "x"}])
    assert "authorization" not in seen["headers"]


def test_google_key_in_header_never_url(post_via):
    seen = post_via({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    c = LLMClient({"provider": "google", "name": "gemini-2.5-pro"}, "AIza-hemlig")
    assert c.complete([{"role": "user", "content": "hej"}])["content"] == "ok"
    assert "AIza-hemlig" not in seen["url"], "aldrig ?key= i URL:en"
    assert seen["headers"]["x-goog-api-key"] == "AIza-hemlig"
    assert ":generateContent" in seen["url"]


def test_provider_error_sanitized_no_key(post_via):
    post_via({"error": {"message": "bad key sk-ant-x"}}, status=401)
    c = LLMClient({"provider": "anthropic", "name": "m"}, "riktig-nyckel-abc")
    with pytest.raises(LLMError) as exc:
        c.complete([{"role": "user", "content": "x"}])
    assert exc.value.status == 401
    assert "riktig-nyckel-abc" not in str(exc.value), "vår nyckel aldrig i felet"
    assert len(str(exc.value)) < 400, "leverantörssvar trunkeras"


@pytest.mark.parametrize("bad", [
    "http://169.254.169.254/latest",           # AWS/GCP metadata-IP (länk-lokal)
    "http://metadata.google.internal/x",        # metadata-värdnamn
])
def test_egress_guard_blocks_metadata(bad):
    c = LLMClient({"provider": "openai-compatible", "name": "m", "endpoint": bad}, None)
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "x"}])


def test_egress_guard_allows_private_lan(post_via):
    # Privat LAN är POÄNGEN med lokal modell — får inte blockeras.
    post_via({"choices": [{"message": {"content": "ok"}}]})
    c = LLMClient({"provider": "ollama", "name": "m", "endpoint": "http://192.168.86.34:11434"}, None)
    assert c.complete([{"role": "user", "content": "x"}])["content"] == "ok"


def test_from_config_byo_raises_not_configured():
    with pytest.raises(LLMNotConfigured):
        LLMClient.from_config({"memaix": {}})


def test_test_call_returns_secretfree_summary(post_via):
    post_via({"choices": [{"message": {"content": "ok"}}]})
    c = LLMClient({"provider": "openai", "name": "gpt-5"}, "sk-hemlig")
    result = c.test()
    assert result["ok"] is True and result["provider"] == "openai"
    assert isinstance(result["latency_ms"], int)
    assert "sk-hemlig" not in json.dumps(result)


# ───────────────────── Fas 2: verktygsanrop, neutralt format ─────────────────


def test_anthropic_tool_roundtrip_translation(post_via):
    seen = post_via({
        "content": [{"type": "tool_use", "id": "tu_1", "name": "calendar_list",
                     "input": {"project": "acme"}}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    c = LLMClient({"provider": "anthropic", "name": "m"}, "k")
    tools = [{"name": "calendar_list", "description": "lista", "input_schema": {"type": "object"}}]
    history = [
        {"role": "user", "content": "vad har jag i kalendern?"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "tu_0", "name": "whoami", "args": {}}]},
        {"role": "tool", "call_id": "tu_0", "name": "whoami", "content": "{\"user\": \"jimmy\"}"},
    ]
    out = c.complete(history, tools=tools)
    # neutralt → anthropic: tool_use/tool_result-block, system utanför messages
    sent = seen["body"]["messages"]
    assert sent[1]["content"][0]["type"] == "tool_use"
    assert sent[2]["content"][0]["type"] == "tool_result"
    assert sent[2]["content"][0]["tool_use_id"] == "tu_0"
    assert seen["body"]["tools"][0]["input_schema"] == {"type": "object"}
    # anthropic → neutralt
    assert out["tool_calls"] == [{"id": "tu_1", "name": "calendar_list",
                                  "args": {"project": "acme"}}]
    assert out["usage"] == 15


def test_openai_tool_roundtrip_translation(post_via):
    seen = post_via({
        "choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "calendar_list", "arguments": "{\"project\": \"acme\"}"}}
        ]}}],
        "usage": {"total_tokens": 42},
    })
    c = LLMClient({"provider": "openai", "name": "m"}, "k")
    tools = [{"name": "calendar_list", "description": "lista", "input_schema": {"type": "object"}}]
    history = [
        {"role": "user", "content": "kalendern?"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_0", "name": "whoami", "args": {}}]},
        {"role": "tool", "call_id": "call_0", "name": "whoami", "content": "{}"},
    ]
    out = c.complete(history, tools=tools)
    sent = seen["body"]["messages"]
    assert sent[1]["tool_calls"][0]["function"]["name"] == "whoami"
    assert sent[2] == {"role": "tool", "tool_call_id": "call_0", "content": "{}"}
    assert seen["body"]["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert out["tool_calls"][0]["args"] == {"project": "acme"} and out["usage"] == 42


def test_google_has_no_tool_support_in_v1(post_via):
    post_via({"candidates": [{"content": {"parts": [{"text": "svar"}]}}]})
    c = LLMClient({"provider": "google", "name": "gemini-2.5-pro"}, "k")
    assert c.supports_tools is False
    out = c.complete([{"role": "user", "content": "x"}],
                     tools=[{"name": "t", "description": "", "input_schema": {}}])
    assert out["content"] == "svar" and out["tool_calls"] == []


# ───────── Karakterisering av _anthropic / _openai_compatible (S3776-sanering) ─────────


def test_anthropic_assistant_text_and_calls_become_blocks_in_order(post_via):
    seen = post_via({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                     "usage": {}})
    c = LLMClient({"provider": "anthropic", "name": "m"}, None)
    out = c.complete([
        {"role": "system", "content": "s1"},
        {"role": "system", "content": "s2"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "jag kollar",
         "tool_calls": [{"id": "1", "name": "x", "args": {"a": 1}},
                        {"id": "2", "name": "y", "args": {}}]},
        {"role": "assistant", "content": "ren text"},
    ])
    body = seen["body"]
    assert body["system"] == "s1\ns2"
    assert body["messages"][0] == {"role": "user", "content": "q"}
    assert body["messages"][1] == {"role": "assistant", "content": [
        {"type": "text", "text": "jag kollar"},
        {"type": "tool_use", "id": "1", "name": "x", "input": {"a": 1}},
        {"type": "tool_use", "id": "2", "name": "y", "input": {}},
    ]}
    assert body["messages"][2] == {"role": "assistant", "content": "ren text"}
    assert seen["headers"]["x-api-key"] == ""  # saknad nyckel -> tom header
    assert out == {"content": "ab", "tool_calls": [], "usage": 0}


def test_anthropic_without_system_tools_or_content(post_via):
    seen = post_via({"content": [{"type": "tool_use", "id": "t", "name": "n"}],
                     "usage": {"input_tokens": "2", "output_tokens": 3}})
    c = LLMClient({"provider": "anthropic", "name": "m", "endpoint": "http://h:1/"}, "k")
    out = c.complete([{"role": "user", "content": "q"}], max_tokens=9)
    assert seen["url"] == "http://h:1/v1/messages"
    assert "system" not in seen["body"] and "tools" not in seen["body"]
    assert seen["body"]["max_tokens"] == 9
    assert out == {"content": None, "tool_calls": [{"id": "t", "name": "n", "args": {}}],
                   "usage": 5}


def test_anthropic_assistant_tool_call_without_text_has_only_tool_blocks(post_via):
    seen = post_via({})  # tomt svar: inga content/usage
    c = LLMClient({"provider": "anthropic", "name": "m"}, "k")
    out = c.complete([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "x", "args": {}}]},
    ])
    assert [b["type"] for b in seen["body"]["messages"][0]["content"]] == ["tool_use"]
    assert out == {"content": None, "tool_calls": [], "usage": 0}


def test_openai_requires_endpoint_for_unknown_provider(post_via):
    c = LLMClient({"provider": "ollama", "name": "m"}, None)
    with pytest.raises(LLMError) as exc:
        c.complete([{"role": "user", "content": "x"}])
    assert str(exc.value) == "ollama kräver en endpoint-URL"


def test_openai_empty_choices_is_error(post_via):
    post_via({"choices": []})
    c = LLMClient({"provider": "openai", "name": "m"}, "k")
    with pytest.raises(LLMError) as exc:
        c.complete([{"role": "user", "content": "x"}])
    assert str(exc.value) == "openai: tomt svar (inga choices)"
    post_via({})
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "x"}])


def test_openai_tool_call_argument_edge_cases_and_auth(post_via):
    seen = post_via({
        "choices": [{"message": {"content": "hej", "tool_calls": [
            {"id": "1", "function": {"name": "a", "arguments": "inte json"}},
            {"function": {"name": "b"}},
            {"id": "3"},
        ]}}],
        "usage": {},
    })
    c = LLMClient({"provider": "openrouter", "name": "m"}, "k")
    out = c.complete([
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "c", "tool_calls": [{"id": "9", "name": "z", "args": {"q": "å"}}]},
    ], tools=[{"name": "n", "description": "d", "input_schema": {"type": "object"}}])
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer k"
    assert seen["body"]["messages"][1] == {
        "role": "assistant", "content": "c",
        "tool_calls": [{"id": "9", "type": "function",
                        "function": {"name": "z", "arguments": '{"q": "\\u00e5"}'}}]}
    assert seen["body"]["tools"] == [{"type": "function", "function": {
        "name": "n", "description": "d", "parameters": {"type": "object"}}}]
    assert out["content"] == "hej" and out["usage"] == 0
    assert out["tool_calls"] == [
        {"id": "1", "name": "a", "args": {}},
        {"id": "", "name": "b", "args": {}},
        {"id": "3", "name": "", "args": {}},
    ]


def test_openai_empty_content_becomes_none_and_v1_not_doubled(post_via):
    seen = post_via({"choices": [{"message": {"content": ""}}], "usage": {"total_tokens": 7}})
    c = LLMClient({"provider": "vllm", "name": "m", "endpoint": "http://h:8000/v1"}, None)
    out = c.complete([{"role": "user", "content": "x"}])
    assert seen["url"] == "http://h:8000/v1/chat/completions"
    assert "authorization" not in seen["headers"]
    assert "tools" not in seen["body"]
    assert out == {"content": None, "tool_calls": [], "usage": 7}
