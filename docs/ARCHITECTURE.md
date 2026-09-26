# Arkitektur

**Senast verifierad mot kod:** 2026-07-25
**Repo:** `jimlov-poc-labs/memaix` (publikt, anonymiserat)

> Detta dokument beskriver **hur systemet är byggt**. För *varför* — se `AGENTS.md` §1
> (auktoritativa v2-beslut) och `PRODUCT.md`. För *vad man kan göra* — se `MCP-API.md`
> och `USER-MANUAL.md`.

---

## Topologi

```
  Användare (Claude / ChatGPT / Mistral / Perplexity — desktop & mobil)
        │  HTTPS: Streamable HTTP MCP + OAuth 2.1/PKCE med DCR
        ▼
  cloudflared  ──  mcp.kundens-domän.se        [profile: tunnel | tunnel-local]
        │  outbound-only, auto-TLS, ingen portöppning
        ▼
  caddy :80    reverse proxy, auto_https off   [profile: hydra]
        │
        ├── /oauth2/register* ────────────► gateway  (spärrar grant types, injicerar audience)
        ├── /oauth2/*, /.well-known/openid-configuration,
        │   /.well-known/jwks.json ───────► hydra:4444
        ├── /login*, /consent* ───────────► login-app:3000
        └── allt annat ───────────────────► gateway:8080
                                              │
  ┌───────────────────────────────────────────┴───────────────────────────────┐
  │  Memaix gateway (Python 3.12, FastMCP + Starlette/uvicorn)   :8080        │
  │                                                                           │
  │  MCP monteras i ROTEN  (streamable_http_path = "/")                       │
  │  Webbläsare på bar domän → redirect till /app (webb-UI) i stället för 401 │
  │                                                                           │
  │  AuthN: HydraTokenVerifier — Bearer-JWT mot Hydras JWKS                   │
  │  AuthZ: acl.yaml → reader < collaborator < owner, per projekt             │
  │  Varje verktygsanrop: rate limit → ACL → audit → idempotens →             │
  │                       timeline-registrering → sökindexering               │
  │                                                                           │
  │  91 MCP-verktyg  ·  webb-UI (/app)  ·  board (/board)  ·  LLM-agent       │
  └───────────────────────────────────────────────────────────────────────────┘
        │
        ├── email_*     → IMAP/SMTP · Microsoft Graph   [per projekt]
        ├── calendar_*  → CalDAV                        [per projekt]
        ├── contacts_*  → CardDAV                       [per projekt]
        ├── files_*     → lokal vault · WebDAV          [per projekt]
        ├── nc_*        → Nextcloud (Files, Tasks, Deck, Notes)
        ├── memory_*    → SQLite (aktivt) + git (historik)
        ├── backlog_*   → markdown i vault
        └── pm_*        → SQLite + CP-SAT (ortools)

  hydra :4444/:4445   ory Hydra v2.2 (OAuth 2.1 AS, DCR, JWT)   [profile: hydra]
  postgres            Hydras databas                            [profile: hydra]
  login-app :3000     FastAPI — Hydras login/consent-UI         [profile: hydra]
  nextcloud :8081     valfri medföljande backend                [profile: nextcloud]
```

---

## Bärande val

- **En instans per deployment (single-tenant).** Varje kund kör sin egen Memaix med egen
  domän, egna tunnlar, egen branding och egen data. Enklare isolering, inget delat dataplan.
- **En connector, RBAC bakom.** Alla användare lägger in samma URL. `config/acl.yaml`
  avgör vad var och en ser. Externa låses till exakt ett projekt.
- **Öppna standarder i backenden.** IMAP/SMTP, CalDAV, CardDAV, WebDAV, git — inga
  proprietära beroenden.
- **Auth via beprövad komponent.** OAuth2 hanteras av ory Hydra (certifierad, DCR), inte
  hemsnickrad kod. Tunneln är ren proxy (ingen Access). Se `REVIEW-RESPONSE.md`.
- **Minne = SQLite + git.** Aktivt tillstånd i SQLite (snabbt, transaktioner, concurrency);
  git ger versionshistorik och rollback. Se `SAFETY.md` — och avvikelse #2 nedan.
- **Fail-closed identitet.** I HTTP-läge finns ingen fallback på `MEMAIX_USER`; saknas ett
  verifierat OAuth-subject kastas fel.
- **AI-agnostiskt.** MCP är öppen standard → Claude, ChatGPT, Mistral, Perplexity m.fl.

---

## Tjänster (`docker-compose.yml`)

Sju services, inget explicit nätverk (default bridge — tjänsterna adresserar varandra på
servicenamn). Två named volumes: `nextcloud-data`, `hydra-db`.

| Service | Image / build | Portar (host) | Profile | Rader |
|---|---|---|---|---|
| `gateway` | `build: ./gateway` → `memaix/gateway:latest` | `127.0.0.1:8080:8080` | `hydra` | 5–22 |
| `login-app` | `build: ./login-app` | – | `hydra` | 24–31 |
| `caddy` | `caddy:2-alpine` | `127.0.0.1:80:80` | `hydra` | 33–44 |
| `cloudflared` | `cloudflare/cloudflared:latest` (token-läge) | – | `tunnel` | 53–58 |
| `cloudflared-local` | samma image, lokal `config.yml` | – | `tunnel-local` | 63–70 |
| `postgres` | `postgres:16-alpine` | – | `hydra` | 72–81 |
| `hydra` | `oryd/hydra:v2.2` | `127.0.0.1:4444:4444`, `:4445:4445` | `hydra` | 83–104 |
| `nextcloud` | `nextcloud:latest` | `127.0.0.1:8081:80` | `nextcloud` | 107–118 |

**Alla portar är bundna till `127.0.0.1`.** Enda vägen in utifrån är cloudflared-tunneln.

**Det finns inga healthchecks.** Ingen `healthcheck:`-nyckel i hela filen. `gateway`
väntar på `hydra` med `condition: service_started` (rad 19–21), inte `service_healthy`.

### Två driftlärdomar som är inbakade i filen

1. **`./config` monteras `rw`, inte `:ro`** (rad 13–14). En `:ro`-mount bröt tyst
   AclWriter och MFA-skrivvägar — gatewayen skriver tillbaka till `acl.yaml`.
2. **`tunnel-local` rekommenderas framför `tunnel`** (rad 48–52). I token-läge hämtas
   ingress-konfigurationen från Cloudflare; blir den fel svarar tunneln 503 på allt
   medan containern rapporterar "healthy".

---

## Gateway (`gateway/`)

Python ≥3.12. MCP-SDK:ns `FastMCP` (`server.py:18`, instansieras `server.py:550`),
Starlette + uvicorn för HTTP. Entrypoint: `gateway/Dockerfile:15` →
`python -m memaix_gateway.server`, `ENV MEMAIX_TRANSPORT=http`.

`main()` (`server.py:2620-2628`): `--http` eller `MEMAIX_TRANSPORT=http` →
`build_http_app()` + uvicorn på `memaix.server.bind` (default `0.0.0.0:8080`).
Annars `mcp.run()` (stdio).

### Modulkarta — `gateway/src/memaix_gateway/`

```
server.py (2634 rader)  __main__.py  cli.py  config.py  acl.py
paths.py  frontmatter.py  backlog_schema.py  doctor.py

auth/          token.py              HydraTokenVerifier, JWKS-hämtning
backends/      memory_store.py       SQLite + FTS5 + git-historik
               token_store.py        SQLite + Fernet-krypterade OAuth-tokens
board/         routes.py  store.py  board.html
capabilities/  catalog.py  nudges.py  registry.py      (upptäckbarhet)
connectors/    base.py  catalog.py  registry.py
               adapters/  contacts_carddav · deck_nextcloud · files_webdav ·
                          mail_microsoft · notes_nextcloud · tasks_caldav
i18n/          locales/  en · sv · fr · de · es
llm/           agent.py  client.py  identity.py  toolbridge.py   (server-side agentloop)
nextcloud/     docgen.py  notes_store.py  sync.py
notify/        brief.py  channels.py  deliver.py  scheduler.py  store.py
outbox/        execute.py  policy.py  preview.py  queue.py       (godkännandekö)
pm/            allocate.py  allocate_cpsat.py  report.py  schedule.py
               schemas.py  store.py  whatif.py
rules/         actions.py  engine.py  match.py  store.py         (automationsregler)
safety/        audit.py  idempotency.py  net.py  rate_limit.py
search/        embedder.py  index.py  query.py  store.py         (semantisk sökning)
timeline/      inverse.py  store.py  undo.py                     (ångra)
tools/         account · backlog · calendar · contacts · email · files · memory ·
               nc_docgen · nc_files · nc_tasks · onboarding · pm · pm_engine · whoami
web/           routes.py  acl_writer.py  totp.py
               api/     accounts · admin · admin_llm · admin_write · brief ·
                        memory · mfa · outbox · search · timeline
               pages/   admin · board · home · login · memory · outbox ·
                        search · settings · shell  (HTML)
               static/  app.css + 8 .js
```

Tester: `gateway/tests/` (~90 filer, `pytest`), `gateway/tests_e2e/` (Playwright).

Extras i `gateway/pyproject.toml:24-48`: `dev`, `search` (sentence-transformers),
`pm` (ortools/CP-SAT), `e2e` (playwright). Semantisk sökning och PM-optimering är alltså
valfria installationer.

### HTTP-ytan — `build_http_app()` (`server.py:2191`)

| Route | Vad |
|---|---|
| `/` | MCP Streamable HTTP (`mcp.settings.streamable_http_path = "/"`, rad 2502) — claude.ai hittar endpointen direkt på connector-URL:en |
| `/health` | liveness |
| `/.well-known/oauth-authorization-server` | proxar Hydras openid-configuration och **injicerar `registration_endpoint`** (rad 2250) — Hydra v2 annonserar inte DCR själv |
| `/.well-known/oauth-protected-resource` | RFC 9728. Insatt **först** i router-listan (rad 2526) för att undvika trailing-slash-mismatch mot claude.ai:s aud-validering |
| `/oauth2/register` (POST) | proxar DCR till `http://hydra:4444/oauth2/register` och injicerar `aud` så JWT:erna får audience-claim. Nekar `grant_types` utanför `authorization_code`/`refresh_token` och `response_types` utanför `code` (`_dcr_avvisning`) — Hydra v2.2 kan inte själv begränsa DCR och saknar initial access token |
| `/oauth2/register/{client_id}` (GET/PUT/DELETE) | RFC 7592-hanteringen, proxad med klientens registration access token. PUT får samma spärr som POST, annars kunde en klient byta sig till `client_credentials` |
| `/link/{provider}` + `/link/{provider}/callback` | per-användar-OAuth mot Google/Microsoft |
| `/hooks/{token}` (POST) | webhook-triggers för automationsregler |
| `/app/*`, `/board` | webb-UI och board, monterade ovanpå MCP-appen |

CORS låses till `https://claude.ai` och `https://api.claude.ai`.
`BrowserRootRedirect` gör att en webbläsare på bar domän får UI:t i stället för MCP:s
401-JSON.

### Auth — hur identitet fastställs

**AuthN.** `HydraTokenVerifier` (`auth/token.py:24`) implementerar MCP-SDK:ns
`TokenVerifier`-protokoll, så FastMCP:s auth-middleware anropar `verify_token()` per
request. JWT valideras mot Hydras JWKS.

**Identitetsupplösning** — `_user()` (`server.py:156-187`), i tre steg:

1. `_AGENT_USER` ContextVar (`server.py:28`, satt av `llm/toolbridge.py` från en
   verifierad webbsession) — **kollas först**, så den server-side LLM-agenten kör som
   rätt användare.
2. OAuth-token via `get_access_token()`; `token.subject` mappas genom `acl.yaml`
   (`Acl.user_by_subject`). Omappad subject → `RuntimeError`.
3. **Fail-closed:** i HTTP-läge kastas `RuntimeError("no authenticated OAuth subject…")`.
   `MEMAIX_USER` honoreras **bara** i stdio-läge.

**AuthZ.** `acl.py:77` — `Acl.enforce(user_id, project, need)`. Rollrankning
`reader < collaborator < owner`. Dessutom `is_admin()`, `is_disabled()`,
`visible_projects()`.

**Verktygspipelinen.** Varje anrop går genom `_tool_call()` (`server.py:528`), som kedjar:
rate limit (60/min per användare, 120/min per projekt) → ACL-enforce → audit-loggning →
idempotenskontroll → timeline-registrering (underlag för `timeline_undo`) →
sökindexering.

**Webb-UI och board har en separat auth-väg:** stateless HMAC-signerad cookie
(`board/routes.py::_check_cookie`), delad mellan `/board` och `/app`. Ingen
SessionMiddleware, ingen server-side sessionslagring (`web/routes.py:3-6`). Ovanpå det
TOTP/MFA (`web/totp.py`, `/app/api/admin/mfa/*`).

### Storage-lager

| Lager | Var | Teknik |
|---|---|---|
| Minne (aktivt) | `{vault}/.memaix.db` | SQLite + FTS5 |
| Minne (historik) | git-repo i vault-roten | git |
| OAuth-tokens | SQLite | Fernet (AES-128-CBC + HMAC-SHA256), nyckel från `TOKEN_MASTER_KEY` |
| Audit, rate limit, idempotens, PM, sök/embeddings, timeline, notify, rules | separata SQLite-DB:er via `MEMAIX_*_DB` (default under `/tmp`) | SQLite |
| Backlog | markdown med YAML-frontmatter i vault | fil |

---

## Verktyg — 91 st, alla projekt-scopade och ACL-validerade

Räknat som `@mcp.tool`-dekoratorer i `server.py`.

| Grupp | Verktyg |
|---|---|
| Identitet | `whoami`, `onboarding_complete` |
| Konto | `account_link/list/unlink` — länka externa OAuth-konton (Google, Microsoft) |
| Outbox | `outbox_list/get/approve/reject` — utgående åtgärder kräver godkännande |
| Timeline | `timeline_list/undo` — ångra genom inversa operationer |
| Sökning | `search_all/reindex/status` — semantisk sökning (kräver `[search]`-extra) |
| Brief | `brief_configure/status/preview/send_now` — proaktiv sammanfattning |
| Regler | `rule_add/list/set_enabled/delete/test`, `standing_set/get` |
| Upptäckbarhet | `capabilities`, `next_suggestion` |
| Filer | `files_list/read/write/search` |
| Minne | `memory_read/search/write/set_status/append/history/revert` |
| Backlog | `backlog_add/list/get/score/comment/set_status/assign` |
| PM (22 st) | `pm_set_methodology/status_report/plan_sprint/sprint_status/raid_add/raid_list`, `resource_add/list/availability/set_skill`, `milestone_add`, `task_add/estimate/log_actual`, `dependency_add`, `scenario_add/list`, `pm_allocate/whatif/utilization/variance`, `plan_commit`, `pm_report` |
| Kalender | `calendar_setup/status/list/find_free/create/update` |
| Kontakter | `contacts_search/get` |
| Nextcloud | `nc_files_list/read/write/search`, `nc_generate_report`, `nc_tasks_list/add/complete`, `deck_sync`, `notes_sync` |
| Mejl | `email_list/read/search/create_draft` (+ `email_send` bakom `allow_send`) |

---

## login-app

Minimal FastAPI-app som implementerar **Hydras login/consent-flöde** — Hydra levererar
inget UI. Kör `uvicorn app:app --host 0.0.0.0 --port 3000`. Rutter: `GET/POST /login`,
`GET /consent` (auto-godkänner för single-user/trusted client), `GET /health`. Anropar
Hydras admin-API (`_hydra_get/_hydra_accept/_hydra_reject`). Inget sessionstillstånd i
appen — Hydra håller sessionen.

`login-app/auth.py` är medvetet fri från FastAPI/requests så säkerhetslogiken kan
enhetstestas från gateway-sviten. PBKDF2-HMAC-SHA256, 200 000 iterationer,
konstanttidsjämförelse.

**Impersonation-försvar** (`auth.py:7-14`): appen mintar en OAuth-identitet med
`subject=username`. Ett **delat** lösenord skulle därför låta vem som helst med lösenordet
logga in *som* någon annan. Lösningen: den delade hashen honoreras **bara när exakt en
användare är tillåten**. Per-användarhashar läses från `acl.yaml`
(`users.<id>.password_hash`) och env `MEMAIX_LOGIN_PASSWORD_HASH_<USER>` — env vinner.

---

## caddy och cloudflared

**`caddy/Caddyfile`** (48 rader) — reverse proxy på `:80`, `auto_https off`
(Cloudflare-tunneln terminerar TLS), `admin off`. Routing enligt topologidiagrammet ovan.
Rad 33–34 dokumenterar att `/.well-known/oauth-authorization-server` medvetet serveras av
**gatewayen** och inte proxas till Hydra.

**`cloudflared/`** innehåller endast `config.example.yml`. Riktig `config.yml` och
`*.json` är gitignorerade (`cloudflared/*` med undantag för exempelfilen). Ingress:
`hostname: mcp.dindomän.se → http://caddy:80`, catch-all `http_status:404`.

---

## vault-template

Mall för en projekt-vault som seedas vid installation. Fem filer:

```
PROJECT-TEMPLATE/playbook.md            per-projekt-kontext
PROJECT-TEMPLATE/backlog/TEMPLATE.md    YAML-frontmatter-schema för backlog-items
                                        (id, title, author, category, status:
                                         inbox → triaged → evaluated →
                                         approved/rejected → in-dev → done)
shared/assistant-manual.md              plattformsoberoende beteenderegler
shared/onboarding-interview.md          intervjuprompt
shared/writing-style.md                 anti-AI-stilguide, bannlysta ord
```

Backlog-schemat har sin kodmotsvarighet i `gateway/src/memaix_gateway/backlog_schema.py`
och `frontmatter.py`. Vault-innehållet är alltså inte bara dokumentation — det är
assistentens beteendekontrakt, och det versioneras med kunden.

---

## Konfiguration

### `config/` — riktiga filer är gitignorerade

`config/*.yaml` ignoreras, `!config/*.example.yaml` undantas. I repot finns bara:

| Fil | Innehåll |
|---|---|
| `memaix.example.yaml` (82 rader) | `server` (bind, public_url, locale), `auth` (issuer, resource_server_url), samt utkommenterade block för `oauth_providers`, `outbox`, `brief`, `rules`, `search`, `pm`, `onboarding` |
| `acl.example.yaml` | `users:` (admin-flagga, `oauth_subjects`, `password_hash`, `grants: {projekt: roll}`) och `projects:` (`vault`, `allow_send`, `calendar`, `outbox`-läge + allowlist, `contacts`/`files`/`tasks` med `password_ref`) |
| `brand.example.yaml` (9 rader) | White-label: `name`, `tagline`, `support_email`, `primary_color`, `logo_path`. Inget i koden får hårdkoda produktnamnet. |

Hemligheter refereras aldrig direkt utan via `*_ref` med schema `env:` / `file:` /
`vault:` / `kms:` (`AGENTS.md` §3).

### `.env` — variabelnamn (se `.env.example`, 36 rader)

- **Tunnel:** `CLOUDFLARE_TUNNEL_TOKEN`
- **Hydra:** `HYDRA_DB_PASSWORD`, `HYDRA_SYSTEM_SECRET`, `HYDRA_PUBLIC_URL`,
  `HYDRA_LOGIN_URL`, `HYDRA_CONSENT_URL`
- **Gateway:** `TOKEN_MASTER_KEY` (obligatorisk i HTTP-läge — gatewayen startar inte utan
  den), `MEMAIX_ALLOW_EPHEMERAL_KEY` (alla länkade konton tappas vid omstart)
- **Delat tillstånd, bara vid >1 worker:** `MEMAIX_RATELIMIT_BACKEND`,
  `MEMAIX_RATELIMIT_DB`, `MEMAIX_STATE_DB`
- **login-app:** `MEMAIX_ALLOWED_USERS`, `MEMAIX_LOGIN_PASSWORD_HASH`
- **Backend-credentials per projekt** (refereras från `acl.yaml` via `*_ref`),
  t.ex. `NEXTCLOUD_APP_PASSWORD`
- **Nextcloud:** `NEXTCLOUD_ADMIN_USER`, `NEXTCLOUD_ADMIN_PASSWORD`,
  `NEXTCLOUD_PUBLIC_HOST`

Används av koden men saknas i `.env.example`: `MEMAIX_CONFIG_DIR` (default `/app/config`,
`config.py:20`), `MEMAIX_TRANSPORT`, `MEMAIX_USER` (stdio), `MEMAIX_LOCALE`,
`MEMAIX_AUDIT_DB`, `MEMAIX_PM_DB`, `MEMAIX_LOGIN_PASSWORD_HASH_<USER>`,
`GOOGLE_CLIENT_SECRET`, `MICROSOFT_CLIENT_SECRET`.

---

## Förhållandet till `memaix-config`

**Detta repo innehåller noll referenser till `memaix-config`.**
`grep -rn "memaix-config" .` (exkl. `.git`) ger inga träffar. Det finns inga submoduler
(`.gitmodules` saknas) och ingen mekanism som binder repona samman.

Arbetsdelningen är rent konventionsbaserad:

| Repo | Innehåll | Synlighet |
|---|---|---|
| `jimlov-poc-labs/memaix` (detta) | Kod, docs, `*.example.yaml`-mallar. All riktig config gitignorerad (`config/*.yaml`, `.env`, `cloudflared/*`, `vaults/`). | Publikt, anonymiserat |
| `jimlov-poc-labs/memaix-config` | Konkreta `config/memaix.yaml` + `config/acl.yaml` för **en** instans, plus egen `.env.example`. | Privat |

Deployment-flödet är manuellt: config kopieras in på värden
(`cp config/memaix.yaml /srv/memaix/config/memaix.yaml`). Hemligheter ligger i
Vaultwarden och hämtas separat — de finns i inget av repona.

> **Känd bräcklighet:** `memaix-config` refererar in i kodrepot med **hårdkodade
> radnummer** (t.ex. "docker-compose.yml rad 77, 88, 92", "server.py:91",
> "server.py:105-108"). De ruttnar tyst vid varje ändring här. Kopplingen är dessutom
> osynlig från kodrepots sida — någon som bara läser detta repo får ingen aning om att
> `memaix-config` existerar.

---

## Drift

**`ops/`** — två systemd-**användar**units för värden (inte containern):
`memaix-watchdog.service` (Type=oneshot, `WorkingDirectory=/srv/memaix`, kör
`scripts/watchdog.py`) och `memaix-watchdog.timer` (var 6:e timme, 5 min efter boot,
`Persistent=true`). Se `SELF-IMPROVING-SYSTEM.md` Fas A.

**`scripts/`**

| Fil | Rader | Vad |
|---|---|---|
| `bootstrap.py` | 534 | Wizard (`--init`) + installation; `--trial`, `--tunnel`, `--no-nextcloud`, `--doctor` |
| `setup_engine.py` | 234 | Delad config-genereringsmotor — en motor, två bärare (CLI + webb) |
| `setup_web.py` | 279 | First-boot-webbwizard, binder `127.0.0.1`. Se `SETUP-UI.md` |
| `setup_page.py` | 202 | Genererar `setup-complete.html` |
| `watchdog.py` | 231 | Väktaren. Körs på värden, endast stdlib + pyyaml |
| `check-docs-index.py` | 40 | Docs-hygien — exit 1 om något `docs/*.md` saknas i `INDEX.md` |
| `gen-password-hash.py` | 18 | PBKDF2-hash för login-appen |
| `migrate-to-standalone-repo.sh` | 58 | Historisk engångs-`git subtree split` — redan körd |

I roten dessutom `install.sh`, `setup.sh`, `setup.ps1`.

**`Makefile`** — `init`, `install`, `install-no-nextcloud`, `trial` (Tier 0, stdio-MCP,
ingen tunnel), `go-remote`, `up`, `down`, `seed`, `logs`, `doctor`, `docs-check`.
Alla anropar `scripts/bootstrap.py` med olika flaggor.

---

## CI (`.github/workflows/ci.yml`)

En fil, 74 rader, triggas på `[push, pull_request]`. Formatet fungerar även på
Forgejo/Gitea Actions. Tre jobb:

| Jobb | Steg |
|---|---|
| `checks` | `pip install -e "gateway[dev]"` → `check-docs-index.py` → `py_compile`/`compileall` → `pytest -q` (cwd `gateway`) → `ruff check` → `mypy` → `bandit` → `pip-audit --strict` |
| `e2e` | `gateway[dev,e2e]` + `playwright install chromium` → `pytest -q tests_e2e` |
| `sbom` | `anchore/sbom-action@v0` → CycloneDX JSON som artifact |

**Vad CI inte gör:** ingen Docker-image byggs eller scannas (`# TODO` i sbom-jobbet),
ingen deploy, ingen `docker compose`-validering, **ingen DCO- eller CLA-kontroll** trots
att `CONTRIBUTING.md` kräver båda, och **ingen SPDX-header-kontroll** trots `AGENTS.md` §4.

---

## Kända avvikelser mellan dokumentation och kod

Verifierade 2026-07-25. Listade här hellre än städade bort, så att nästa läsare inte
bygger på fel antagande.

| # | Avvikelse | Var |
|---|---|---|
| 1 | **`README.md` beskriver inte produkten.** Den säger att koden flyttat till `jimlov-poc-labs/memaix` — vilket är exakt det repo filen ligger i. Kvarleva från `git subtree split` 2026-06-30. | `README.md` |
| 2 | **Git-commits är synkrona, inte asynkrona.** `AGENTS.md` §1 och tidigare versioner av detta dokument slog fast "git asynkront, aldrig commit-per-skrivning". Koden commit:ar synkront inne i `write_lock` så att snapshot-id blir en riktig commit-hash; det finns ett `TODO(perf)` om batchning. `gateway/Dockerfile:5` säger uttryckligen "commit per skrivning". | `backends/memory_store.py:6-10` |
| 3 | **`make up` startar inte kärntjänsterna.** Målet kör `--profile tunnel --profile nextcloud`, men gateway, caddy, hydra, postgres och login-app ligger alla under profilen `hydra`. | `Makefile:22` vs `docker-compose.yml` |
| 4 | ~~**Hydra kör i dev-läge med läckande loggar.**~~ Åtgärdat 2026-09-26: `serve all` utan `--dev` (utom lokala http-installationer, där setup sätter `HYDRA_SERVE_FLAGS=--dev`), `SERVE_TLS_ENABLED: "false"` (TLS termineras i tunneln), `LOG_LEVEL: info`, `LOG_LEAK_SENSITIVE_VALUES: "false"`. | `docker-compose.yml` (hydra) |
| 5 | **Inga healthchecks.** Trots att `DOCTOR.md` och `make doctor` är centrala i drift-berättelsen har ingen service en `healthcheck:`. `gateway` väntar bara på `service_started`. | `docker-compose.yml` |
| 6 | **`gateway/README.md` märker implementerad kod som stub.** `server.py [stub]` (2634 rader), `auth/ [todo]`, `tools/ [stub]` — alla tre är fullt implementerade. Samma sak i `tools/__init__.py:1` ("STUBS to implement per docs/BUILD.md"). | `gateway/README.md:11-13` |
| 7 | **`docs/BUSINESS-CASE.md` är gitignorerad men länkad från `INDEX.md`** — bruten länk i den publika kopian. | `docs/INDEX.md` |

---

## Dokumentationskarta

71 `.md`-filer ligger platt i `docs/` (inga underkataloger). `docs/INDEX.md` organiserar
dem **per läsarroll**, inte per ämne, med numrerad läsordning:

1. **🧭 Beslutsfattare** — `PRODUCT`, `FOR-LEDNINGSGRUPPEN`, `ENTERPRISE`, `LEGAL`, `LICENSING`
2. **🏗️ Bygga gatewayen** — `AGENTS.md` som post 0, sedan `ARCHITECTURE` (denna fil) →
   `MCP-API` → `BUILD` → PM-modulen → `SAFETY` (+ `THREAT-MODEL`, `MEMORY-RETRIEVAL`,
   `TESTING`) → `REVIEW-RESPONSE` → `PACKAGING` → `OPEN-GAPS` med alla `FEATURE-*.md`
   som underposter → `CODE-WORKFLOW`
3. **🚀 Installera och driva** — `USER-MANUAL` som post 0, sedan `INSTALL`,
   `QUICK-INSTALL`, `EXPOSE`, `WIZARD`, `SETUP-UI`, `DOCTOR`, `OBSERVABILITY`,
   `SECURITY`, `SECRETS`, `AI-CLIENTS`, `BACKENDS`, `PER-USER-OAUTH`, `MAIL`,
   `BACKUP`, `UPDATE` m.fl.
4. **💼 Sälja installation/hosting** — `BUSINESS-CASE` (+ `SWOT`), `SERVICE-PROVIDERS`,
   `WHITE-LABEL`
5. **Snabbkarta** — fråga→dokument-tabell, ~40 rader

Hygienen upprätthålls maskinellt: `scripts/check-docs-index.py` failar om något
`docs/*.md` saknas i `INDEX.md`. Körs i CI och som `make docs-check`.

**Lägger du till en fil i `docs/` måste du lägga in den i `INDEX.md`, annars faller CI.**
