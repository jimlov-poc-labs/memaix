# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lager 1 — provider-adaptrar bakom ETT anrop (FEATURE-LLM-ENGINE.md).

Tre adaptrar täcker åtta leverantörsval:
- anthropic            → Messages API
- google               → Gemini generateContent
- openai-compatible    → chat/completions: openai, openrouter, mistral,
                         ollama, vllm och egen endpoint — samma format,
                         olika bas-URL.

Säkerhetskontraktet (specens §Säkerhet, byggs här — inte efteråt):
- API-nyckeln läses per anrop via config.secret(), skickas i header (aldrig
  i URL), hålls inte i klientobjektet och förekommer aldrig i fel/loggar.
- Egress: inga redirects följs; länk-lokala/metadata-adresser vägras.
  Privata LAN-adresser är däremot tillåtna — lokal Ollama är ett huvudfall.
- Leverantörsfel saneras och trunkeras innan de når anroparen.

Inga leverantörs-SDK:er — httpx (befintligt beroende) rakt mot REST.
Verktygsanrop (tools) kommer i Fas 2; signaturen är förberedd.
"""

from __future__ import annotations

import ipaddress
import json
import time
from urllib.parse import urlparse

import httpx

_TIMEOUT = 30.0
_ERROR_BODY_MAX = 300

# Default-endpoints för API-leverantörer; endpoint-leverantörer (ollama/vllm/
# openai-compatible) kräver explicit URL (setup_engine/admin_llm validerar).
_DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
}

_BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


class LLMError(RuntimeError):
    """Sanerat leverantörsfel — säkert att visa för admin (aldrig nyckeln)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class LLMNotConfigured(LLMError):
    """Inget model-block — BYO-läge; motorn är avstängd per design."""

    def __init__(self):
        super().__init__("inget model-block i memaix.yaml (BYO-läge)")


def _guard_endpoint(url: str) -> None:
    """Vägra länk-lokala/metadata-mål. Privat LAN är tillåtet (lokal Ollama)."""
    host = urlparse(url).hostname or ""
    if host.lower() in _BLOCKED_HOSTS:
        raise LLMError(f"endpoint-värd tillåts inte: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # värdnamn — DNS-upplösning sker i httpx; kända metadatanamn stoppade ovan
    if ip.is_link_local:
        raise LLMError(f"länk-lokal endpoint tillåts inte: {host}")


def _sanitize(body: str) -> str:
    return body.replace("\n", " ")[:_ERROR_BODY_MAX]


def _anthropic_message(m: dict) -> dict:
    """Ett neutralt meddelande (ej system) → Messages API-format."""
    if m["role"] == "assistant" and m.get("tool_calls"):
        blocks = []
        if m.get("content"):
            blocks.append({"type": "text", "text": m["content"]})
        blocks += [
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
            for c in m["tool_calls"]
        ]
        return {"role": "assistant", "content": blocks}
    if m["role"] == "tool":
        return {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": m["call_id"], "content": m["content"],
        }]}
    return {"role": m["role"], "content": m["content"]}


def _anthropic_payload(model: str, messages: list, max_tokens: int, tools: list | None) -> dict:
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    converted = [_anthropic_message(m) for m in messages if m["role"] != "system"]
    payload = {"model": model, "max_tokens": max_tokens, "messages": converted}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = [
            {"name": t["name"], "description": t["description"],
             "input_schema": t["input_schema"]}
            for t in tools
        ]
    return payload


def _anthropic_result(data: dict) -> dict:
    blocks = data.get("content") or []
    usage = data.get("usage") or {}
    return {
        "content": "".join(b.get("text", "") for b in blocks if b.get("type") == "text") or None,
        "tool_calls": [
            {"id": b["id"], "name": b["name"], "args": b.get("input") or {}}
            for b in blocks if b.get("type") == "tool_use"
        ],
        "usage": int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
    }


def _openai_message(m: dict) -> dict:
    """Ett neutralt meddelande → chat/completions-format."""
    if m["role"] == "assistant" and m.get("tool_calls"):
        return {
            "role": "assistant", "content": m.get("content"),
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                for c in m["tool_calls"]
            ],
        }
    if m["role"] == "tool":
        return {"role": "tool", "tool_call_id": m["call_id"], "content": m["content"]}
    return {"role": m["role"], "content": m["content"]}


def _openai_parse_args(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def _openai_tool_calls(msg: dict) -> list:
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        calls.append({
            "id": c.get("id", ""),
            "name": fn.get("name", ""),
            "args": _openai_parse_args(fn.get("arguments")),
        })
    return calls


class LLMClient:
    """Ett anrop — complete(messages) → text. Adapter väljs av model-blocket."""

    def __init__(self, model_cfg: dict, api_key: str | None):
        self.provider = model_cfg.get("provider", "")
        self.model = model_cfg.get("name", "")
        self.endpoint = (model_cfg.get("endpoint") or "").rstrip("/")
        self._key = api_key  # hålls bara under objektets (korta) livstid

    @classmethod
    def from_config(cls, cfg: dict) -> "LLMClient":
        """Bygg från config.load()-resultatet. LLMNotConfigured i BYO-läge."""
        model = (cfg.get("memaix") or {}).get("model") or {}
        if not model.get("provider"):
            raise LLMNotConfigured()
        key = None
        ref = model.get("api_key_ref")
        if ref:
            from .. import config as _config

            try:
                key = _config.secret(ref)
            except (KeyError, ValueError) as exc:
                raise LLMError(f"API-nyckelns ref gick inte att lösa: {type(exc).__name__}")
        return cls(model, key)

    # ------------------------------------------------------------------

    @property
    def supports_tools(self) -> bool:
        """Gemini-adapterns verktygsformat avviker mest — i v1 kör google
        text-utan-verktyg (specens öppna fråga; agentloopen varnar admin)."""
        return self.provider != "google"

    def complete(self, messages: list, max_tokens: int = 1024, tools: list | None = None) -> dict:
        """En icke-strömmande tur. Neutralt format in och ut (Fas 2):

        messages: [{"role": "system"|"user", "content": str}
                   | {"role": "assistant", "content": str|None,
                      "tool_calls": [{"id","name","args"}]}
                   | {"role": "tool", "call_id", "name", "content": str}]
        tools:    [{"name", "description", "input_schema"}] (JSON Schema)
        →         {"content": str|None, "tool_calls": [...], "usage": int}

        Adaptrarna äger översättningen till leverantörens format — agent-
        loopen ser aldrig leverantörsspecifika strukturer."""
        if tools and not self.supports_tools:
            tools = None
        if self.provider == "anthropic":
            return self._anthropic(messages, max_tokens, tools)
        if self.provider == "google":
            return self._google(messages, max_tokens)
        return self._openai_compatible(messages, max_tokens, tools)

    def test(self) -> dict:
        """Minimalt riktigt anrop — admin-knappen och doctor. Hemlighetsfritt svar."""
        started = time.monotonic()
        reply = self.complete(
            [{"role": "user", "content": "Svara med exakt ordet: ok"}], max_tokens=16
        )
        return {
            "ok": True,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "reply": (reply["content"] or "").strip()[:80],
        }

    # ------------------------------------------------------------------

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        _guard_endpoint(url)
        try:
            resp = httpx.post(
                url, json=payload, headers=headers,
                timeout=_TIMEOUT, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            # httpx-fel innehåller URL:en men aldrig headers — säkert att visa.
            raise LLMError(f"kunde inte nå {self.provider}: {type(exc).__name__}")
        if resp.status_code >= 400:
            raise LLMError(
                f"{self.provider} svarade {resp.status_code}: {_sanitize(resp.text)}",
                status=resp.status_code,
            )
        try:
            return resp.json()
        except ValueError:
            raise LLMError(f"{self.provider} svarade inte med JSON")

    def _anthropic(self, messages: list, max_tokens: int, tools: list | None = None) -> dict:
        payload = _anthropic_payload(self.model, messages, max_tokens, tools)
        base = self.endpoint or "https://api.anthropic.com"
        data = self._post(
            f"{base}/v1/messages",
            {"x-api-key": self._key or "", "anthropic-version": "2023-06-01"},
            payload,
        )
        return _anthropic_result(data)

    def _openai_base(self) -> str:
        base = self.endpoint or _DEFAULT_ENDPOINTS.get(self.provider, "")
        if not base:
            raise LLMError(f"{self.provider} kräver en endpoint-URL")
        # Ollama m.fl. anges ofta utan /v1 — normalisera i stället för att gissa fel.
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return base

    def _openai_compatible(self, messages: list, max_tokens: int, tools: list | None = None) -> dict:
        base = self._openai_base()
        payload: dict = {
            "model": self.model, "max_tokens": max_tokens,
            "messages": [_openai_message(m) for m in messages],
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"], "description": t["description"],
                    "parameters": t["input_schema"],
                }}
                for t in tools
            ]
        headers = {}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        data = self._post(f"{base}/chat/completions", headers, payload)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.provider}: tomt svar (inga choices)")
        msg = choices[0].get("message") or {}
        usage = data.get("usage") or {}
        return {
            "content": msg.get("content") or None,
            "tool_calls": _openai_tool_calls(msg),
            "usage": int(usage.get("total_tokens", 0)),
        }

    def _google(self, messages: list, max_tokens: int) -> dict:
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user",
             "parts": [{"text": m["content"]}]}
            for m in messages if m["role"] != "system"
        ]
        payload: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        base = self.endpoint or "https://generativelanguage.googleapis.com"
        data = self._post(
            f"{base}/v1beta/models/{self.model}:generateContent",
            {"x-goog-api-key": self._key or ""},  # header — aldrig ?key= i URL/loggar
            payload,
        )
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("google: tomt svar (inga candidates)")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        meta = data.get("usageMetadata") or {}
        return {
            "content": "".join(p.get("text", "") for p in parts) or None,
            "tool_calls": [],
            "usage": int(meta.get("totalTokenCount", 0)),
        }
