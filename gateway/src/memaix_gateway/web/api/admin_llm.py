# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin: vilken AI driver Memaix — läs/skriv `model:`-blocket i memaix.yaml.

Schemat är CHOOSE-YOUR-LLM.md:s dokumenterade block (provider, name,
api_key_ref, endpoint). Tre lägen ur admin-UI:t:

- byo               → inget model-block alls (modellen bor i användarens AI-app)
- API-leverantör    → anthropic | openai | google | openrouter | mistral + nyckel
- egen endpoint     → openai-compatible | ollama | vllm + bas-URL
                      (täcker både LLM på lokala nätet och på en molninstans)

Nyckeln lagras aldrig i YAML: den skrivs till config/secrets/llm_api_key
(0600) och refereras som `api_key_ref: file:...` (docs/SECRETS.md). GET
returnerar aldrig nyckeln — bara has_key. Samma vaktkedja som övriga
admin-writes: user → admin → MFA, audit-loggat, atomisk skrivning.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

API_PROVIDERS = {"anthropic", "openai", "google", "openrouter", "mistral"}
ENDPOINT_PROVIDERS = {"openai-compatible", "ollama", "vllm"}
PROVIDERS = {"byo"} | API_PROVIDERS | ENDPOINT_PROVIDERS

_KEY_FILENAME = "llm_api_key"


def _require_admin_mfa(request):
    # Lat import — admin_write drar in routes; på modulnivå blir det en cykel.
    from .admin_write import _require_admin_mfa as impl

    return impl(request)


def _audit():
    from .admin_write import _audit as impl

    return impl()


def _config_dir():
    from ... import config

    return config.CONFIG_DIR


def _writer():
    """Atomisk writer för memaix.yaml — samma backup/replace-mekanik som
    acl.yaml (AclWriter är generisk YAML-IO; mutationerna här är våra egna)."""
    from ..acl_writer import AclWriter

    return AclWriter(_config_dir() / "memaix.yaml")


def _current_model() -> dict:
    from ... import config

    return (config.load().get("memaix") or {}).get("model") or {}


def _has_key(model: dict) -> bool:
    from ... import config

    ref = model.get("api_key_ref")
    if not ref:
        return False
    try:
        return bool(config.secret(ref))
    except (KeyError, ValueError, NotImplementedError):
        return False


def api_admin_llm_get(request: Request) -> JSONResponse:
    """GET /app/api/admin/llm — aktuellt AI-val, aldrig nyckeln."""
    ok, err = _require_admin_mfa(request)
    if err:
        return err
    model = _current_model()
    return JSONResponse({
        "provider": model.get("provider", "byo"),
        "name": model.get("name", ""),
        "endpoint": model.get("endpoint", ""),
        "has_key": _has_key(model),
        "providers": sorted(PROVIDERS),
    })


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _is_http_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _validation_error(provider, name: str, endpoint: str) -> str | None:
    """Första valideringsfelet (i samma ordning som tidigare) eller None."""
    if provider not in PROVIDERS:
        return f"provider must be one of {sorted(PROVIDERS)}"
    if provider != "byo" and not name:
        return "name (modellnamn) krävs"
    if provider in ENDPOINT_PROVIDERS and not _is_http_url(endpoint):
        return "endpoint (http(s)://…) krävs för lokal/egen LLM"
    if endpoint and not _is_http_url(endpoint):
        return "endpoint måste vara en http(s)-URL"
    return None


def _store_api_key(api_key: str) -> str:
    """Skriv nyckeln till config/secrets (0600) och returnera `file:`-referensen."""
    secrets_dir = _config_dir() / "secrets"
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    key_path = secrets_dir / _KEY_FILENAME
    key_path.write_text(api_key.strip() + "\n", encoding="utf-8")
    key_path.chmod(0o600)
    return f"file:{key_path}"


def _model_block(provider: str, name: str, endpoint: str, key_ref: str) -> dict:
    model = {"provider": provider, "name": name}
    if endpoint:
        model["endpoint"] = endpoint
    if key_ref:
        model["api_key_ref"] = key_ref
    return model


def _key_state(api_key: str, key_ref: str) -> str:
    if api_key:
        return "ny"
    return "behållen" if key_ref else "ingen"


async def api_admin_llm_set(request: Request) -> JSONResponse:
    """PUT /app/api/admin/llm {provider, name?, endpoint?, api_key?}"""
    ok, err = _require_admin_mfa(request)
    if err:
        return err
    user, _acl = ok
    try:
        body = await request.json()
    except Exception:
        return _bad_request("bad_request")

    provider = body.get("provider")
    name = (body.get("name") or "").strip()
    endpoint = (body.get("endpoint") or "").strip()
    api_key = body.get("api_key") or ""

    invalid = _validation_error(provider, name, endpoint)
    if invalid:
        return _bad_request(invalid)

    current = _current_model()
    writer = _writer()

    if provider == "byo":
        writer.set_top_level("model", None)
        _audit().log(user, "-", "admin_set_llm", True, "provider=byo (model-block borttaget)")
        return JSONResponse({"ok": True, "provider": "byo"})

    key_ref = _store_api_key(api_key) if api_key else current.get("api_key_ref", "")
    if provider in API_PROVIDERS and not key_ref:
        return _bad_request("api_key krävs för en API-leverantör")

    writer.set_top_level("model", _model_block(provider, name, endpoint, key_ref))

    _audit().log(
        user, "-", "admin_set_llm", True,
        f"provider={provider} name={name} endpoint={endpoint or '-'} "
        f"key={_key_state(api_key, key_ref)}",
    )
    return JSONResponse({
        "ok": True, "provider": provider, "name": name,
        "endpoint": endpoint, "has_key": bool(key_ref),
    })


async def api_admin_llm_test(request: Request) -> JSONResponse:
    """POST /app/api/admin/llm/test — minimalt riktigt anrop mot SPARAT AI-val.

    Svaret är hemlighetsfritt (LLMError saneras i klientlagret). Körs i
    trådpool så gatewayns event-loop inte blockeras av leverantörslatens."""
    ok, err = _require_admin_mfa(request)
    if err:
        return err
    user, _acl = ok

    import anyio

    from ... import config
    from ...llm import LLMClient, LLMError, LLMNotConfigured

    try:
        client = LLMClient.from_config(config.load())
        result = await anyio.to_thread.run_sync(client.test)
    except LLMNotConfigured:
        return JSONResponse(
            {"error": "inget AI-val sparat (BYO) — spara ett val först"}, status_code=400
        )
    except LLMError as exc:
        _audit().log(user, "-", "admin_test_llm", False, str(exc)[:200])
        return JSONResponse({"error": str(exc)}, status_code=502)

    _audit().log(
        user, "-", "admin_test_llm", True,
        f"{result['provider']}/{result['model']} {result['latency_ms']}ms",
    )
    return JSONResponse(result)
