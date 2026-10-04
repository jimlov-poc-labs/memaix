# SPDX-License-Identifier: AGPL-3.0-or-later
"""Characterization tests for ``api_admin_llm_set`` (PUT /app/api/admin/llm).

Pins every validation branch, status code, error message and audit line, and
that an API key never appears in a response, the YAML, the audit log or the
application log."""

from __future__ import annotations

import logging

import pytest
import yaml
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from memaix_gateway.web.api import admin_llm as mod

SECRET = "sk-super-secret-123"
URL = "/app/api/admin/llm"


class _Audit:
    def __init__(self):
        self.entries = []

    def log(self, *a, **kw):
        self.entries.append((a, kw))


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    (tmp_path / "memaix.yaml").write_text('server:\n  public_url: "https://mcp.example.se"\n')
    audit = _Audit()
    denied = {"on": False}

    def _gate(request):
        if denied["on"]:
            return None, JSONResponse({"error": "forbidden"}, status_code=403)
        return ("root", None), None

    monkeypatch.setattr(mod, "_require_admin_mfa", _gate)
    monkeypatch.setattr(mod, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "_audit", lambda: audit)
    monkeypatch.setattr(
        mod, "_current_model",
        lambda: (yaml.safe_load((tmp_path / "memaix.yaml").read_text()) or {}).get("model") or {},
    )
    app = Starlette(routes=[Route(URL, mod.api_admin_llm_set, methods=["PUT"])])
    client = TestClient(app, raise_server_exceptions=False)
    return client, tmp_path, audit, denied


def _model(root):
    return (yaml.safe_load((root / "memaix.yaml").read_text()) or {}).get("model")


def test_gate_denial_is_returned_untouched(rig):
    client, root, audit, denied = rig
    denied["on"] = True
    r = client.put(URL, json={"provider": "anthropic", "name": "m", "api_key": SECRET})
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert _model(root) is None and audit.entries == []
    assert not (root / "secrets").exists()


def test_invalid_json_is_bad_request(rig):
    client, root, audit, _ = rig
    r = client.put(URL, content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json() == {"error": "bad_request"}
    assert _model(root) is None and audit.entries == []


def test_non_object_body_is_server_error_and_writes_nothing(rig):
    # Quirk locked as-is: a JSON array body is not validated, .get() blows up.
    client, root, audit, _ = rig
    r = client.put(URL, json=["anthropic"])
    assert r.status_code == 500
    assert _model(root) is None and audit.entries == []


@pytest.mark.parametrize("body", [{}, {"provider": None}, {"provider": "skynet"}, {"provider": ""}])
def test_unknown_or_missing_provider_is_400_with_sorted_list(rig, body):
    client, root, audit, _ = rig
    r = client.put(URL, json=body)
    assert r.status_code == 400
    assert r.json() == {"error": f"provider must be one of {sorted(mod.PROVIDERS)}"}
    assert _model(root) is None and audit.entries == []


@pytest.mark.parametrize("name", [None, "", "   "])
def test_non_byo_requires_name(rig, name):
    client, root, _, _ = rig
    body = {"provider": "anthropic", "api_key": SECRET}
    if name is not None:
        body["name"] = name
    r = client.put(URL, json=body)
    assert r.status_code == 400
    assert r.json() == {"error": "name (modellnamn) krävs"}
    assert not (root / "secrets").exists(), "ingen nyckel skrivs när valideringen faller"
    assert _model(root) is None


@pytest.mark.parametrize("provider", sorted(mod.ENDPOINT_PROVIDERS))
@pytest.mark.parametrize("endpoint", [None, "", "  ", "ftp://x", "localhost:11434"])
def test_endpoint_providers_require_http_endpoint(rig, provider, endpoint):
    client, root, _, _ = rig
    body = {"provider": provider, "name": "m"}
    if endpoint is not None:
        body["endpoint"] = endpoint
    r = client.put(URL, json=body)
    assert r.status_code == 400
    assert r.json() == {"error": "endpoint (http(s)://…) krävs för lokal/egen LLM"}
    assert _model(root) is None


@pytest.mark.parametrize("endpoint", ["https://llm.example.se/v1", "http://10.0.0.5:8000"])
def test_endpoint_provider_accepts_http_and_https(rig, endpoint):
    client, root, _, _ = rig
    r = client.put(URL, json={"provider": "vllm", "name": "m", "endpoint": endpoint})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "provider": "vllm", "name": "m", "endpoint": endpoint, "has_key": False}
    assert _model(root) == {"provider": "vllm", "name": "m", "endpoint": endpoint}


def test_api_provider_with_non_http_endpoint_is_400(rig):
    client, root, _, _ = rig
    r = client.put(URL, json={
        "provider": "anthropic", "name": "m", "api_key": SECRET, "endpoint": "ftp://proxy",
    })
    assert r.status_code == 400
    assert r.json() == {"error": "endpoint måste vara en http(s)-URL"}
    assert _model(root) is None
    assert not (root / "secrets").exists(), "ingen nyckel skrivs när valideringen faller"


def test_api_provider_with_http_endpoint_stores_it(rig):
    client, root, _, _ = rig
    r = client.put(URL, json={
        "provider": "openai", "name": "m", "api_key": SECRET, "endpoint": "https://proxy.example.se",
    })
    assert r.status_code == 200 and r.json()["endpoint"] == "https://proxy.example.se"
    assert _model(root)["endpoint"] == "https://proxy.example.se"


def test_api_provider_without_key_is_400_and_writes_nothing(rig):
    client, root, audit, _ = rig
    r = client.put(URL, json={"provider": "google", "name": "m"})
    assert r.status_code == 400
    assert r.json() == {"error": "api_key krävs för en API-leverantör"}
    assert _model(root) is None and audit.entries == []


def test_new_key_is_stored_in_0600_file_and_never_leaks(rig, caplog):
    client, root, audit, _ = rig
    with caplog.at_level(logging.DEBUG):
        r = client.put(URL, json={
            "provider": "anthropic", "name": "  claude  ", "api_key": f"  {SECRET}  \n",
        })
    assert r.status_code == 200
    assert r.json() == {
        "ok": True, "provider": "anthropic", "name": "claude", "endpoint": "", "has_key": True,
    }
    key_file = root / "secrets" / "llm_api_key"
    assert key_file.read_text() == SECRET + "\n"
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert (root / "secrets").stat().st_mode & 0o777 == 0o700
    assert _model(root) == {"provider": "anthropic", "name": "claude", "api_key_ref": f"file:{key_file}"}
    for haystack in (r.text, (root / "memaix.yaml").read_text(), str(audit.entries), caplog.text):
        assert SECRET not in haystack
    assert audit.entries == [(
        ("root", "-", "admin_set_llm", True, "provider=anthropic name=claude endpoint=- key=ny"), {},
    )]
    assert (root / "memaix.yaml").read_text().count("public_url") == 1, "övriga sektioner orörda"


def test_existing_secrets_dir_is_reused_and_key_overwritten(rig):
    client, root, _, _ = rig
    (root / "secrets").mkdir()
    (root / "secrets" / "llm_api_key").write_text("old\n")
    r = client.put(URL, json={"provider": "mistral", "name": "m", "api_key": SECRET})
    assert r.status_code == 200
    assert (root / "secrets" / "llm_api_key").read_text() == SECRET + "\n"


def test_whitespace_only_key_counts_as_a_key(rig):
    # Quirk locked as-is: "   " is truthy, so an empty key file is written.
    client, root, audit, _ = rig
    r = client.put(URL, json={"provider": "openai", "name": "m", "api_key": "   "})
    assert r.status_code == 200 and r.json()["has_key"] is True
    assert (root / "secrets" / "llm_api_key").read_text() == "\n"
    assert audit.entries[0][0][4].endswith("key=ny")


def test_existing_key_ref_is_kept_when_no_new_key(rig):
    client, root, audit, _ = rig
    (root / "memaix.yaml").write_text(
        "model:\n  provider: anthropic\n  name: old\n  api_key_ref: env:MY_KEY\n"
    )
    r = client.put(URL, json={"provider": "anthropic", "name": "new"})
    assert r.status_code == 200 and r.json()["has_key"] is True
    assert _model(root) == {"provider": "anthropic", "name": "new", "api_key_ref": "env:MY_KEY"}
    assert audit.entries[0][0][4] == "provider=anthropic name=new endpoint=- key=behållen"
    assert not (root / "secrets").exists()


def test_new_key_replaces_existing_ref(rig):
    client, root, _, _ = rig
    (root / "memaix.yaml").write_text(
        "model:\n  provider: anthropic\n  name: old\n  api_key_ref: env:MY_KEY\n"
    )
    r = client.put(URL, json={"provider": "anthropic", "name": "new", "api_key": SECRET})
    assert r.status_code == 200
    assert _model(root)["api_key_ref"] == f"file:{root / 'secrets' / 'llm_api_key'}"


def test_endpoint_provider_without_key_audits_ingen(rig):
    client, root, audit, _ = rig
    r = client.put(URL, json={
        "provider": "ollama", "name": " qwen ", "endpoint": " http://lan:11434 ",
    })
    assert r.status_code == 200
    assert r.json() == {
        "ok": True, "provider": "ollama", "name": "qwen",
        "endpoint": "http://lan:11434", "has_key": False,
    }
    assert "api_key_ref" not in _model(root)
    assert audit.entries[0][0][4] == "provider=ollama name=qwen endpoint=http://lan:11434 key=ingen"


def test_endpoint_provider_with_key_stores_ref(rig):
    client, root, audit, _ = rig
    r = client.put(URL, json={
        "provider": "openai-compatible", "name": "m", "endpoint": "https://x.se", "api_key": SECRET,
    })
    assert r.status_code == 200 and r.json()["has_key"] is True
    assert _model(root)["api_key_ref"].startswith("file:")
    assert SECRET not in r.text and SECRET not in str(audit.entries)


def test_byo_removes_model_block_and_ignores_submitted_key(rig):
    client, root, audit, _ = rig
    (root / "memaix.yaml").write_text(
        "server:\n  a: 1\nmodel:\n  provider: anthropic\n  name: m\n  api_key_ref: env:K\n"
    )
    r = client.put(URL, json={"provider": "byo", "api_key": SECRET, "name": "", "endpoint": ""})
    assert r.status_code == 200 and r.json() == {"ok": True, "provider": "byo"}
    cfg = yaml.safe_load((root / "memaix.yaml").read_text())
    assert "model" not in cfg and cfg["server"] == {"a": 1}
    assert not (root / "secrets").exists(), "byo skriver aldrig nyckelfil"
    assert audit.entries == [(("root", "-", "admin_set_llm", True, "provider=byo (model-block borttaget)"), {})]
    assert SECRET not in str(audit.entries) and SECRET not in r.text


def test_byo_ignores_invalid_endpoint(rig):
    # byo passes the endpoint validation only when it is empty/valid; a bad
    # endpoint still fails the generic http(s) check before byo is handled.
    client, _, _, _ = rig
    r = client.put(URL, json={"provider": "byo", "endpoint": "ftp://x"})
    assert r.status_code == 400
    assert r.json() == {"error": "endpoint måste vara en http(s)-URL"}
