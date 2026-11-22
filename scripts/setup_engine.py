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
import string
import subprocess
from pathlib import Path

TRACK_TRIAL, TRACK_SELFHOST, TRACK_MANAGED = 1, 2, 3

# CHOOSE-YOUR-LLM.md: BYO = inget model-block; API-leverantörer kräver nyckel;
# endpoint-lägen (LLM på lokala nätet ELLER egen molninstans) kräver bas-URL.
LLM_API_PROVIDERS = ("anthropic", "openai", "google", "openrouter", "mistral")
LLM_ENDPOINT_PROVIDERS = ("openai-compatible", "ollama", "vllm")

# WIZARD.md fråga 6 (backends): "senare" (default) eller en av dessa fyra val.
# Bara BUNDLED_NEXTCLOUD har motor-stöd idag — resten (Gmail/M365/extern IMAP)
# är fortfarande "lägg till senare" via `memaix backend add` (docs/BACKENDS.md).
FILES_BACKEND_NONE = "none"
FILES_BACKEND_BUNDLED_NEXTCLOUD = "bundled_nextcloud"
FILES_BACKENDS = (FILES_BACKEND_NONE, FILES_BACKEND_BUNDLED_NEXTCLOUD)

# Kontot nextcloud-imaget bootstrapar sig själv från (NEXTCLOUD_ADMIN_USER/
# NEXTCLOUD_ADMIN_PASSWORD i .env, se docker-compose.yml) — samma konstant på
# båda ställena write_config() skriver det, så de aldrig kan glida isär.
BUNDLED_NEXTCLOUD_ADMIN_USER = "admin"
# Interna Docker-nätverksadressen till nextcloud-tjänsten (compose service-
# namnet); porten 8081 i docker-compose.yml är bara host-publiceringen.
BUNDLED_NEXTCLOUD_URL = "http://nextcloud"
_NC_PASSWORD_LEN = 32
_NC_ALPHABET = string.ascii_letters + string.digits

_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

# PER-USER-OAUTH.md / BACKENDS.md: varje kund driver sin EGEN OAuth-app (eget
# Google Cloud-projekt / Azure AD-app) — aldrig en Memaix-delad app. Dessa
# fält samlar in den appens client_id/client_secret (och för Microsoft:
# tenant_id, se docstring på _validate_microsoft_oauth) en gång per instans,
# vid setup. Formatkraven är medvetet strikta: värdena hamnar dels i en
# f-string-byggd YAML-fil, dels radbaserat i .env — ett citattecken,
# radbrytning, `$` eller `#` i indata kan annars injicera YAML-nycklar eller
# nya .env-rader (se SECRETS.md).
_GOOGLE_OAUTH_CLIENT_ID_RE = re.compile(
    r"^[A-Za-z0-9-]+\.apps\.googleusercontent\.com$"
)
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Tillåtna tecken i en client secret: inga citattecken, blanksteg, radbrytning,
# $ eller # — de kan bryta YAML- eller .env-syntaxen de klistras in i.
_OAUTH_SECRET_RE = re.compile(r"^[A-Za-z0-9+/=._~:@%-]+$")

# oauth_providers-skelettet är låst till detta exakta schema (config.secret(),
# server.py rad ~438-440/~3692-3693/~3723-3724, config/memaix.example.yaml) —
# bygg inte om det här utan att synka alla tre ställen.
_GOOGLE_OAUTH_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",
)
_GOOGLE_OAUTH_MAIL_CAPABILITY_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
)
_MICROSOFT_OAUTH_SCOPES = (
    "openid",
    "email",
    "Calendars.ReadWrite",
    "Mail.Read",
    "offline_access",
)


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
    if a.get("files_backend") == FILES_BACKEND_BUNDLED_NEXTCLOUD:
        profiles.append("nextcloud")
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
        "files_backend": FILES_BACKEND_NONE,
        # Kundens EGEN OAuth-app (PER-USER-OAUTH.md) — instansnivå, en gång.
        # Tomt = oauth_providers skrivs inte alls för den providern; den
        # enskilda användarens /link/<provider>-flöde är separat och kräver
        # detta satt av en admin först.
        "google_oauth_client_id": "",
        "google_oauth_client_secret": "",
        "microsoft_oauth_client_id": "",
        "microsoft_oauth_client_secret": "",
        "microsoft_oauth_tenant_id": "",
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

    files_backend = a.get("files_backend", FILES_BACKEND_NONE)
    if files_backend not in FILES_BACKENDS:
        errors.append(f"Okänt filer-backend-val: {files_backend!r}.")

    _validate_google_oauth(a, errors)
    _validate_microsoft_oauth(a, errors)
    return errors


def _validate_google_oauth(a: dict, errors: list[str]) -> None:
    """Kundens egen Google Cloud OAuth-app. Tomt är giltigt (ingen koppling
    konfigureras); fail closed annars — hellre ett klart fel vid setup än en
    trasig/ofullständig oauth_providers.google som gatewayen tyst struntar i."""
    client_id = a.get("google_oauth_client_id", "").strip()
    client_secret = a.get("google_oauth_client_secret", "").strip()
    if not client_id and not client_secret:
        return
    if not client_id or not client_secret:
        errors.append(
            "Google OAuth kräver både client ID och client secret tillsammans "
            "(eller lämna båda tomma)."
        )
        return
    if not _GOOGLE_OAUTH_CLIENT_ID_RE.match(client_id):
        errors.append(
            "Google OAuth client ID ser inte ut som en giltig "
            "…apps.googleusercontent.com-identifierare från Google Cloud Console."
        )
    if not _OAUTH_SECRET_RE.match(client_secret):
        errors.append(
            "Google OAuth client secret innehåller otillåtna tecken "
            "(inga citattecken, blanksteg, $, # eller radbrytningar)."
        )


def _validate_microsoft_oauth(a: dict, errors: list[str]) -> None:
    """Kundens egen Azure AD-app (single-tenant Entra-app i kundens egen
    tenant, BACKENDS.md). Till skillnad från Google kräver Microsoft-flödet
    även tenant ID — gatewayens token-URL är idag hårdkodad mot /common/
    (server_http.py, server.py _refresh_microsoft_token), så tenant_id
    skrivs till config för framtida bruk men används INTE av gatewayen än;
    se docs/PER-USER-OAUTH.md-notisen som denna ändring lägger till."""
    client_id = a.get("microsoft_oauth_client_id", "").strip()
    client_secret = a.get("microsoft_oauth_client_secret", "").strip()
    tenant_id = a.get("microsoft_oauth_tenant_id", "").strip()
    present = [v for v in (client_id, client_secret, tenant_id) if v]
    if not present:
        return
    if len(present) != 3:
        errors.append(
            "Microsoft/Azure AD OAuth kräver client ID, client secret OCH "
            "tenant ID tillsammans (eller lämna alla tre tomma)."
        )
        return
    if not _GUID_RE.match(client_id):
        errors.append("Microsoft OAuth client ID måste vara Azure-appens GUID.")
    if not _GUID_RE.match(tenant_id):
        errors.append(
            "Microsoft OAuth tenant ID måste vara kundens egen Azure-tenants GUID."
        )
    if not _OAUTH_SECRET_RE.match(client_secret):
        errors.append(
            "Microsoft OAuth client secret innehåller otillåtna tecken "
            "(inga citattecken, blanksteg, $, # eller radbrytningar)."
        )


def public_url(a: dict) -> str:
    if a["track"] == TRACK_TRIAL:
        return "http://localhost:8080"
    return f"https://{a['domain']}"


FILES_NOTICE = (
    "OBS: start-projekten skapades utan filarea (inget Nextcloud-konto). "
    "Nya projekt via project_create kräver nextcloud_provision i config/memaix.yaml om "
    "require_files: true är satt; se docs/SECRETS.md."
)


def files_notice(projects: list) -> str | None:
    """Notis när start-projekten skapas utan filarea.

    write_config KAN skriva nextcloud_provision nu (files_backend ==
    bundled_nextcloud), men bara för nya projekt skapade EFTER installationen
    via project_create — start-projekten själva får ändå ingen filarea
    (seed_vaults/write_config provisionerar inget Nextcloud-konto för dem),
    så notisen gäller oförändrat varje installation som skapar projekt."""
    return FILES_NOTICE if projects else None


def _yaml_scope_lines(indent: str, scopes: tuple[str, ...]) -> str:
    return "".join(f'{indent}- "{s}"\n' for s in scopes)


def _oauth_providers_block(a: dict) -> str:
    """oauth_providers-blocket för memaix.yaml — PER-USER-OAUTH.md: instansens
    admin konfigurerar VILKEN OAuth-app hela instansen använder (kundens egen,
    aldrig en Memaix-delad app), en gång, innan någon enskild användare kan
    länka sitt konto via /link/<provider>. Tomt om inget providersvar gavs."""
    google_id = a.get("google_oauth_client_id", "").strip()
    google_secret = a.get("google_oauth_client_secret", "").strip()
    ms_id = a.get("microsoft_oauth_client_id", "").strip()
    ms_secret = a.get("microsoft_oauth_client_secret", "").strip()
    ms_tenant = a.get("microsoft_oauth_tenant_id", "").strip()

    if not (google_id and google_secret) and not (ms_id and ms_secret and ms_tenant):
        return ""

    lines = ["\noauth_providers:\n"]
    if google_id and google_secret:
        lines.append('  google:\n')
        lines.append(f'    client_id: "{google_id}"\n')
        lines.append('    client_secret_ref: "env:GOOGLE_CLIENT_SECRET"\n')
        lines.append('    scopes:\n')
        lines.append(_yaml_scope_lines('      ', _GOOGLE_OAUTH_SCOPES))
        lines.append('    capability_scopes:\n')
        lines.append('      mail:\n')
        lines.append(_yaml_scope_lines('        ', _GOOGLE_OAUTH_MAIL_CAPABILITY_SCOPES))
    if ms_id and ms_secret and ms_tenant:
        lines.append('  microsoft:\n')
        lines.append(f'    client_id: "{ms_id}"\n')
        lines.append('    client_secret_ref: "env:MICROSOFT_CLIENT_SECRET"\n')
        # tenant_id skrivs för framtida bruk — gatewayen använder idag
        # hårdkodad /common/ (se _validate_microsoft_oauth-docstring ovan
        # och PER-USER-OAUTH.md). Inte en hemlighet; får stå i klartext.
        lines.append(f'    tenant_id: "{ms_tenant}"\n')
        lines.append('    scopes:\n')
        lines.append(_yaml_scope_lines('      ', _MICROSOFT_OAUTH_SCOPES))
    return "".join(lines)


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

    # Bundlad Nextcloud (compose-profilen "nextcloud"): peka gatewayens OCS-
    # provisionering på exakt det konto nextcloud-imaget bootstrapar sig själv
    # från (samma NEXTCLOUD_ADMIN_USER/PASSWORD som skrivs till .env nedan) —
    # ingen handpåskriven adminidentitet. docs/BACKENDS.md förklarar skillnaden
    # mot en kunds egen, EXTERNA Nextcloud (dit skriver kunden själv in riktiga
    # adminuppgifter; den vägen bygger write_config aldrig).
    nextcloud_block = ""
    if a.get("files_backend") == FILES_BACKEND_BUNDLED_NEXTCLOUD:
        nextcloud_block = (
            f'\nnextcloud_provision:\n'
            f'  url: "{BUNDLED_NEXTCLOUD_URL}"\n'
            f'  admin_user: "{BUNDLED_NEXTCLOUD_ADMIN_USER}"\n'
            f'  admin_password_ref: "env:NEXTCLOUD_ADMIN_PASSWORD"\n'
        )

    oauth_providers_block = _oauth_providers_block(a)

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
        f'{nextcloud_block}'
        f'{oauth_providers_block}'
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
        f"HYDRA_DB_PASSWORD={secrets.token_hex(32)}",
        f"HYDRA_SYSTEM_SECRET={secrets.token_hex(32)}",
        f"HYDRA_PUBLIC_URL={issuer}",
        f"HYDRA_LOGIN_URL={url.rstrip('/')}/login",
        f"HYDRA_CONSENT_URL={url.rstrip('/')}/consent",
        f"TOKEN_MASTER_KEY={fernet_key()}",
        f"MEMAIX_ALLOWED_USERS={admin}",
        f"MEMAIX_LOGIN_PASSWORD_HASH_{admin.upper()}={password_hash}",
        f"NEXTCLOUD_ADMIN_USER={BUNDLED_NEXTCLOUD_ADMIN_USER}",
        f"NEXTCLOUD_ADMIN_PASSWORD={secrets.token_hex(16)}",
        "NEXTCLOUD_PUBLIC_HOST=",
    ]
    # Compose läser .env för variabelsubstitution: vilka profiler `make up`
    # ska resa, och vilket uid gatewayen kör som mot bind-mountarna (samma
    # ägare som skrev vaults/ och data/ här, annars vägrar git — se compose).
    env_lines.append(f"COMPOSE_PROFILES={compose_profiles(a)}")
    # Hydra vägrar starta med en http-issuer utan --dev. Bara lokala
    # installationer (http://localhost) får dev-läget; allt med riktig domän
    # kör Hydra utan, och compose defaultar dit när variabeln saknas.
    if issuer.startswith("http://"):
        env_lines.append("HYDRA_SERVE_FLAGS=--dev")
    if hasattr(os, "getuid"):
        env_lines.append(f"MEMAIX_UID={os.getuid()}")
        env_lines.append(f"MEMAIX_GID={os.getgid()}")
    if a.get("llm_api_key"):
        env_lines.append(f"LLM_API_KEY={a['llm_api_key']}")
    google_oauth_configured = bool(
        a.get("google_oauth_client_id", "").strip()
        and a.get("google_oauth_client_secret", "").strip()
    )
    if google_oauth_configured:
        env_lines.append(f"GOOGLE_CLIENT_SECRET={a['google_oauth_client_secret'].strip()}")
    microsoft_oauth_configured = bool(
        a.get("microsoft_oauth_client_id", "").strip()
        and a.get("microsoft_oauth_client_secret", "").strip()
        and a.get("microsoft_oauth_tenant_id", "").strip()
    )
    if microsoft_oauth_configured:
        env_lines.append(
            f"MICROSOFT_CLIENT_SECRET={a['microsoft_oauth_client_secret'].strip()}"
        )
    write_secret_file(root / ".env", "\n".join(env_lines) + "\n")

    # Tunnel-token i en fil (compose monterar den med --token-file), så den inte
    # syns i `docker inspect`. Filen skapas även när token saknas: saknas den
    # gör Docker en katalog av monteringspunkten och cloudflared startar aldrig.
    secrets_dir = root / "cloudflared-secrets"
    secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_secret_file(secrets_dir / "tunnel.token", a["tunnel_token"])

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
        "files_backend": a.get("files_backend", FILES_BACKEND_NONE),
        "google_oauth_configured": google_oauth_configured,
        "microsoft_oauth_configured": microsoft_oauth_configured,
        "written": ["config/brand.yaml", "config/memaix.yaml", "config/acl.yaml", ".env"],
        "notice": files_notice(list(projects)),
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
