# Funktion #1 — Proaktiv brief och notiser

SPDX-License-Identifier: AGPL-3.0-or-later

Designdok + byggspec för proaktiv leverans: en schemalagd sammanställning
(dagsbrief) och händelsenotiser som når användaren i den kanal hen valt, samt
en "hämta-vid-öppning"-brief via MCP-connectorn.

Doket är skrivet för att kunna byggas stegvis av en implementatör (t.ex. en
kodande AI som Sonnet) utan ytterligare kontext. Följ [Byggordning](#byggordning)
och [Utvecklingsinstruktioner](#utvecklingsinstruktioner) i tur och ordning.

---

## 1. Kan vi pusha via connectorn?

**Nej — inte som äkta push.** MCP är pull/request-response inom en aktiv session.
Servern kan skicka notifieringar i protokollet (logg, progress, `resources/updated`,
sampling), men bara medan en session är öppen, och de hostade klienterna
(claude.ai, ChatGPT) väcker inte modellen spontant och visar inte server-initierade
notiser som användar-push utanför en konversation. Det finns inget API för att
lägga ett meddelande i användarens chatt eller skicka en telefonnotis.

Konsekvens för designen:

- **Proaktiv leverans sker via sidokanaler** — e-post (finns redan via SMTP),
  chatt-webhooks (Slack/Telegram/Discord). ntfy togs bort som kanal (Jimmy
  2026-11-22). Se
  [Leveranskanaler](#4-leveranskanaler).
- **Connectorn får en kompletterande "hämta-vid-öppning"-roll**: en MCP-prompt
  `daily_brief` och verktyget `brief_preview` levererar briefen när användaren
  nästa gång öppnar Memaix i sin klient. Om klienten stödjer schemalagda uppgifter
  (t.ex. ChatGPT "tasks") kan användaren själv schemalägga ett anrop — men servern
  kan inte initiera det, så vi förlitar oss aldrig på det.

Designen är alltså: **servern genererar och pushar via sidokanal**, och connectorn
erbjuder samma innehåll on-demand.

---

## 2. Översikt

```
                    ┌──────────────────────────────────────────┐
                    │  Scheduler (asyncio-loop i gatewayen)      │
                    │  vaknar 1 ggr/min, hämtar due briefer      │
                    │  ur brief_schedule, claim:ar atomiskt      │
                    └───────────────┬──────────────────────────┘
                                    │  för varje due (user, slot)
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │  BriefBuilder.build(acl, user, cfg)        │
                    │  → kalender idag, obesvarad mail, backlog/ │
                    │    sprint-ändringar, öppna RAID            │
                    └───────────────┬──────────────────────────┘
                                    │  BriefContent (markdown + text)
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │  Delivery: NotificationChannel-adaptrar    │
                    │  email · webhook(slack/telegram/discord)   │
                    │  idempotens-vakt (user+date+slot)          │
                    └──────────────────────────────────────────┘

  Connector (pull):  MCP-prompt daily_brief · verktyg brief_preview / brief_send_now
  Konfiguration:     verktyg brief_configure / brief_status  → notify_prefs (SQLite)
```

Principer:

- **Opt-in.** Ingen brief skickas förrän användaren kört `brief_configure`.
- **Per användare.** Schema, tidszon, tysta timmar och kanaler är per `memaix_user`.
- **Återanvänd befintlig kod.** Kalender/mail/backlog/pm-verktygen och deras
  ACL-enforce anropas som vanligt; scheduler-körningen agerar *som* en specifik
  användare (samma `acl`, `user`-argument).
- **Idempotent.** En brief för en given (user, datum, slot) skickas högst en gång
  även om loopen kör flera gånger eller startar om (gap #13).
- **Granskningsbar.** Varje sänd brief loggas i audit-loggen.

---

## 3. Datamodell

Ny SQLite-databas, sökväg via env `MEMAIX_NOTIFY_DB` (default
`/tmp/memaix-notify.db`). Två tabeller.

```sql
-- Per-användares briefinställningar.
CREATE TABLE IF NOT EXISTS notify_prefs (
    memaix_user  TEXT PRIMARY KEY,
    enabled      INTEGER NOT NULL DEFAULT 0,
    timezone     TEXT NOT NULL DEFAULT 'UTC',   -- IANA, t.ex. 'Europe/Stockholm'
    brief_time   TEXT NOT NULL DEFAULT '07:00', -- HH:MM i användarens tz
    quiet_start  TEXT,                           -- HH:MM eller NULL
    quiet_end    TEXT,                           -- HH:MM eller NULL
    channels     TEXT NOT NULL DEFAULT '[]',     -- JSON: [{type, ...}] se nedan
    projects     TEXT NOT NULL DEFAULT '[]',     -- JSON: projekt att ta med ([] = alla synliga)
    updated_at   TEXT NOT NULL
);

-- Schema-rader; en per (user, slot). "daily" är enda sloten i v1.
CREATE TABLE IF NOT EXISTS brief_schedule (
    memaix_user  TEXT NOT NULL,
    slot         TEXT NOT NULL,       -- 'daily'
    next_run     INTEGER NOT NULL,    -- unix epoch (UTC) för nästa körning
    last_run     INTEGER,             -- unix epoch för senaste lyckade körning
    PRIMARY KEY (memaix_user, slot)
);

-- Idempotens: en rad per faktiskt sänd brief.
CREATE TABLE IF NOT EXISTS brief_sent (
    idem_key     TEXT PRIMARY KEY,    -- f'{user}:{slot}:{YYYY-MM-DD}'
    sent_at      TEXT NOT NULL
);
```

`channels`-JSON, exempel:

```json
[
  {"type": "email", "to": "jimmy@example.com"},
  {"type": "webhook", "url_ref": "env:BRIEF_SLACK_WEBHOOK", "format": "slack"}
]
```

`url_ref` följer samma `*_ref`-konvention som resten av configen (se
`docs/SECRETS.md` / `config.secret`). Hemligheter lagras **aldrig** i klartext i
tabellen — bara referensen.

---

## 4. Leveranskanaler

Abstraktion (`notify/channels.py`):

```python
class NotificationChannel(Protocol):
    def send(self, subject: str, markdown: str, text: str) -> None: ...
```

Adaptrar i v1:

| type | Krav | Anteckning |
|------|------|-----------|
| `email` | projektets SMTP-config (finns) | Återanvänd `email_send`-vägen; skicka till `channels[].to`. |
| `webhook` | `url_ref` + `format` (`slack`\|`discord`\|`raw`) | POST JSON. `slack` → `{"text": ...}`. Timeout 10 s. |

Varje adapter tar emot både `markdown` och en förrenderad `text`-variant så
kanaler utan markdown-stöd får läsbar text. Fel i en kanal får **inte** stoppa
övriga — logga och fortsätt (per-kanal try/except). (ntfy togs bort som kanal,
Jimmy 2026-11-22.)

Web Push/PWA är medvetet utanför v1 (kräver service worker + VAPID); se
[Framtida arbete](#framtida-arbete).

---

## 5. Brief-innehåll

`BriefBuilder.build(acl, user, cfg, prefs) -> BriefContent` sätter samman, per
projekt användaren valt (eller alla synliga):

1. **Kalender idag** — händelser i [00:00, 24:00) i användarens tz. Återanvänd
   kalenderupplösningen (se not om `_resolve_calendar_dav` nedan). Hoppa tyst
   över projekt utan kalender.
2. **Mail som väntar** — obesvarade/olästa i INBOX (via `email_list`), topp N
   (default 5). Bara projekt med mailbox.
3. **Backlog/sprint-ändringar sedan senaste brief** — diff mot `last_run` via
   audit-loggen (tool i {`backlog_set_status`,`backlog_score`,`board_move`,
   `pm_plan_sprint`}) + aktuell sprint-burndown via `pm_sprint_status` om aktiv
   sprint finns.
4. **Öppna RAID** — antal öppna via `pm_raid_list`.

`BriefContent` = `{subject, markdown, text}`. Rendera på svenska/lokaliserat via
`i18n` (nya nycklar, se instruktion 7). Om allt är tomt: skicka en kort
"inget nytt idag"-brief (konfigurerbart; default skicka).

> **Not — kalender/mail utanför request-kontext.** Scheduler-körningen har ingen
> MCP-session, så `server._user()` funkar inte. `BriefBuilder` måste få `user`
> explicit och lösa kalender/token på samma sätt som `server._resolve_calendar_dav`.
> Bryt därför ut token-/adapter-upplösningen (instruktion 3) så den kan anropas
> både från request-verktygen och från briefen.

---

## 6. MCP-yta (connector)

Nya verktyg i `server.py` (registreras med `@mcp.tool()`), tunna wrappers precis
som övriga — de går genom en variant av `_tool_call` men mot projektet `"shared"`
för rate-limit/audit (brief är användarglobal, inte projektbunden):

| Verktyg | Signatur | Beskrivning |
|---------|----------|-------------|
| `brief_configure` | `(enabled: bool, brief_time: str="07:00", timezone: str="UTC", channels: list\|None=None, quiet_hours: dict\|None=None, projects: list\|None=None)` | Sätt/uppdatera inställningar. Beräknar och skriver `brief_schedule.next_run`. |
| `brief_status` | `()` | Returnera aktuella inställningar + `next_run` (ISO) + `last_run`. |
| `brief_preview` | `()` | Bygg briefen **nu** och returnera `{subject, markdown, text}` utan att skicka. Detta är hämta-vid-öppning-vägen. |
| `brief_send_now` | `()` | Bygg och leverera direkt via konfigurerade kanaler (ignorerar tysta timmar; audit-loggas). |

Plus en MCP-prompt:

```python
@mcp.prompt()
def daily_brief() -> str:
    """Leverera dagens brief för den anropande användaren (hämta-vid-öppning)."""
    # returnerar brief_preview()-innehållets markdown som prompttext
```

---

## 7. Schemaläggare

En asyncio-loop startad i `build_http_app()` (endast HTTP-läge). Design som
undviker dubbelkörning i multi-worker utan extra beroenden:

- Vakna var 60:e sekund.
- `SELECT ... FROM brief_schedule WHERE next_run <= now`.
- För varje rad: **claim atomiskt** med
  `UPDATE brief_schedule SET next_run = <nästa> WHERE memaix_user=? AND slot=? AND next_run=<gamla>`.
  Om `rowcount == 0` tog en annan worker den — hoppa. (Compare-and-set; samma
  princip som SQLiteRateLimiter.)
- Beräkna nästa `next_run` från `brief_time` + tz (nästa dag).
- Kontrollera idempotens (`brief_sent`), respektera tysta timmar, bygg och leverera.
- Uppdatera `last_run`.

Ingen APScheduler-dependency i v1. Loopen ska vara robust: en exception för en
användare får inte döda loopen (per-user try/except + logg).

> Tid: `Date.now()`/`time.time()` används i produktionskod som vanligt. I tester
> injiceras "nu" som parameter (se testinstruktion) — bygg `_tick(now: float)`
> som ren funktion så den kan testas deterministiskt.

---

## 8. Konfiguration

`config/memaix.example.yaml` — ny sektion (allt valfritt, briefen är opt-in per
användare ändå):

```yaml
memaix:
  brief:
    enabled: true                 # global huvudbrytare (default true)
    default_timezone: "Europe/Stockholm"
    send_when_empty: true         # skicka även när inget nytt
    max_mail: 5
```

`.env.example` — kanalhemligheter (referenser), t.ex.:

```
# BRIEF_SLACK_WEBHOOK=            # webhook-URL om channel.format=slack
```

Nya env:
- `MEMAIX_NOTIFY_DB` — sökväg till notify-DB (default `/tmp/memaix-notify.db`).

---

## 9. Säkerhet & integritet

- Briefen innehåller känslig data → kanaler måste vara betrodda. Dokumentera det;
  default är e-post till användarens egen adress.
- Hemligheter (webhook-URL, tokens) lagras som `*_ref` och resolvas med
  `config.secret` — aldrig i klartext i `notify_prefs`.
- Opt-in och per användare; `enabled=false` stänger av direkt.
- Respektera tysta timmar för schemalagd brief (men `brief_send_now` är explicit
  och får gå igenom).
- Audit-logga varje sändning (`tool="brief_send"`, `ok`, kanalantal) — logga
  aldrig briefens innehåll eller hemligheter.
- Mottagar-/URL-allowlist är inte v1 men bör övervägas (kopplar till
  `THREAT-MODEL.md` exfiltrering).

---

## Byggordning

Bygg och testa i denna ordning — varje steg är självständigt grönt:

1. **Store** (`notify/store.py`) — SQLite-tabeller + CRUD. *Testbart isolerat.*
2. **Kanaler** (`notify/channels.py`) — adaptrar + factory, injicerbar HTTP/SMTP.
3. **Kalenderupplösning utbruten** — refaktorera `server._resolve_calendar_dav`
   så kärnan kan anropas utan request-kontext.
4. **BriefBuilder** (`notify/brief.py`) — sammanställ innehåll (injicera verktyg).
5. **Leverans** (`notify/deliver.py`) — bygg → idempotensvakt → kanaler → audit.
6. **Scheduler** (`notify/scheduler.py`) — `_tick(now)` + asyncio-loop, claim-logik.
7. **MCP-yta** (`server.py`) — verktyg + prompt + starta loopen i `build_http_app`.
8. **i18n + config + docs** — nycklar, exempelconfig, INDEX.
9. **CI** — testerna körs redan (funktion #3); säkerställ att de nya passerar.

---

## Utvecklingsinstruktioner

Konventioner att följa (matcha befintlig kod):
- Filhuvud: `# SPDX-License-Identifier: AGPL-3.0-or-later` + kort docstring.
- SQLite: `threading.Lock` + `PRAGMA journal_mode=WAL`, ny connection per operation
  (se `backends/token_store.py`, `safety/rate_limit.py:SQLiteRateLimiter`).
- Injicerbara beroenden för testbarhet (som `_imap`/`_dav`/`_smtp` i verktygen).
- Inga hemligheter i loggar. Audit via `safety/audit.py`.
- Kör `python -m pytest -q` från `gateway/` — allt måste vara grönt.

### Steg 1 — `notify/store.py`
Skapa paketet `gateway/src/memaix_gateway/notify/__init__.py` (tomt) och
`notify/store.py` med klassen `NotifyStore`:

```python
class NotifyStore:
    def __init__(self, db_path: Path) -> None: ...
    @classmethod
    def for_path(cls, db_path: Path) -> "NotifyStore": ...
    # prefs
    def get_prefs(self, user: str) -> dict | None
    def set_prefs(self, user: str, **fields) -> dict     # upsert, returnerar prefs
    # schedule
    def upsert_schedule(self, user: str, slot: str, next_run: int) -> None
    def due(self, now: int) -> list[dict]                # rader med next_run <= now
    def claim(self, user: str, slot: str, old_next: int, new_next: int) -> bool
    def mark_run(self, user: str, slot: str, last_run: int) -> None
    # idempotens
    def already_sent(self, idem_key: str) -> bool
    def record_sent(self, idem_key: str, sent_at: str) -> None
```

`claim` gör `UPDATE ... WHERE ... AND next_run=?` och returnerar `cur.rowcount == 1`.
**Test** (`tests/test_notify_store.py`): upsert+get prefs; `due` filtrerar på tid;
`claim` returnerar True första gången och False för fel `old_next`; `already_sent`
före/efter `record_sent`.

### Steg 2 — `notify/channels.py`
Definiera `NotificationChannel` (Protocol) och adaptrarna `EmailChannel`,
`WebhookChannel`. Varje `send(subject, markdown, text)`. (ntfy fanns som
tredje adapter men togs bort som kanal, Jimmy 2026-11-22.)
`build_channels(specs: list[dict], *, _http=None, _smtp=None, acl=None, project=None)`
bygger adaptrar från JSON-specarna; resolva `url_ref` via `config.secret`.
HTTP-anrop via injicerbar klient (default `requests.post`, timeout 10).
**Test** (`tests/test_notify_channels.py`): webhook `slack`-format POST:ar
`{"text": ...}` till rätt URL (mocka `_http`); en trasig kanal kastar men
fångas av leveranslagret (testas i steg 5). Ingen riktig nätverkstrafik.

### Steg 3 — Bryt ut kalenderupplösning
I `server.py`, extrahera kärnan i `_resolve_calendar_dav(project, user)` till en
funktion som tar `(acl, cfg, store, user, project)` (ingen `_user()`-slagning).
Låt det gamla anropet delegera dit. Detta så `BriefBuilder` kan lösa kalender.
**Test:** befintliga `test_calendar.py` ska fortsatt passera; lägg ett test som
kallar den utbrutna funktionen direkt med injicerad tokenstore.

### Steg 4 — `notify/brief.py`
`BriefBuilder` med `build(acl, user, cfg, prefs, *, now, tools=None) -> dict`.
`tools` är en liten struct/dict som injicerar `calendar_list`, `email_list`,
`backlog_list`, `pm_sprint_status`, `pm_raid_list`, `audit` — default de riktiga.
Returnera `{"subject","markdown","text"}`. Rendera via `i18n`-nycklar.
**Test** (`tests/test_brief.py`): injicera fejkade verktyg som returnerar kända
värden; verifiera att markdown innehåller kalenderpost, mailrubrik, RAID-antal;
tom-fall ger "inget nytt"-brief.

### Steg 5 — `notify/deliver.py`
`deliver(store, acl, cfg, user, prefs, *, now, force=False) -> dict`:
bygg idem_key `f"{user}:daily:{date}"`; om `already_sent` och inte `force` → return
`{"skipped": "duplicate"}`; bygg brief; `build_channels`; anropa varje `send` i
try/except (logga fel per kanal); `record_sent`; audit-logga `brief_send`.
Respektera tysta timmar om inte `force`.
**Test** (`tests/test_deliver.py`): två anrop → andra hoppar (idempotens); en
trasig kanal stoppar inte de andra; `force=True` kringgår idempotens och tysta
timmar; audit-rad skapas.

### Steg 6 — `notify/scheduler.py`
Ren funktion `run_due(store, deliver_fn, now) -> int` (antal körda): hämta `due`,
`claim` var och en (beräkna nästa `next_run` via `next_brief_epoch(prefs, now)`),
kör `deliver_fn`, `mark_run`. Plus `next_brief_epoch(prefs, now)` som räknar ut
nästa `brief_time` i användarens tz (använd `zoneinfo`). Plus en asyncio-loop
`async def scheduler_loop(store, deliver_fn, interval=60)` som anropar `run_due`
och sover.
**Test** (`tests/test_scheduler.py`): `next_brief_epoch` ger rätt UTC-epoch för
given tz/tid (deterministiskt, mata in `now`); `run_due` claim:ar och kör bara
due-rader; dubbel `run_due` med samma `now` kör inte om samma slot (claim släpper
inte igenom).

### Steg 7 — MCP-yta i `server.py`
Lägg `_get_notify_store()` (lazy, som `_get_token_store`). Lägg verktygen
`brief_configure/status/preview/send_now` och prompten `daily_brief`. `configure`
validerar tid (`HH:MM`), tz (`zoneinfo.available_timezones()` eller try/except
`ZoneInfo`), och kanal-specar (kända `type`). Starta `scheduler_loop` som en
`asyncio.create_task` i `build_http_app()` (bara om `brief.enabled`).
**Test** (`tests/test_server.py`): `brief_configure` → `brief_status` speglar
inställningarna och sätter `next_run`; `brief_preview` returnerar innehåll med
injicerad store/tools; ogiltig tz/tid ger fel.

### Steg 8 — i18n, config, docs
Lägg brief-nycklar i alla `i18n/locales/*.json` (en, sv, fr, de, es — engelska
som fallback räcker för start men lägg åtminstone en/sv). Uppdatera
`config/memaix.example.yaml` och `.env.example` enligt [Konfiguration](#8-konfiguration).
Lägg denna fil i `docs/INDEX.md` (redan gjort) och uppdatera statusrutan i
`docs/DEVELOPMENT-PROPOSALS.md` (#1 → påbörjad/levererad).

### Steg 9 — Kör allt
`cd gateway && python -m pytest -q` (allt grönt) och
`python3 scripts/check-docs-index.py`. CI kör redan testsviten (funktion #3).

### Acceptanskriterier
- [ ] `brief_configure(enabled=True, brief_time="07:00", timezone="Europe/Stockholm", channels=[{"type":"email","to":"x@y"}])` gör att `brief_status` visar rätt och `next_run` pekar på nästa 07:00 svensk tid.
- [ ] Schemaläggaren levererar vid rätt tidpunkt och exakt en gång per dag (idempotens verifierad).
- [ ] `brief_send_now` levererar direkt oavsett tysta timmar.
- [ ] `brief_preview` och prompten `daily_brief` returnerar samma innehåll utan att skicka.
- [ ] En trasig kanal stoppar inte övriga; fel loggas.
- [ ] Inga hemligheter eller briefinnehåll i loggar; varje sändning i audit.
- [ ] Multi-worker: två scheduler-loopar dubbelskickar inte (claim-testet bevisar det).
- [ ] Hela testsviten grön; docs-index grön.

---

## Framtida arbete
- Web Push/PWA-kanal (service worker + VAPID) för äkta mobil-push.
- Fler slots (kvällssammanfattning, veckorapport — återanvänd `pm_status_report`).
- Mottagar-/URL-allowlist för kanaler (exfiltreringsskydd).
- Klient-schemalagd hämtning via connector där klienten stödjer det (ChatGPT tasks).
- APScheduler-jobstore om schemabehoven växer bortom en enkel daglig slot.
