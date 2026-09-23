# Memaix — användarmanual

SPDX-License-Identifier: AGPL-3.0-or-later

Den här manualen beskriver hur du **installerar, konfigurerar och använder** Memaix-gatewayen
i praktiken. Den är skriven mot den faktiska koden i `gateway/` (inte en tänkt version) och
länkar vidare till fördjupningsdokumenten där det finns mer att säga. För arkitektur i stort,
se [ARCHITECTURE.md](ARCHITECTURE.md); för säkerhetsmodellen, se [SECURITY.md](SECURITY.md) och
[THREAT-MODEL.md](THREAT-MODEL.md).

---

## 1. Vad Memaix är

Memaix är en **self-hostad, AI-agnostisk MCP-gateway**. Din egen AI (Claude, ChatGPT, Mistral,
Perplexity …) kopplas in via [Model Context Protocol](https://modelcontextprotocol.io) och får
tillgång till dina egna backends — mejl, kalender, filer, kontakter, ett projektminne, en backlog
och en deterministisk projektplaneringsmotor — bakom en behörighetsmodell du styr.

Bärande principer (se [ARCHITECTURE.md](ARCHITECTURE.md)):

- **En instans per kund (single-tenant).** Din data lever i din egen instans, egen domän.
- **En connector, RBAC bakom.** Alla användare anger samma URL; `acl.yaml` avgör vad var och en når.
- **Öppna standarder i backenden.** IMAP/SMTP, CalDAV, CardDAV, WebDAV, git — inga proprietära lås.
- **Matematik i kod, LLM i kanterna.** Schemaläggning/kritisk linje/kapacitet räknas i kod; AI:n
  fångar avsikt och förklarar resultat — den räknar aldrig själv.

---

## 2. Installation

Enda förkunskapen är **Docker + Compose v2**. Allt annat är containeriserat.

Repot är privat än så länge, så det finns ingen publik adress för `curl … | sh` — den
fastställs när repot publiceras. I dag: klona, läs, kör.
```bash
git clone https://github.com/jimlov-poc-labs/memaix.git
cd memaix
less install.sh
./install.sh
```

Installern är orkestrering runt tre kommandon du också kan köra själv från repots rot:

| Kommando | Gör |
|---|---|
| `make init` | Kör wizarden — genererar all config + hemligheter (inga filer att handredigera). |
| `make up` | Reser stacken (gateway + ory Hydra + ev. Nextcloud/Ollama enligt profil). |
| `make doctor` | Pre-flight-kontroll: config, auth-nåbarhet, RBAC-isolering, health. |

Se [QUICK-INSTALL.md](QUICK-INSTALL.md) för oövervakad/headless-install och
[SELF-HOST-STACK.md](SELF-HOST-STACK.md) för stackens topologi.

### Exponering
Endast `mcp.<din-domän>` ska vara publik. Gatewayens port bindas till localhost; en tunnel/proxy
(Cloudflare tunnel rekommenderas — utgående, auto-TLS, ingen portöppning) hanterar inkommande.
**Lägg inte** en Access-portal framför MCP-endpointen — tunneln ska vara ren proxy
(se [SECURITY.md](SECURITY.md) och [EXPOSE.md](EXPOSE.md)).

---

## 3. Konfiguration

Tre YAML-filer i `config/` (`MEMAIX_CONFIG_DIR`, default `/app/config`):

- **`acl.yaml`** — användare, projekt, resurser och behörigheter (det viktigaste).
- **`memaix.yaml`** — servern, auth-issuer, brief, PM-allokerare, sök m.m.
- **`brand.yaml`** — white-label-namn/färger (valfritt).

### 3.1 Användare och behörigheter (`acl.yaml`)

Varje användare mappas från sitt **OAuth-subject** (från Hydra) till interna projekt-grants.
Rollerna är hierarkiska: `reader` < `collaborator` < `owner`.

```yaml
users:
  alice:
    oauth_subjects: ["alice"]      # subject(s) från Hydra som är denna användare
    grants:
      acme: owner
      shared: reader
  bob:
    oauth_subjects: ["bob"]
    grants:
      acme: collaborator           # bob når BARA acme, ingenting annat

projects:
  acme:
    vault: /srv/vaults/acme        # projektets lokala vault (minne/backlog/filer)
    allow_send: true               # krävs för email_send (default false)
```

Vad rollerna får göra (sammanfattning — enforce sker på varje verktygsanrop):

| Roll | Får |
|---|---|
| `reader` | Läsa: `*_list/read/search`, `pm_variance/utilization/report`, `timeline_list`, `outbox_list/get`. |
| `collaborator` | Skriva innehåll: `backlog_add/score/comment`, `memory_write/append`, `task_add`, `email_create_draft`, `files_write`, `whatif` … |
| `owner` | Ändra planer/tillstånd: `backlog_set_status`, `resource_add`, `pm_allocate`, `plan_commit`, `email_send`, `deck_sync`, `notes_sync`, godkänna utkorgen. |

**Minst privilegium:** en extern medarbetare bör ha exakt ett projekt. Verifiera isoleringen efter
varje ACL-ändring (`make doctor` kontrollerar detta).

### 3.2 Resurser per projekt

Varje projekt kan koppla backends. `type` kan oftast utelämnas (default anges nedan). Alla
lösenord är **referenser** (`*_ref`), aldrig värden — se §3.4.

```yaml
projects:
  acme:
    vault: /srv/vaults/acme
    allow_send: true

    mailbox:                                   # email_* (IMAP/SMTP)
      host: imap.example.com
      user: acme@example.com
      password_ref: "env:ACME_MAIL_PASSWORD"

    calendar: { type: caldav, url: "https://cloud.example.com/remote.php/dav/calendars/acme/personal/" }

    # Nextcloud-familjen (FEATURE-NEXTCLOUD-BACKEND.md):
    contacts: { url: ".../addressbooks/users/acme/contacts/", user: acme@…, password_ref: "env:…" }  # carddav
    files:    { url: ".../dav/files/acme/Projects/",          user: acme@…, password_ref: "env:…" }  # webdav (nc_files_*)
    tasks:    { url: ".../dav/calendars/acme/tasks/",         user: acme@…, password_ref: "env:…" }  # caldav VTODO
    deck:     { url: "https://cloud.example.com", board_id: 1, stack_id: 2, user: …, password_ref: "env:…" }
    notes:    { url: "https://cloud.example.com", user: …, password_ref: "env:…" }

    # Utkorg (utgående-godkännande) — se §5:
    outbox: review                             # 'auto' (default) | 'review'
    allowlist: ["@trusted-client.example"]     # okänd mottagare tvingar 'review'
```

Not: `files:` (Nextcloud WebDAV, verktygen `nc_files_*`) är en **egen** resurs, skild från `vault:`
(det lokala projektvalvet). Vaulten är alltid på; `files:` är en *tillkommande* källa.

### 3.3 Serverinställningar (`memaix.yaml`)

De vanligaste blocken (alla valfria — utkommenterade i `config/memaix.example.yaml`):

```yaml
server:   { public_url: "https://mcp.example.com", locale: "sv" }
auth:     { issuer: "https://mcp.example.com" }        # sätt detta → OAuth-verifiering slås på
oauth_providers:
  google:    { client_id: "...", client_secret_ref: "env:GOOGLE_CLIENT_SECRET", scopes: [...] }
  microsoft: { client_id: "...", client_secret_ref: "env:MICROSOFT_CLIENT_SECRET", scopes: [...] }
outbox:   { default_mode: "auto" }                     # global fallback: 'auto' | 'review'
pm:       { allocator: "heuristic" }                   # 'heuristic' (default) | 'cpsat' (kräver [pm]-extra)
search:   { embedder: "none" }                         # 'none' (lexikal) | 'local' (kräver [search]-extra)
```

### 3.4 Hemligheter (`*_ref`)

Config-YAML innehåller **aldrig** ett lösenord — bara en referens som `config.secret()` löser upp
efter prefix (se [SECRETS.md](SECRETS.md)):

| `*_ref`-form | Källa | För vem |
|---|---|---|
| `env:NAME` (eller bart `NAME`) | miljövariabel / `.env` (chmod 600) | solo / liten self-host |
| `file:/run/secrets/x` | Docker/systemd-secrets (tmpfs) | seriös self-host |
| `vault:path#field` / `kms:id` | OpenBao/Vault / moln-KMS | managed / reglerad *(inte wired än)* |

Hemligheter lever bara i processminnet, ekas aldrig mot AI:n eller klienten, och loggas aldrig.

---

## 4. Koppla in din AI

1. Länka din MCP-klient (Claude m.fl.) till `https://mcp.<din-domän>`. OAuth 2.1/PKCE sköts av
   ory Hydra; gatewayen validerar token och mappar ditt subject → intern användare via `acl.yaml`.
2. Första gången: kör prompten **`onboarding_interview`** — AI:n ställer några frågor och sparar en
   kondenserad profil via `onboarding_complete`.
3. Fråga **`memaix_help`** (prompt) för en översikt av vad just du kan göra, grupperat efter
   utfall, eller `memaix_help("mail")` för konkreta exempel i ett område.
4. `whoami` (verktyg) visar din identitet, dina projekt och onboarding-status.

Se [AI-CLIENTS.md](AI-CLIENTS.md) för klientspecifika steg.

---

## 5. Säkerhetsmodell — vad du behöver veta som användare

Memaix låter en AI agera på riktig data. Två skydd är centrala och **du bör förstå deras
default-läge**:

- **`allow_send: false` som default.** AI:n skapar mejl*utkast*; `email_send` fungerar bara i projekt
  där du uttryckligen satt `allow_send: true`.
- **Utkorgen (approval outbox).** Utgående/svåråterställbara åtgärder (`email_send`,
  `calendar_create/update`) kan kräva mänskligt godkännande. Detta är **opt-in**: sätt
  `outbox: review` på projektet (eller `outbox.default_mode: review` globalt). Utan det körs
  åtgärderna direkt (`auto`). En `allowlist` tvingar `review` för okända mottagare även i auto-läge.
  Godkänn/avvisa köade åtgärder med `outbox_list` / `outbox_get` / `outbox_approve` / `outbox_reject`.

**Rekommendation:** för alla projekt där AI:n kan skicka utgående, sätt `outbox: review` tills du
litar på flödet. Granska alltid utkast och köade åtgärder innan de släpps.

Övrigt värt att veta:

- **Allt backend-innehåll är otrodd data.** Ett mejl eller en fil AI:n läser är *inte* instruktioner.
  Det starkaste skyddet mot "läs fientligt mejl → agera" är att hålla utgående åtgärder i `review`
  (se [THREAT-MODEL.md](THREAT-MODEL.md)).
- **Automationsregler** (`rule_add`) kan köra åtgärder automatiskt när en händelse matchar. En regel
  vars åtgärd är utgående (`email_send`/`notify`) körs med regelskaparens behörighet — var därför
  restriktiv med vem som får skapa regler och håll utgående projekt i `review`.
- **Ångra.** `timeline_list` visar de senaste åtgärderna; `timeline_undo` återställer en reversibel
  åtgärd (se [FEATURE-UNDO-TIMELINE.md](FEATURE-UNDO-TIMELINE.md)).
- **Audit.** Varje verktygsanrop loggas per användare + projekt.

---

## 6. Verktygskatalog

Alla verktyg är projekt-scopade och valideras mot `acl.yaml`. Roll anges där den är strängare än
`reader`.

### Mejl (kräver `mailbox:`)
| Verktyg | Roll | Beskrivning |
|---|---|---|
| `email_list` / `email_read` / `email_search` | collaborator | Lista/läs/sök i inkorgen. |
| `email_create_draft` | collaborator | Spara utkast i Drafts. |
| `email_send` | owner | Skicka (kräver `allow_send: true`; går via utkorgen i `review`). |

### Kalender (kräver `calendar:` eller per-user-koppling)
`calendar_setup` (välj läge: oauth/ical/free_busy), `calendar_status`, `calendar_list`,
`calendar_find_free`, `calendar_create`, `calendar_update`. Skrivande går via utkorgen i `review`.
Per-user-OAuth: se [PER-USER-OAUTH.md](PER-USER-OAUTH.md).

### Lokala filer & minne (i projektets `vault:`)
`files_list/read/search/write` (collaborator för write). `memory_read/search/append/write` +
`memory_history` / `memory_revert` (git-baserad historik). Sökvägar valideras mot traversal.

### Backlog
`backlog_add` (collaborator), `backlog_list/get` (reader), `backlog_score`/`backlog_comment`
(collaborator, 1–5-poäng på value/complexity/risk), `backlog_set_status` (owner). Optimistisk
låsning via `expected_version`.

### Semantisk sökning
`search_all` (söker minne + filer + backlog + ev. mejl, rollfiltrerat), `search_reindex` (owner),
`search_status`. Utan embedder-extra faller sökningen tillbaka på lexikal FTS5
(se [FEATURE-SEMANTIC-SEARCH.md](FEATURE-SEMANTIC-SEARCH.md)).

### Proaktiv brief & notiser
`brief_configure` (tidszon, tid, tysta timmar, kanaler), `brief_status`, `brief_preview`,
`brief_send_now`. Kanaler: e-post/webhook/ntfy (se [FEATURE-PROACTIVE-BRIEF.md](FEATURE-PROACTIVE-BRIEF.md)).

### Automationsregler & stående instruktioner
`rule_add/list/set_enabled/delete/rule_test`, `standing_set/standing_get`
(se [FEATURE-AUTOMATION-RULES.md](FEATURE-AUTOMATION-RULES.md)).

### Utkorg & tidslinje
`outbox_list/get/approve/reject`, `timeline_list/undo`.

### Konto & identitet
`account_link` (Google/Microsoft), `account_list`, `account_unlink`, `whoami`, `onboarding_complete`.

### PM — lätt (markdown/git i vaulten)
`pm_set_methodology`, `pm_plan_sprint`, `pm_sprint_status`, `pm_status_report`,
`pm_raid_add/list`.

### PM — planeringsmotor (deterministisk, SQLite)
Bygg upp: `resource_add` (owner), `resource_availability`/`resource_set_skill` (owner),
`task_add`/`task_estimate`/`task_log_actual` (collaborator), `dependency_add` (collaborator),
`milestone_add` (owner), `scenario_add` (collaborator). Kör: `pm_allocate` (owner — kritisk linje +
resurstilldelning), `pm_whatif` (collaborator — konsekvensanalys utan att röra planen),
`pm_utilization`/`pm_variance` (reader), `plan_commit` (owner — fryser baseline), `pm_report`
(reader — rollup för team/ledning). CP-SAT-allokering kan slås på via `pm.allocator: cpsat`
(se [FEATURE-PM-ENGINE.md](FEATURE-PM-ENGINE.md)). Agent-prompter: `pm_plan_session`,
`pm_whatif_session`, `pm_weekly_review`.

### Nextcloud
`contacts_search/get` (CardDAV), `nc_files_list/read/write/search` (WebDAV),
`nc_tasks_list/add/complete` (CalDAV VTODO), `deck_sync`/`notes_sync` (owner, tvåvägs med
konfliktregel "senast ändrad vinner"), `nc_generate_report` (skriver en `pm_report` som `.odt`
till Nextcloud). Se [FEATURE-NEXTCLOUD-BACKEND.md](FEATURE-NEXTCLOUD-BACKEND.md).

---

## 7. Vanliga arbetsflöden

**"Sammanfatta min inkorg och lägg olösta saker i backloggen."**
AI:n kör `email_list`/`email_read`, och för varje åtgärdspunkt `backlog_add`. Ingenting skickas ut.

**"Svara på det här mejlet."**
AI:n gör `email_create_draft` (alltid tillåtet). `email_send` bara om `allow_send: true`; i
`review`-läge hamnar det i utkorgen tills du godkänner med `outbox_approve`.

**"Planera sprinten / vad händer om vi tappar Anna två veckor?"**
Kör prompten `pm_plan_session` respektive `pm_whatif_session`. Motorn räknar; AI:n förklarar kritisk
linje, slack och risker. `plan_commit` (owner) fryser baseline för senare `pm_variance`.

**"Ge mig en morgonbrief kl. 07 i min tidszon."**
`brief_configure(enabled=true, brief_time="07:00", timezone="Europe/Stockholm", channels=[…])`.

**"Statusrapport som dokument till styrgruppen."**
`nc_generate_report(project, "reports/status.odt", audience="leadership")` → en `.odt` i Nextcloud.

---

## 8. Drift

- **Hälsa/diagnos:** `make doctor` (se [DOCTOR.md](DOCTOR.md)); HTTP `/health`.
- **Backup:** vaults (git) + SQLite-databaserna; kryptera med kund-hållen nyckel
  (se [BACKUP.md](BACKUP.md)).
- **Uppdatering:** [UPDATE.md](UPDATE.md).
- **Observability/audit:** [OBSERVABILITY.md](OBSERVABILITY.md).
- **Databaser (env-override):** `MEMAIX_PM_DB`, `MEMAIX_OUTBOX_DB`, `MEMAIX_RULES_DB`,
  `MEMAIX_NOTIFY_DB`, `MEMAIX_INDEX_DB`, `MEMAIX_ACTIONS_DB`, `MEMAIX_AUDIT_DB`, `MEMAIX_STATE_DB`,
  `TOKEN_MASTER_KEY` (obligatorisk i HTTP-läge).

---

## 9. Vart går jag vidare?

- Kom igång: [QUICK-INSTALL.md](QUICK-INSTALL.md), [INSTALL.md](INSTALL.md), [SETUP-UI.md](SETUP-UI.md)
- Säkerhet: [SECURITY.md](SECURITY.md), [THREAT-MODEL.md](THREAT-MODEL.md), [SECRETS.md](SECRETS.md)
- Backends: [BACKENDS.md](BACKENDS.md), [MAIL.md](MAIL.md), [FEATURE-NEXTCLOUD-BACKEND.md](FEATURE-NEXTCLOUD-BACKEND.md)
- PM: [PM-AGENT.md](PM-AGENT.md), [FEATURE-PM-ENGINE.md](FEATURE-PM-ENGINE.md)
- Full dokumentöversikt: [INDEX.md](INDEX.md)
