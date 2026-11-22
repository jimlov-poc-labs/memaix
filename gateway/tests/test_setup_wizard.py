# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup-motorn + lokala webb-wizarden (scripts/setup_engine.py, setup_web.py).

Motorn ska skriva den AKTUELLA säkerhetsmodellen (admin: true, per-user
password_hash i både acl.yaml och .env) — regressionsskydd mot att wizarden
halkar efter koden igen. Webben testas för sitt säkerhetskontrakt:
token-krav, self-shutdown, inga hemligheter tillbaka till klienten.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bootstrap  # noqa: E402
import setup_engine as engine  # noqa: E402
import setup_web  # noqa: E402


def _answers(**overrides):
    a = engine.defaults()
    a.update({"admin_user": "jimmy", "password": "hemligt123", "project_name": "acme"})
    a.update(overrides)
    return a


# ───────────────────────────── engine ──────────────────────────────────────


def test_write_config_current_security_model(tmp_path):
    summary = engine.write_config(_answers(), tmp_path)

    acl = yaml.safe_load((tmp_path / "config" / "acl.yaml").read_text())
    user = acl["users"]["jimmy"]
    assert user["admin"] is True, "admin-användaren måste få admin: true"
    assert ":" in user["password_hash"], "per-user hash i acl.yaml (login-appens källa)"
    assert user["grants"] == {"acme": "owner", "shared": "owner"}

    env = (tmp_path / ".env").read_text()
    assert "MEMAIX_LOGIN_PASSWORD_HASH_JIMMY=" in env, "per-user hash i .env (board-källan)"
    assert "MEMAIX_LOGIN_PASSWORD_HASH=" not in env.replace(
        "MEMAIX_LOGIN_PASSWORD_HASH_JIMMY=", ""
    ), "ingen delad hash — per-user-modellen gäller"
    assert "hemligt123" not in env, "aldrig klartextlösenord på disk"
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600

    assert summary["public_url"] == "http://localhost:8080"
    assert "password" not in json.dumps(summary), "sammanfattningen är hemlighetsfri"


def test_write_config_selfhost_url(tmp_path):
    a = _answers(track=engine.TRACK_SELFHOST, domain="mcp.example.se")
    engine.write_config(a, tmp_path)
    cfg = yaml.safe_load((tmp_path / "config" / "memaix.yaml").read_text())
    assert cfg["server"]["public_url"] == "https://mcp.example.se"
    assert cfg["auth"]["issuer"] == "https://mcp.example.se/"


@pytest.mark.parametrize(
    "field,value,hint",
    [
        ("admin_user", "Jimmy!", "användarnamn"),
        ("admin_user", "j", "användarnamn"),
        ("password", "kort", "8 tecken"),
        ("project_name", "Stort Projekt", "projektnamn"),
    ],
)
def test_validate_rejects(field, value, hint, tmp_path):
    errors = engine.validate(_answers(**{field: value}))
    assert errors and any(hint.lower() in e.lower() for e in errors)


def test_validate_selfhost_requires_domain():
    assert engine.validate(_answers(track=engine.TRACK_SELFHOST, domain=""))
    assert not engine.validate(_answers(track=engine.TRACK_SELFHOST, domain="mcp.x.se"))


def test_seed_vaults_idempotent(tmp_path):
    (tmp_path / "vault-template" / "PROJECT-TEMPLATE").mkdir(parents=True)
    (tmp_path / "vault-template" / "PROJECT-TEMPLATE" / "INDEX.md").write_text("x")
    first = engine.seed_vaults(tmp_path, ["acme"])
    assert set(first) == {"acme", "shared"}
    assert (tmp_path / "vaults" / "acme" / "INDEX.md").exists()
    assert engine.seed_vaults(tmp_path, ["acme"]) == [], "körs om utan att röra befintligt"


# ───────────────────────────── setup_web ───────────────────────────────────


TOKEN = "a" * 32


@pytest.fixture()
def wizard(tmp_path, monkeypatch):
    """Setup-webben mot en tom temporär repo-rot, på en ledig port."""
    monkeypatch.setattr(setup_web, "ROOT", tmp_path)
    setup_web.Handler.token = TOKEN
    setup_web.Handler.done = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), setup_web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], tmp_path, thread
    server.shutdown()
    thread.join(timeout=5)


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


def test_token_required(wizard):
    port, _, _ = wizard
    status, _ = _request(port, "GET", "/")
    assert status == 403
    status, _ = _request(port, "GET", f"/?token={'b' * 32}")
    assert status == 403
    status, body = _request(port, "GET", f"/?token={TOKEN}")
    assert status == 200 and "Memaix" in body


def test_apply_validates_and_never_echoes_secrets(wizard):
    port, _, _ = wizard
    fields = urlencode({
        "track": "1", "admin_user": "jimmy",
        "password": "hemligt123", "password2": "annat-lösen",
    })
    status, body = _request(port, "POST", f"/apply?token={TOKEN}", fields)
    assert status == 200 and "matchar inte" in body
    assert "hemligt123" not in body and "annat-lösen" not in body


def test_apply_llm_api_choice_writes_model_block_and_never_leaks_key(wizard):
    port, root, _ = wizard
    fields = urlencode({
        "track": "1", "admin_user": "jimmy",
        "password": "hemligt123", "password2": "hemligt123",
        "project_name": "acme",
        "llm_choice": "api", "llm_provider_api": "anthropic",
        "llm_model_api": "claude-sonnet-4-5", "llm_api_key": "topphemlig-nyckel",
    })
    status, body = _request(port, "POST", f"/apply?token={TOKEN}", fields)
    assert status == 200 and "✓" in body
    assert "topphemlig-nyckel" not in body

    cfg = yaml.safe_load((root / "config" / "memaix.yaml").read_text())
    assert cfg["model"] == {
        "provider": "anthropic", "name": "claude-sonnet-4-5", "api_key_ref": "LLM_API_KEY",
    }
    assert "topphemlig-nyckel" not in (root / "config" / "memaix.yaml").read_text()
    env = (root / ".env").read_text()
    assert "LLM_API_KEY=topphemlig-nyckel" in env
    result_text = (root / ".setup-result.json").read_text()
    assert "topphemlig-nyckel" not in result_text


def test_apply_llm_endpoint_choice_writes_endpoint_no_key(wizard):
    port, root, _ = wizard
    fields = urlencode({
        "track": "1", "admin_user": "jimmy",
        "password": "hemligt123", "password2": "hemligt123",
        "project_name": "acme",
        "llm_choice": "endpoint", "llm_endpoint": "http://192.168.1.20:11434",
        "llm_model_endpoint": "qwen3-coder:30b",
    })
    status, body = _request(port, "POST", f"/apply?token={TOKEN}", fields)
    assert status == 200 and "✓" in body
    cfg = yaml.safe_load((root / "config" / "memaix.yaml").read_text())
    assert cfg["model"]["endpoint"] == "http://192.168.1.20:11434"
    assert "LLM_API_KEY" not in (root / ".env").read_text()


def test_apply_llm_api_unknown_provider_rejected_not_byo(wizard):
    port, root, _ = wizard
    fields = urlencode({
        "track": "1", "admin_user": "jimmy",
        "password": "hemligt123", "password2": "hemligt123",
        "project_name": "acme",
        "llm_choice": "api", "llm_provider_api": "skynet",
        "llm_model_api": "m", "llm_api_key": "k",
    })
    status, body = _request(port, "POST", f"/apply?token={TOKEN}", fields)
    assert status == 200
    assert "Okänd AI-leverantör" in body
    assert not (root / "config" / "acl.yaml").exists(), "ett ogiltigt val ska inte skriva config"


def test_apply_llm_choice_unknown_value_rejected_not_silent_byo(wizard):
    port, root, _ = wizard
    fields = urlencode({
        "track": "1", "admin_user": "jimmy",
        "password": "hemligt123", "password2": "hemligt123",
        "project_name": "acme",
        "llm_choice": "nonsense",
    })
    status, body = _request(port, "POST", f"/apply?token={TOKEN}", fields)
    assert status == 200
    assert "Okänt AI-val" in body
    assert not (root / "config" / "acl.yaml").exists(), (
        "okänt llm_choice ska ge ett fel, inte tyst falla tillbaka till BYO"
    )


def test_done_flag_closes_surface(wizard):
    port, _, _ = wizard
    setup_web.Handler.done = True
    status, _ = _request(port, "GET", f"/?token={TOKEN}")
    assert status == 410


def test_apply_writes_config_and_shuts_down(wizard):
    port, root, thread = wizard
    fields = urlencode({
        "track": "1", "admin_user": "jimmy",
        "password": "hemligt123", "password2": "hemligt123",
        "project_name": "acme",
    })
    status, body = _request(port, "POST", f"/apply?token={TOKEN}", fields)
    assert status == 200 and "✓" in body

    acl = yaml.safe_load((root / "config" / "acl.yaml").read_text())
    assert acl["users"]["jimmy"]["admin"] is True
    result = json.loads((root / ".setup-result.json").read_text())
    assert result["track"] == 1 and "password" not in json.dumps(result)

    # Självavstängande: servertråden dör av sig själv efter lyckad installation
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_write_config_llm_api_provider(tmp_path):
    a = _answers(llm_provider="anthropic", llm_model="claude-sonnet-4-5",
                 llm_api_key="sk-ant-test")
    engine.write_config(a, tmp_path)
    cfg = yaml.safe_load((tmp_path / "config" / "memaix.yaml").read_text())
    assert cfg["model"] == {
        "provider": "anthropic", "name": "claude-sonnet-4-5", "api_key_ref": "LLM_API_KEY",
    }
    env = (tmp_path / ".env").read_text()
    assert "LLM_API_KEY=sk-ant-test" in env
    assert "sk-ant-test" not in (tmp_path / "config" / "memaix.yaml").read_text()


def test_write_config_llm_endpoint_local_or_cloud(tmp_path):
    a = _answers(llm_provider="openai-compatible", llm_model="qwen3-coder:30b",
                 llm_endpoint="http://192.168.1.20:11434")
    engine.write_config(a, tmp_path)
    cfg = yaml.safe_load((tmp_path / "config" / "memaix.yaml").read_text())
    assert cfg["model"]["endpoint"] == "http://192.168.1.20:11434"
    assert "api_key_ref" not in cfg["model"]
    assert "LLM_API_KEY" not in (tmp_path / ".env").read_text()


def test_write_config_byo_no_model_block(tmp_path):
    engine.write_config(_answers(), tmp_path)
    cfg = yaml.safe_load((tmp_path / "config" / "memaix.yaml").read_text())
    assert "model" not in cfg


def test_validate_llm():
    assert engine.validate(_answers(llm_provider="anthropic", llm_model="m"))       # nyckel saknas
    assert engine.validate(_answers(llm_provider="ollama", llm_model="m"))          # endpoint saknas
    assert engine.validate(_answers(llm_provider="skynet", llm_model="m"))          # okänd
    assert not engine.validate(_answers(
        llm_provider="google", llm_model="gemini-2.5-pro", llm_api_key="k"))
    assert not engine.validate(_answers(
        llm_provider="vllm", llm_model="m", llm_endpoint="https://gpu.moln.se:8000"))


# ───────────────────────────── CLI-wizarden (bootstrap.py) ─────────────────


def test_cli_wizard_api_choice_never_prints_the_key(monkeypatch, capsys):
    """run_wizard() -> val [2] API-nyckel. Nyckeln matas in maskerat (getpass)
    och ska aldrig dyka upp i terminalutskriften (sammanfattningen skriver
    bara ut leverantör/modell, inte nyckeln)."""
    inputs = iter([
        "1",                    # spår: prova lokalt
        "",                     # produktnamn (default Memaix)
        "",                     # support-mejl (default)
        "",                     # admin-användarnamn (default admin)
        "2",                    # AI-val: API-nyckel
        "1",                    # leverantör: Claude (Anthropic)
        "claude-sonnet-4-5",    # modellnamn
        "",                     # projektnamn (default shared)
        "j",                    # bekräfta
    ])
    secrets_in = iter(["hemligt123", "hemligt123", "topphemlig-nyckel"])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(inputs))
    monkeypatch.setattr(bootstrap.getpass, "getpass", lambda *a, **kw: next(secrets_in))

    captured: list[dict] = []
    monkeypatch.setattr(bootstrap, "apply_answers",
                         lambda answers, **kw: captured.append(answers))

    bootstrap.run_wizard()

    assert len(captured) == 1
    answers = captured[0]
    assert answers["llm_provider"] == "anthropic"
    assert answers["llm_model"] == "claude-sonnet-4-5"
    assert answers["llm_api_key"] == "topphemlig-nyckel"

    out = capsys.readouterr().out
    assert "topphemlig-nyckel" not in out, "API-nyckeln ska aldrig synas i stdout/terminalhistorik"


# ───────────────────────────── installer ───────────────────────────────────


def test_env_is_600_even_when_it_existed_open(tmp_path):
    """.env ska aldrig ha umask-rättigheter, inte heller om en äldre fil
    låg kvar med 644 — den stramas åt innan hemligheterna skrivs."""
    env = tmp_path / ".env"
    env.write_text("OLD=1\n")
    env.chmod(0o644)
    engine.write_config(_answers(), tmp_path)
    assert (env.stat().st_mode & 0o777) == 0o600


def test_write_config_compose_profiles_and_mount_dirs(tmp_path):
    engine.write_config(_answers(), tmp_path)
    env = (tmp_path / ".env").read_text()
    assert "COMPOSE_PROFILES=hydra\n" in env
    for d in ("data", "data/login-app", "vaults"):
        assert (tmp_path / d).is_dir()

    engine.write_config(
        _answers(track=2, domain="mcp.acme.se", tunnel_provider="cloudflare"), tmp_path
    )
    assert "COMPOSE_PROFILES=hydra,tunnel\n" in (tmp_path / ".env").read_text()


def test_tunnel_token_goes_to_a_600_file_not_env(tmp_path):
    engine.write_config(
        _answers(track=2, domain="mcp.acme.se", tunnel_provider="cloudflare", tunnel_token="tok-abc"),
        tmp_path,
    )
    token = tmp_path / "cloudflared-secrets" / "tunnel.token"
    assert token.read_text() == "tok-abc"
    assert token.stat().st_mode & 0o777 == 0o600
    assert "tok-abc" not in (tmp_path / ".env").read_text()
    assert "CLOUDFLARE_TUNNEL_TOKEN" not in (tmp_path / ".env").read_text()


def test_tunnel_token_file_exists_even_without_a_token(tmp_path):
    engine.write_config(_answers(), tmp_path)
    assert (tmp_path / "cloudflared-secrets" / "tunnel.token").is_file()


def test_hydra_dev_mode_only_for_http_issuer(tmp_path):
    """Hydra kör utan --dev så fort issuern är https; bara en lokal
    http-issuer (som Hydra annars vägrar starta med) får dev-läget."""
    engine.write_config(_answers(), tmp_path)
    lokal = (tmp_path / ".env").read_text()
    assert "HYDRA_PUBLIC_URL=http://localhost" in lokal
    assert "HYDRA_SERVE_FLAGS=--dev\n" in lokal

    engine.write_config(
        _answers(track=2, domain="mcp.acme.se", tunnel_provider="cloudflare"), tmp_path
    )
    env = (tmp_path / ".env").read_text()
    assert "HYDRA_PUBLIC_URL=https://" in env
    assert "HYDRA_SERVE_FLAGS" not in env


def test_unattended_init_generates_password_file(tmp_path, monkeypatch, capsys):
    import bootstrap

    for d in ("vault-template",):
        (tmp_path / d).mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "setup_page.py").write_text(
        (SCRIPTS / "setup_page.py").read_text()
    )
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "CONFIG", tmp_path / "config")
    monkeypatch.setenv("MEMAIX_PROFILE", "trial")
    monkeypatch.delenv("MEMAIX_ADMIN_PASSWORD", raising=False)

    bootstrap.run_unattended()

    pw_file = tmp_path / "config" / "initial-admin-password"
    assert (pw_file.stat().st_mode & 0o777) == 0o600
    password = pw_file.read_text().strip()
    assert len(password) >= 20
    assert password not in capsys.readouterr().out
    acl = yaml.safe_load((tmp_path / "config" / "acl.yaml").read_text())
    assert acl["users"]["admin"]["admin"] is True


def test_unattended_rejects_unknown_profile(monkeypatch):
    import bootstrap

    monkeypatch.setenv("MEMAIX_PROFILE", "bogus")
    with pytest.raises(SystemExit):
        bootstrap.run_unattended()


def test_summary_warns_that_start_projects_have_no_file_area(tmp_path):
    summary = engine.write_config(_answers(), tmp_path)
    assert "filarea" in summary["notice"] and "require_files" in summary["notice"]
