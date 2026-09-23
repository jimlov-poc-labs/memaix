"""Memaix setup-motor — config-generering delad av CLI- och webb-wizarden.

SPDX-License-Identifier: AGPL-3.0-or-later

En motor, två bärare (SETUP-UI.md): bootstrap.py --init (CLI) och
setup_web.py (lokal webb) samlar in samma svar och anropar write_config()
här. All kunskap om VAD som skrivs bor på ett enda ställe, så bärarna
aldrig kan glida isär.

Skriver den aktuella säkerhetsmodellen:
- admin-användaren får `admin: true` (implicit owner + /app/admin)
- lösenordshash per användare i BÅDE acl.yaml (login-appens källa) och
  .env MEMAIX_LOGIN_PASSWORD_HASH_<USER> (board/webb-cookiens källa)
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

TRACK_TRIAL, TRACK_SELFHOST, TRACK_MANAGED = 1, 2, 3

# CHOOSE-YOUR-LLM.md: BYO = inget model-block; API-leverantörer kräver nyckel;
# endpoint-lägen (LLM på lokala nätet ELLER egen molninstans) kräver bas-URL.
LLM_API_PROVIDERS = ("anthropic", "openai", "google", "openrouter", "mistral")
LLM_ENDPOINT_PROVIDERS = ("openai-compatible", "ollama", "vllm")

_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def pbkdf2_hash(password: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}:{key.hex()}"


def fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def write_secret_file(path: Path, content: str) -> None:
    """Skriv en fil som aldrig är läsbar för andra, inte ens ett ögonblick.

    write_text() + chmod() lämnar ett fönster där filen har umask-rättigheter
    (typiskt 644). Här skapas den med 600 direkt, och en befintlig fil
    stramas åt innan innehållet skrivs.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    if not hasattr(os, "fchmod"):  # Windows
        path.chmod(0o600)


def compose_profiles(a: dict) -> str:
    """Compose-profiler för spåret. Hydra-profilen bär gateway + OAuth-stacken
    och behövs i alla spår (gatewayen kör HTTP och doctor kräver Hydra)."""
    profiles = ["hydra"]
    if a.get("tunnel_provider") in ("cloudflare", "cloudflare-quick"):
        profiles.append("tunnel")
    return ",".join(profiles)


def defaults() -> dict:
    return {
        "track": TRACK_TRIAL,
        "name": "Memaix",
        "support_email": "support@example.com",
        "domain": "",
        "tunnel_provider": "none",
        "tunnel_token": "",
        "admin_user": "admin",
        "password": "",
        "project_name": "shared",
        "llm_provider": "byo",
        "llm_model": "",
        "llm_endpoint": "",
        "llm_api_key": "",
    }


def validate(a: dict) -> list[str]:
    """Valideringsfel på människospråk — tom lista = OK att skriva."""
    errors = []
    if a["track"] not in (TRACK_TRIAL, TRACK_SELFHOST, TRACK_MANAGED):
        errors.append("Ogiltigt spår.")
    if not _USERNAME_RE.match(a["admin_user"]):
        errors.append(
            "Admin-användarnamnet måste vara 2–32 tecken: små bokstäver, siffror, "
            "understreck; börja med bokstav (blir del av ett miljövariabelnamn)."
        )
    if len(a["password"]) < 8:
        errors.append("Lösenordet måste vara minst 8 tecken.")
    if not _PROJECT_RE.match(a["project_name"]):
        errors.append("Projektnamn: 2–32 tecken, små bokstäver/siffror/bindestreck.")
    if a["track"] in (TRACK_SELFHOST, TRACK_MANAGED):
        if not a["domain"] or "." not in a["domain"] or "/" in a["domain"]:
            errors.append("Self-host kräver en domän (t.ex. mcp.företag.se).")

    provider = a.get("llm_provider", "byo")
    if provider != "byo":
        if provider not in LLM_API_PROVIDERS + LLM_ENDPOINT_PROVIDERS:
            errors.append("Okänd AI-leverantör.")
        elif not a.get("llm_model"):
            errors.append("AI-valet kräver ett modellnamn.")
        elif provider in LLM_API_PROVIDERS and not a.get("llm_api_key"):
            errors.append(f"AI-leverantören {provider} kräver en API-nyckel.")
        elif provider in LLM_ENDPOINT_PROVIDERS and not str(
            a.get("llm_endpoint", "")
        ).startswith(("http://", "https://")):
            errors.append("Lokal/egen LLM kräver en endpoint-URL (http(s)://…).")
    return errors


def public_url(a: dict) -> str:
    if a["track"] == TRACK_TRIAL:
        return "http://localhost:8080"
    return f"https://{a['domain']}"


def write_config(a: dict, root: Path) -> dict:
    """Skriv config/brand.yaml, config/memaix.yaml, config/acl.yaml och .env.

    Förutsätter att validate(a) är tom. Returnerar en hemlighetsfri
    sammanfattning (för wizardens kvittosida och .setup-result.json).
    """
    config = root / "config"
    config.mkdir(exist_ok=True)
    url = public_url(a)
    issuer = url.rstrip("/") + "/"
    admin = a["admin_user"]
    project = a["project_name"]
    password_hash = pbkdf2_hash(a["password"])

    (config / "brand.yaml").write_text(
        f'name: "{a["name"]}"\n'
        f'tagline: "Bring your own AI. Own your memory."\n'
        f'support_email: "{a["support_email"]}"\n'
        f'primary_color: "#4f46e5"\n'
        f'logo_path: ""\n'
    )

    model_block = ""
    if a.get("llm_provider", "byo") != "byo":
        model_block = (
            f'\nmodel:\n'
            f'  provider: {a["llm_provider"]}\n'
            f'  name: "{a["llm_model"]}"\n'
        )
        if a.get("llm_api_key"):
            model_block += "  api_key_ref: LLM_API_KEY\n"
        if a.get("llm_endpoint"):
            model_block += f'  endpoint: "{a["llm_endpoint"]}"\n'

    (config / "memaix.yaml").write_text(
        f'server:\n'
        f'  bind: "0.0.0.0:8080"\n'
        f'  public_url: "{url}"\n'
        f'\n'
        f'auth:\n'
        f'  issuer: "{issuer}"\n'
        f'  resource_server_url: "{issuer}"\n'
        f'\n'
        f'onboarding:\n'
        f'  enabled: true\n'
        f'{model_block}'
    )

    projects = {project: f"/srv/vaults/{project}"}
    if project != "shared":
        projects["shared"] = "/srv/vaults/shared"
    grants = "".join(f"      {p}: owner\n" for p in projects)
    project_blocks = "".join(
        f"  {p}:\n    vault: {vault}\n    allow_send: false\n"
        for p, vault in projects.items()
    )
    (config / "acl.yaml").write_text(
        f"users:\n"
        f"  {admin}:\n"
        f"    # admin: true ger implicit owner på alla projekt + tillgång till /app/admin\n"
        f"    admin: true\n"
        f"    password_hash: \"{password_hash}\"\n"
        f"    oauth_subjects:\n"
        f"      - {admin}\n"
        f"    grants:\n"
        f"{grants}"
        f"\n"
        f"projects:\n"
        f"{project_blocks}"
    )

    env_lines = [
        "# Genererat av Memaix setup — ändra inte för hand",
        f"CLOUDFLARE_TUNNEL_TOKEN={a['tunnel_token']}",
        f"HYDRA_DB_PASSWORD={secrets.token_hex(32)}",
        f"HYDRA_SYSTEM_SECRET={secrets.token_hex(32)}",
        f"HYDRA_PUBLIC_URL={issuer}",
        f"HYDRA_LOGIN_URL={url.rstrip('/')}/login",
        f"HYDRA_CONSENT_URL={url.rstrip('/')}/consent",
        f"TOKEN_MASTER_KEY={fernet_key()}",
        f"MEMAIX_ALLOWED_USERS={admin}",
        f"MEMAIX_LOGIN_PASSWORD_HASH_{admin.upper()}={password_hash}",
        "NEXTCLOUD_ADMIN_USER=admin",
        f"NEXTCLOUD_ADMIN_PASSWORD={secrets.token_hex(16)}",
        "NEXTCLOUD_PUBLIC_HOST=",
    ]
    # Compose läser .env för variabelsubstitution: vilka profiler `make up`
    # ska resa, och vilket uid gatewayen kör som mot bind-mountarna (samma
    # ägare som skrev vaults/ och data/ här, annars vägrar git — se compose).
    env_lines.append(f"COMPOSE_PROFILES={compose_profiles(a)}")
    if hasattr(os, "getuid"):
        env_lines.append(f"MEMAIX_UID={os.getuid()}")
        env_lines.append(f"MEMAIX_GID={os.getgid()}")
    if a.get("llm_api_key"):
        env_lines.append(f"LLM_API_KEY={a['llm_api_key']}")
    write_secret_file(root / ".env", "\n".join(env_lines) + "\n")

    # Skapa bind-mount-katalogerna själva. Saknas de skapar Docker dem som
    # root, och gatewayen (som kör som MEMAIX_UID) kan då inte skriva /data.
    for d in ("data", "data/login-app", "vaults"):
        (root / d).mkdir(parents=True, exist_ok=True)

    return {
        "track": a["track"],
        "public_url": url,
        "tunnel_provider": a["tunnel_provider"],
        "admin_user": admin,
        "project_name": project,
        "llm_provider": a.get("llm_provider", "byo"),
        "written": ["config/brand.yaml", "config/memaix.yaml", "config/acl.yaml", ".env"],
    }


def seed_vaults(root: Path, project_names: list) -> list:
    """Seeda vault-mappar från vault-template. Ren Python (Windows-säker);
    git-historik initieras om git finns på värden, annars hoppas den över
    (gatewayn init:ar om vid behov)."""
    tpl = root / "vault-template"
    vaults = root / "vaults"
    seeded = []
    for pname in list(dict.fromkeys(list(project_names) + ["shared"])):
        dest = vaults / pname
        if dest.exists():
            continue
        src = tpl / ("shared" if pname == "shared" else "PROJECT-TEMPLATE")
        if src.exists():
            shutil.copytree(src, dest)
        else:
            dest.mkdir(parents=True)
        if shutil.which("git"):
            env = {**os.environ, "GIT_AUTHOR_NAME": "memaix",
                   "GIT_AUTHOR_EMAIL": "memaix@localhost",
                   "GIT_COMMITTER_NAME": "memaix",
                   "GIT_COMMITTER_EMAIL": "memaix@localhost"}
            for cmd in (["git", "-C", str(dest), "init", "-q"],
                        ["git", "-C", str(dest), "add", "-A"],
                        ["git", "-C", str(dest), "commit", "-qm", "seed"]):
                subprocess.run(cmd, env=env, capture_output=True)
        seeded.append(pname)
    return seeded
