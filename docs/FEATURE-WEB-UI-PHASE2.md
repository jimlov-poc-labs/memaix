# Funktion #25 — Webb-UI Fas 2 (Fas D)

SPDX-License-Identifier: AGPL-3.0-or-later

Designdok + byggspec för den andra fasen av Memaix webb-UI: global sökning i
topbaren (bygger på funktion #2 — semantisk sökning), brief-inställningar i
settings-fliken (bygger på funktion #1 — proaktiv brief), admin-skrivoperationer
med kill-switch och projekteditering, MFA-krav för admin-routes samt en
ångra-tidslinje i board-vyn (bygger på funktion #5 — ångra & åtgärdstidslinje).

Detta är Fas D i byggordningen (WEB-UI-SPEC §6). **Påbörjas inte förrän Fas A
(funktion #22), Fas B och Fas C är kompletta, och förrän funktion #1 och #2 är
byggda.** Specen är skriven för att kunna implementeras självständigt per
delkomponent — se [Byggordning](#5-byggordning).

---

## 1. Vad användaren upplever

### Global sökning

I topbarens mitt finns ett sökfält (`⌘K` eller klick öppnar det). Användaren
skriver en fråga i klarspråk — *"vad lovade vi kunden om leverans"* — och får
ett dropdown-resultat med rankade träffar medan de skriver (debounce 300 ms).
Varje träff visar projektnamn, källtyp (minne / fil / backlog / mail), en
titel och ett textutdrag. Klick på en träff navigerar till rätt kontextvy
(kortmodal, minnesvy, mailvy) eller kopierar referensen till urklipp om vyn
ännu ej är byggd.

Sökningen respekterar ACL: resultat inkluderar bara projekt användaren har
tillgång till. Semantisk rang (om inbäddningsmodellen är aktiv) och lexikal FTS5
kombineras via RRF — identisk backend-logik som MCP-verktyget `search_all`.

### Brief-inställningar

En ny flik **Brief** under `/app/settings` låter användaren slå på och
konfigurera sin dagliga sammanfattning. Formuläret visar: aktivera/inaktivera,
tid (HH:MM), tidszon (IANA-väljare med autocomplete), tysta timmar (start/slut),
kanaler (e-post, webhook) och projekturval. Spara → `POST /app/api/brief`.
Statusruta visar när nästa brief skickas.

### Admin-skrivoperationer

Admin-sektionen under `/app/admin` utökas från läsvyer till:

- **Användare-fliken** — kill-switch per användare (toggle → confirmation-dialog →
  `POST /app/api/admin/users/{uid}/disable`). Rolltilldelning infogas/ändras via
  redigerbar grants-matris med dropdown per projekt. Ändringar kräver bekräftelsedialog.
- **Projekt-fliken** — redigera `allow_send`, `outbox_mode`, resurssökvägar (vault, mailbox, calendar). Spara-knapp med diff-visning ("du ändrar X från Y till Z").
- **Audit-fliken** — filtrering på user, projekt, tool, ok, tidsintervall; expanderbara
  felrader; sidnumrering.

### MFA-krav för admin

Alla `/app/admin`-routes (GET och POST) kräver att användaren är admin *och* har
genomfört TOTP-verifiering i den aktuella webbsessionen. Vid saknad MFA-verifiering
redirectas användaren till `/app/admin/mfa` — ett TOTP-inmatningsformulär. Lyckad
TOTP sätter `memaix_mfa_verified_at` i signerad cookie (TTL 8 h); utgången cookie
kräver ny verifiering. QR-kod för TOTP-setup visas en gång vid `/app/admin/mfa/setup`
(kräver ny TOTP för att spara).

### Ångra-tidslinje

I board-vyn (`/app/board`) öppnar en knapp "Tidslinje ⏱" en drawer på höger sida
med de senaste 50 reversibla åtgärderna. Varje rad visar: relativ tid, åtgärds-typ,
berört objekt och en **Ångra**-knapp. Klick → confirmation-dialog → `POST
/app/api/timeline/{action_id}/undo` → optimistisk borttagning av raden +
toast "Ångrat." Irreversibla åtgärder (t.ex. skickat mejl) visas med grå
"Kan ej ångras"-text och en ikon-info med förklaring.

---

## 2. Nyckelbeslut

1. **Sökresultat byggs i backend — aldrig i JS.** `GET /app/api/search?q=...`
   anropar `search_all()` (funktion #2) och returnerar JSON med rankade träffar.
   Frontend renderar — söker aldrig direkt mot `EmbeddingStore` eller FTS5.

2. **Brief-inställningar är tunn UI ovanpå `NotifyStore`.** `POST /app/api/brief`
   anropar `NotifyStore.set_prefs()` + beräknar `next_run` — samma kod som
   MCP-verktyget `brief_configure`. Ingen logikduplicering.

3. **MFA-sessionen lever i cookie, inte i server-state.** Cookie innehåller
   `{user}:{mfa_ts}:{sig}` signerad med `HYDRA_SYSTEM_SECRET`. Serversidan
   verifierar signaturen och kontrollerar att `mfa_ts` är tillräckligt färsk
   (< 8 h). Ingen server-side session-store behövs.

4. **TOTP-hemligheten lagras i `acl.yaml` under `users.{uid}.totp_secret_ref`.**
   Värdet är en `*_ref`-referens — aldrig i klartext. `config.secret()` löser den.
   Använd `file:`-schema (t.ex. `file:/run/secrets/totp_alice`, läge 0600) för
   hemligheter som skrivs vid runtime: `env:`-refs läses bara vid processtart och
   blir inte live förrän omstart. Används bara av admin.

5. **Admin-skrivoperationer skriver till `acl.yaml` på disk via `AclWriter`.**
   En ny hjälpklass `acl_writer.py` gör atomisk skrivning (tmp-fil + rename) med
   backup-rotation (behåller de 3 senaste). Ändringen audit-loggas.

   > **Reconciliation 2026-07:** Den körande gatewayen cachar `Acl` i en
   > modulglobal (`server._acl` via `_get_acl()`) — en diskskrivning syns alltså
   > **inte** förrän cachen töms. Använd den nu tillagda `server.reload_acl()`
   > (tömmer cachen och läser om från disk) direkt efter varje lyckad AclWriter-
   > skrivning. Det fiktiva `acl._cache = None` finns inte — anropa `reload_acl()`.

6. **Tidslinjens data kommer från ångra/tidslinje-funktionen (#5).** Ingen
   logikduplicering — återanvänd de faktiska tool-funktionerna.

   > **Reconciliation 2026-07 (fiktiva namn):** Klassen heter `ActionsStore`
   > (`timeline/store.py`), inte `ActionLog`, och den har **ingen** `query`- eller
   > `undo`-metod. Rätt anrop:
   > - Lista: `ActionsStore.list(projects: list[str], limit=50)` (samma som MCP-
   >   verktyget `timeline_list`). Filtrera projekt via `acl.visible_projects(user)`.
   > - Ångra: den fristående funktionen `timeline.undo.undo(store, acl, user, action_id)`
   >   (samma som MCP-verktyget `timeline_undo`) — den enforce:ar samma roll som
   >   originalåtgärden krävde. Anropa den, dubblera inte inverslogiken.

7. **Faser är addativa — ingen befintlig kod rivs.** Fas D lägger till nya routes,
   nya HTML-sidor och ny JS utan att modifiera Fas A–C-kod (utöver att lägga till
   Brief-fliken i `settings.html` och Tidslinje-knappen i `board.html`).

---

## 3. Översikt

```
Browser                      Gateway
───────                      ───────

GET /app/api/search?q=…  ──► web/api/search.py
                             acl.visible_projects(user)
                             search_all(acl, user, cfg, …) [funktion #2]
                         ◄── [{project, source_type, ref, title, snippet}]

POST /app/api/brief      ──► web/api/brief.py
                             NotifyStore.set_prefs()      [funktion #1]
                             beräkna next_run
                         ◄── {next_run, status: "ok"}

GET /app/api/me          ──► (Fas A, utökas) + mfa_verified_at ur cookie

GET /app/admin           ──► MFA-check → /app/admin/mfa om ej verifierad
POST /app/api/admin/…    ──► web/api/admin_write.py
                             AclWriter / NotifyStore / …
                             audit_log(user, "admin_*", …)

GET /app/api/timeline    ──► ActionsStore.list()          [funktion #5]
POST /app/api/timeline/{id}/undo ──► timeline.undo.undo() [funktion #5]

/app/admin/mfa           ──► TOTP-formulär
POST /app/admin/mfa/verify ► TOTP.verify() → sätter mfa-cookie

Klient-JS (nytt i Fas D)
─────────────────────────
search-bar.js: debounce, dropdown, keyboard-navigering (↑↓ Enter Escape)
brief-settings.js: formulärvalidering (HH:MM, IANA-tz), kanal-builder
admin-users.js: grants-matris, kill-switch, confirmation-dialog
admin-audit.js: filter-form, sidnumrering, expand/collapse
timeline-drawer.js: drawer, undo-flow, optimistisk UI
mfa.js: TOTP-input, QR-rendering (qrcode.js eller server-renderad SVG)
```

---

## 4. Komponenter

### 4.1 Global sökning — `web/static/search-bar.js` + `GET /app/api/search`

**Topbar-markup** (läggs till i `shell.html` från Fas A):

```html
<div class="search-wrap" id="search-wrap" hidden>
  <input id="search-input" type="search" placeholder="Sök…" autocomplete="off"
         aria-label="Sök i Memaix" aria-controls="search-results" aria-expanded="false">
  <ul id="search-results" role="listbox" hidden></ul>
</div>
<button id="search-open" aria-label="Öppna sök (⌘K)">🔍</button>
```

`hidden`-attributet tas bort av `search-bar.js` när funktion #2 är aktiv
(`me.search_enabled`). Om sökning ej är aktiverad (embedder saknas, funktionen ej byggd)
visas inget sökfält — inga broken states.

**`search-bar.js`-beteende:**

- `⌘K` / `Ctrl+K` → fokusera `#search-input`, visa `#search-wrap`.
- Escape → rensa + stäng.
- Input-event med 300 ms debounce → `api('GET', '/app/api/search?q=' + encodeURIComponent(q) + '&limit=8')`.
- Resultat renderas som `<li role="option">` med ikon per `source_type` (🧠 memory, 📁 file, 📋 backlog, ✉️ mail), projektnamn, titel och snippet (max 120 tecken, klippt med "…").
- Tangentbordsnavigering: ↑↓ = flytta fokus, Enter = aktivera, Escape = stäng.
- Klick utanför → stäng.
- `aria-expanded`, `aria-activedescendant` för tillgänglighet.

**`GET /app/api/search` — backend:**

```python
async def api_search(request: Request) -> JSONResponse:
    """
    GET /app/api/search?q=<sträng>&limit=<int>&projects=<komma-sep>
    Anropar search_all() (funktion #2).
    Kräver autentisering (401 annars).
    Returnerar {results: [...], semantic: bool, projects_searched: [...]}.
    """
```

Projektfilter tolkas som komma-separerad lista om `projects`-param finns;
valideras mot `visible_projects(user)`.

### 4.2 Brief-inställningar — `web/pages/settings-brief.html` + `web/api/brief.py`

**Settings-sidan** (`/app/settings`) utökas med en tredje flik **Brief** som
bara renderas om funktion #1 är byggd (`me.brief_available`).

Flik-innehåll:

```
┌ Brief ─────────────────────────────────────────────────┐
│  Aktivera dagsbrief          [●]                       │
│  Tidpunkt  [07:00]           Tidszon [Europe/Stockholm] │
│  Tysta timmar  [22:00] — [07:00]                       │
│  Kanaler                                               │
│  ┌──────────────────────────────────────────────────┐  │
│  │ + e-post  mrjimlov@gmail.com              [✕]    │  │
│  │ + webhook (Slack)  env:BRIEF_SLACK_WEBHOOK [✕]   │  │
│  │ [+ Lägg till kanal]                              │  │
│  └──────────────────────────────────────────────────┘  │
│  Projekt att inkludera (tomt = alla)                   │
│  [acme ✓]  [project-a]                                 │
│  Nästa brief: imorgon 07:00 (Europe/Stockholm)         │
│                                          [Spara]       │
└─────────────────────────────────────────────────────────┘
```

**`brief-settings.js`:**

- Laddar befintliga inställningar via `GET /app/api/brief` → `{prefs, next_run}`.
- Tidszon-fältet är ett `<input list="tz-list">` med ett `<datalist>` förfyllt med
  vanliga IANA-tidszoner (inte fullständig lista — topp ~50 + användarens nuvarande).
- Kanal-builder: "Lägg till kanal"-knapp öppnar en inline-modal med typ-väljare
  (e-post / webhook) och relevanta fält. `url_ref`-fält accepterar
  `env:VAR_NAME`-syntax och visar tydlig tooltip "referens, inte klartext-URL".
- Validering klient-side: HH:MM-format (regex), tidszon (måste finnas i listan),
  e-postformat.
- `POST /app/api/brief` → toast "Brief-inställningar sparade. Nästa: {next_run}."
- Felhantering: om servern svarar 422 (ogiltig tz, ogiltig tid) visas felmeddela
  bredvid fältet, inte bara i toast.

**`GET /app/api/brief`:**

```python
async def api_brief_get(request: Request) -> JSONResponse:
    """Returnerar {prefs: dict | null, next_run: str | null}."""
```

**`POST /app/api/brief`:**

```python
async def api_brief_post(request: Request) -> JSONResponse:
    """
    Body: {enabled, brief_time, timezone, channels, quiet_hours, projects}.
    Anropar NotifyStore.set_prefs() + beräknar next_run via next_brief_epoch().
    Audit-loggar 'brief_configure'.
    Returnerar {ok: true, next_run: ISO-sträng}.
    422 vid ogiltig tz eller tid.
    """
```

### 4.3 Admin-skrivoperationer — `web/api/admin_write.py` + `web/pages/admin.html`

**Kill-switch (disable user):**

```python
async def api_admin_user_disable(request: Request) -> JSONResponse:
    """
    POST /app/api/admin/users/{uid}/disable  {disabled: bool}
    Kräver: is_admin + MFA-verifierad.
    Skriver users.{uid}.disabled = true/false till acl.yaml via AclWriter.
    Enforce förhindrar disabled-användare i _require_user.
    Audit-loggar 'admin_user_disable'.
    """
```

UI: varje rad i användartabellen har en toggle-switch. Klick → `confirm("Inaktivera {uid}? De loggas ut omedelbart.")` → disable-anrop → rad markeras
med rödaktig bakgrund + "Inaktiverad"-chip. Aktiveringslänk i samma toggle.

**Grants-matris:**

```
┌ Användare ─────────────────────────────────────────────────┐
│ Användare    │ acme      │ project-a │ Aktiv │ Kill-switch  │
│ alice        │ owner ▾   │ owner ▾   │ ✓     │ [Inaktivera] │
│ bob          │ reader ▾  │ —         │ ✓     │ [Inaktivera] │
│ charlie      │ —         │ collab ▾  │ ✗ 🔴 │ [Aktivera]   │
└────────────────────────────────────────────────────────────┘
```

Varje cell är en `<select>` med alternativ `— (ingen)`, `reader`, `collaborator`,
`owner`. Ändring → fält markeras dirty → "Spara ändringar"-knapp aktiveras →
bekräftelsedialog ("Du ändrar 2 roller") → `PATCH /app/api/admin/users/{uid}/grants
{grants: {project: role, ...}}`.

**Projekteditering:**

```
┌ Projekt: acme ────────────────────────────────────────────┐
│ vault         /vaults/acme                     [Ändra]    │
│ mailbox       imap://alice@mail.example.com    [Ändra]    │
│ calendar      dav://cal.example.com/alice      [Ändra]    │
│ allow_send    ● Ja  ○ Nej                                 │
│ outbox_mode   ◉ approve_all  ○ skip  ○ auto               │
│                                         [Spara] [Avbryt]  │
└───────────────────────────────────────────────────────────┘
```

`PATCH /app/api/admin/projects/{project}` — skriver till `acl.yaml` via `AclWriter`.

**`AclWriter` — kontrakt:**

```python
class AclWriter:
    """Atomisk skrivning av acl.yaml med backup-rotation."""
    def __init__(self, acl_path: Path) -> None: ...

    def set_user_disabled(self, uid: str, disabled: bool) -> None: ...
    def set_grants(self, uid: str, grants: dict[str, str]) -> None: ...
    def set_project_field(self, project: str, key: str, value: Any) -> None: ...

    def _write_atomic(self, data: dict) -> None:
        """tmp-fil + os.replace; bevarar 3 backuper (.bak1/.bak2/.bak3)."""
```

**Kill-switch fungerar redan i kärnan (MEX-025-beroende, klart):** `acl.py::enforce`
nekar nu en användare med `disabled: true` allt (även admin) och `Acl.is_disabled`
finns. `set_user_disabled` behöver alltså bara skriva flaggan + anropa
`server.reload_acl()` — men **måste** ha en lockout-spärr:

- En admin får **inte** avaktivera sig själv.
- Den **sista aktiva admin:en** får inte avaktiveras (annars kan ingen slå på
  någon igen utan manuell acl.yaml-redigering).

Returnera `409 last_admin` / `409 self_disable` från web-routen om spärren träffar.
Efter varje skrivning: `server.reload_acl()` så flaggan slår igenom direkt.

### 4.4 MFA — `web/pages/admin-mfa.html` + `web/api/mfa.py`

**Cookie-schema:**

```
memaix_mfa = "{user}:{ts}:{sig}"
sig = HMAC-SHA256(HYDRA_SYSTEM_SECRET, f"{user}:{ts}")[:32]
TTL: 8 timmar
```

**MFA-check i route-middleware:**

```python
def _require_mfa(request: Request, user: str) -> bool:
    """
    Returnerar True om MFA-cookie är giltig och < 8 h gammal.
    Används av alla /app/admin-handlers.
    """
```

**`GET /app/admin/mfa` — TOTP-formulär:**

Visar ett formulär med ett 6-siffrigt inmatningsfält och en submit-knapp.
Om TOTP-hemligheten ej är uppsatt: visa "Du behöver konfigurera TOTP →
Gå till setup." med länk till `/app/admin/mfa/setup`.

**`GET /app/admin/mfa/setup` — setup-flöde:**

1. Generera ny TOTP-hemlighet (`pyotp.random_base32()`).
2. Visa QR-kod som server-renderad SVG (via `qrcode`-biblioteket i Python,
   format `otpauth://totp/Memaix:{user}?secret=...`).
3. Visar 6-siffrigt bekräftelsefält — användaren verifierar att appen fungerar
   innan hemligheten sparas.
4. `POST /app/admin/mfa/setup {code}` — verifiera koden mot den *väntande*
   hemligheten, spara sedan.

> **Reconciliation 2026-07 (två reella buggar i enrollment):**
>
> 1. **Väntande hemlighet måste överleva mellan GET och POST.** MFA-sessionen är
>    cookie-only (nyckelbeslut 3) — det finns inget server-side session-state.
>    Bär därför den nygenererade hemligheten i en kortlivad **signerad**
>    enrollment-cookie (`memaix_mfa_enroll`, HMAC med `HYDRA_SYSTEM_SECRET`, TTL
>    ~10 min) och verifiera POST-koden mot *den* — annars regenereras hemligheten
>    per request och verifieringen kan aldrig lyckas. Rensa cookien efter sparning.
> 2. **Spara som `file:`-ref, inte `env:`.** `config.secret()` läser `env:`-refs
>    bara vid processtart — en hemlighet skriven till `.env` vid runtime blir
>    **inte** live förrän gatewayen startas om, så efterföljande `pyotp.TOTP(
>    config.secret(ref))` skulle misslyckas. Spara i stället som
>    `file:/run/secrets/totp_{user}` (0600) som `config.secret()` läser live från
>    disk. `totp_secret_ref` i `acl.yaml` pekar på filen; skriv den via AclWriter-
>    grannen och anropa `server.reload_acl()`.

**`POST /app/admin/mfa/verify`:**

```python
async def api_mfa_verify(request: Request) -> JSONResponse:
    """
    Body: {code: "123456"}.
    Verifierar mot TOTP-hemligheten (pyotp.TOTP(secret).verify(code, valid_window=1)).
    Vid OK: sätter memaix_mfa-cookie (httponly, samesite=strict, TTL 8h), redirect /app/admin.
    Vid fel: returnerar 401 {error: "Fel kod"}.
    Max 5 försök per 10 min (rate-limit via SQLiteRateLimiter, user+endpoint).
    """
```

### 4.5 Ångra-tidslinje — `web/static/timeline-drawer.js` + `web/api/timeline.py`

**Board-vy-tillägg:**

En knapp "Tidslinje ⏱" i board-topbaren (bredvid sprint-filter) öppnar en drawer.
Drawern är ett `<aside>`-element som glider in från höger (CSS transition).

```
┌ Tidslinje ──────────────────────────── [✕] ┐
│ ● 14:02  board_move  BL-14: inbox→triaged  │
│          acme                   [Ångra]    │
│ ● 13:50  memory_write  standup/2026-07-01  │
│          acme                   [Ångra]    │
│ ✗ 13:44  email_send  kund@example.com      │
│          acme  [Kan ej ångras: mejl skickat ℹ] │
│ ● 13:30  calendar_create  Möte med X       │
│          acme                   [Ångra]    │
└────────────────────────────────────────────┘
```

- Grön punkt = reversibel. Grå kryss = irreversibel.
- "Ångra"-klick → `confirm("Ångra: board_move BL-14? Kortet återgår till 'inbox'.")`
  → `POST /app/api/timeline/{action_id}/undo` → ta bort rad optimistiskt → toast
  "Ångrat. Sidan uppdateras…" → reload board-data.
- Irreversibla rader: info-ikon med tooltip från `action.irreversible_reason`
  (t.ex. "E-post kan inte återkallas — använd utkorgen för att stoppa innan sändning").
- Om ångra misslyckas (409 = redan ångrat; 422 = åtgärd ej möjlig): toast med
  felmeddelande, lägg tillbaka raden.

**`GET /app/api/timeline`:**

```python
async def api_timeline(request: Request) -> JSONResponse:
    """
    GET /app/api/timeline?project=<X>&limit=<50>
    Anropar ActionsStore.list(projects, limit) — projects från
    acl.visible_projects(user). (INTE ActionLog.query — den finns inte.)
    ACL: enforce reader (läsning av tidslinje).
    Returnerar {actions: [{id, tool, project, summary, ts, reversible,
                           irreversible_reason, undone_at}]}.
    """
```

**`POST /app/api/timeline/{action_id}/undo`:**

```python
async def api_timeline_undo(request: Request) -> JSONResponse:
    """
    Anropar timeline.undo.undo(store, acl, user, action_id). (INTE
    ActionLog.undo — använd den fristående undo()-funktionen.)
    ACL: enforce samma roll som ursprungliga åtgärden (owner för board_move,
         collaborator för memory_write etc.) — hämtas ur action-raden.
    409 om redan ångrat.
    422 om irreversibel.
    Audit-loggar 'action_undo'.
    Returnerar {ok: true, summary: "Ångrat: board_move BL-14"}.
    """
```

---

## 5. Byggordning

Fas D byggs *efter* att Fas A, B och C är kompletta och funktion #1 + #2 är byggda.
Delkomponenterna inom Fas D är tillräckligt oberoende för att kunna byggas parallellt
av olika implementatörer, men ordningen nedan minimerar blockerare.

1. **MFA-infrastruktur** (`web/api/mfa.py`, `_require_mfa()`, `admin-mfa.html`,
   `mfa.js`). Är blockerare för admin-skrivoperationer. Kräver `pyotp` i
   `pyproject.toml`. Testa: `pytest tests/test_mfa.py`.

2. **`AclWriter`** (`web/acl_writer.py`) — atomisk skrivning av `acl.yaml`, backup-rotation.
   Helt oberoende. Testa: `pytest tests/test_acl_writer.py` (temp-kataloger, inga nätverk).

3. **Admin-skrivoperationer** (`web/api/admin_write.py`, uppdatera `admin.html`
   + `admin-users.js`). Beroende av steg 1+2. Testa: `pytest tests/test_admin_write.py`.

4. **Global sökning** (`web/api/search.py`, `search-bar.js`, topbar-tillägg i `shell.html`).
   Beroende av funktion #2 (search_all). Testa: `pytest tests/test_api_search.py`;
   manuell test i browser med riktig fråga.

5. **Brief-inställningar** (`web/api/brief.py`, `settings-brief.html` flik, `brief-settings.js`).
   Beroende av funktion #1 (NotifyStore). Testa: `pytest tests/test_api_brief.py`.

6. **Ångra-tidslinje** (`web/api/timeline.py`, `timeline-drawer.js`, board.html-tillägg).
   Beroende av funktion #5 (`ActionsStore` + `timeline.undo.undo`). Testa: `pytest tests/test_api_timeline.py`.

7. **CI + docs** — kör `python -m pytest -q` från `gateway/` (allt grönt);
   `python3 scripts/check-docs-index.py`; uppdatera `docs/INDEX.md` och
   `docs/DEVELOPMENT-PROPOSALS.md` (MEX-025 → levererad).

---

## 6. Utvecklingsinstruktioner / Kodkontrakt

Konventioner: se `FEATURE-WEB-UI-FOUNDATION.md` §6 och övriga funktion-docs
(SPDX-huvud, SQLite WAL-mönster, injicerbara beroenden, inga hemligheter i loggar,
audit via `safety/audit.py`).

### Filstruktur (Fas D-tillägg)

```
gateway/src/memaix_gateway/
└── web/
    ├── acl_writer.py              (nytt)
    ├── api/
    │   ├── admin_write.py         (nytt)
    │   ├── brief.py               (nytt)
    │   ├── mfa.py                 (nytt)
    │   ├── search.py              (nytt)
    │   └── timeline.py            (nytt)
    ├── pages/
    │   ├── admin-mfa.html         (nytt)
    │   ├── admin-mfa-setup.html   (nytt)
    │   └── settings-brief.html    (ny flik, inkluderas i settings.html)
    └── static/
        ├── admin-users.js         (uppdateras, lägg till skrivfunktioner)
        ├── brief-settings.js      (nytt)
        ├── mfa.js                 (nytt)
        ├── search-bar.js          (nytt)
        └── timeline-drawer.js     (nytt)
```

### Nya routes (läggs till i `web/routes.py`)

```python
Route("/app/api/search",                    api_search,             methods=["GET"]),
Route("/app/api/brief",                     api_brief_get,          methods=["GET"]),
Route("/app/api/brief",                     api_brief_post,         methods=["POST"]),
Route("/app/api/admin/users/{uid}/disable", api_admin_user_disable, methods=["POST"]),
Route("/app/api/admin/users/{uid}/grants",  api_admin_user_grants,  methods=["PATCH"]),
Route("/app/api/admin/projects/{project}",  api_admin_project,      methods=["PATCH"]),
Route("/app/api/timeline",                  api_timeline,           methods=["GET"]),
Route("/app/api/timeline/{id}/undo",        api_timeline_undo,      methods=["POST"]),
Route("/app/admin/mfa",                     admin_mfa,              methods=["GET"]),
Route("/app/admin/mfa/setup",               admin_mfa_setup,        methods=["GET"]),
Route("/app/admin/mfa/verify",              api_mfa_verify,         methods=["POST"]),
Route("/app/admin/mfa/setup",               api_mfa_setup_save,     methods=["POST"]),
```

### `_require_mfa` — kontrakt

```python
def _require_mfa(request: Request, user: str) -> bool:
    """
    Kontrollera memaix_mfa-cookie: parse {user}:{ts}:{sig}, verifiera HMAC,
    kontrollera att ts > now - 8*3600. Returnera True om OK.
    Kallas i varje /app/admin-handler. Vid False: redirect /app/admin/mfa.
    """
```

### `AclWriter` — testkontrakt

```
tests/test_acl_writer.py:
- set_user_disabled skriver disabled: true/false till rätt användare
- set_grants uppdaterar grants-dict utan att röra andra användare
- _write_atomic är atomisk: avbrott mitt i ger inte korrupt fil
- backup-rotation: max 3 bak-filer, äldst tas bort vid overflow
- två parallella writes: sista vinner (last-write-wins, dokumenterat)
```

### Säkerhet

- Alla `/app/api/admin/*` och `/app/admin/*`: `_require_user` + `_require_mfa` +
  `acl.is_admin(user)` — alla tre kraven, i den ordningen. Saknas något: 401/403.
- TOTP: rate-limit (5 försök / 10 min per användare) via `SQLiteRateLimiter`.
  Rate-limit-DB: `MEMAIX_RATELIMIT_DB` (återanvänd befintlig om det finns).
- `AclWriter` skriver aldrig lösenord, tokens eller TOTP-hemligheter till `acl.yaml`
  i klartext. Använd alltid `*_ref`-konventionen.
- Sökresultat: `snippet` trunkeras server-side (< 200 tecken) — returnera aldrig
  hela dokumentet via sök-API.
- Undo: den befintliga `timeline.undo.undo()` är **roll**-scopad, inte
  ägar-scopad — den enforce:ar samma roll som originalåtgärden krävde
  (`_NEED_FOR_TOOL`), oavsett vem som utförde den. Webb-lagret ska anropa den som
  den är; duplicera inte behörighetslogik i web-routen. Vill man dessutom hindra
  att en användare ångrar en *annan* användares åtgärd (utom admin) är det en
  ändring i `timeline/undo.undo` (så MCP och webb behåller samma regel) — inte en
  web-only-kontroll. Specificera i så fall den ändringen separat innan den byggs.
- Alla admin-skrivoperationer audit-loggas med `tool="admin_{operation}"`,
  `detail=f"{field}: {old} → {new}"` (aldrig hemligheter i detail).

### Beroenden att lägga till i `pyproject.toml`

```toml
[project.dependencies]
# … befintliga …
pyotp = ">=2.9"
qrcode = {version = ">=7.4", extras = ["pil"]}   # för TOTP-setup QR-SVG

[project.optional-dependencies]
search = ["sentence-transformers", "numpy"]       # redan från funktion #2
```

### Acceptanskriterier

- [ ] `⌘K` i topbar öppnar sökfält; fråga returnerar rankade träffar med projekt,
      typ och snippet. Resultat är ACL-filtrerade (inga projekt utanför `visible_projects`).
- [ ] Sökning degraderar graciöst: om funktion #2 ej byggts visas inget sökfält
      (`me.search_enabled = false`).
- [ ] Brief-fliken under Inställningar sparar konfiguration via `NotifyStore.set_prefs()`;
      `next_run` visas korrekt i tidszonen som valts.
- [ ] `/app/admin` redirectar till `/app/admin/mfa` om MFA-cookie saknas eller
      är äldre än 8 h.
- [ ] TOTP-verifiering accepterar rätt kod; felar (401) på fel kod; rate-limitar
      efter 5 försök.
- [ ] Kill-switch inaktiverar användare: efter `disable`, returnerar `_require_user`
      null för den användaren (401 på alla autentiserade routes).
- [ ] Grants-matrisändring sparas till `acl.yaml`; ändringen syns vid nästa
      `GET /app/api/me` för berörd användare.
- [ ] `AclWriter` gör atomic write; backup-rotation bevarar 3 versioner; trasig
      write korrupterar aldrig produktionsfilen.
- [ ] Tidslinje-drawer öppnas i board; reversibla åtgärder har Ångra-knapp;
      irreversibla är grå med förklaring.
- [ ] Undo-flödet anropar `timeline.undo.undo()` och audit-loggar `action_undo`; 409
      vid dubbelångring.
- [ ] Kill-switch: `disabled: true` gör att `acl.enforce` nekar användaren allt
      (verifieras redan av `tests/test_acl_admin.py`); AclWriter anropar
      `server.reload_acl()` efter skrivning och spärrar självavaktivering / sista admin.
- [ ] MFA-enrollment: väntande hemlighet bärs i signerad cookie mellan setup-GET och
      verify-POST; hemligheten sparas som `file:`-ref (live utan omstart), inte `env:`.
- [ ] Inga hemligheter, TOTP-koder eller dokumentinnehåll i audit-loggar eller
      access-loggar.
- [ ] `python -m pytest -q` från `gateway/` är grönt; inga regressions i Fas A–C.

---

## Framtida arbete

- **Web Push / PWA-kanal** för brief (service worker + VAPID) — nämns i
  `FEATURE-PROACTIVE-BRIEF.md §Framtida arbete`; kräver manifest.json i `web/static/`.
- **Fler tidslinje-slots** — veckorapport, sprint-sammanfattning via `pm_status_report`.
- **Admin: vault-statistik** — antal noter, backlog-items, senaste git-commit per
  projekt; kräver ny `GET /app/api/admin/vaults` som mappar `vault/`-sökvägar.
- **Sökning med cross-encoder-omrankning** (funktion #2 framtida arbete) — UI
  behöver ingen ändring om backend returnerar bättre rang.
- **Klient-schemalagd brief-hämtning** — om klienten stödjer det (ChatGPT tasks);
  ingen UI-ändring, men `brief_configure` bör exponera `subscription_url` för
  klienten att schemalägga mot.
- **Audit-export** — CSV-nedladdning från admin audit-fliken;
  `GET /app/api/admin/audit/export?format=csv`.
