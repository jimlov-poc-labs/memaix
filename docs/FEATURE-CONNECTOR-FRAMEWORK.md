# Funktion #7 — Connector-ramverk (pluggbara backend-adaptrar)

SPDX-License-Identifier: AGPL-3.0-or-later

Designdok + byggspec för ett **pluggbart connector-ramverk**: ett enhetligt
adapter-gränssnitt så att nya integrationer (Microsoft Graph, Gmail, Nextcloud,
Slack, Jira, mötestranskript …) läggs till som *plugins* istället för
engångslösningar. Realiserar och generaliserar adaptermodellen i
[BACKENDS.md](BACKENDS.md).

Byggs stegvis enligt [Byggordning](#byggordning) och
[Utvecklingsinstruktioner](#utvecklingsinstruktioner). Detta är grunden i fas 4
(se [ROADMAP.md](ROADMAP.md)) och en förutsättning för Nextcloud-fördjupningen
([FEATURE-NEXTCLOUD-BACKEND.md](FEATURE-NEXTCLOUD-BACKEND.md)).

---

## 1. Problemet

Idag är backend-valet hårdkopplat i verktygen: `email.py` skapar `imap_tools.MailBox`
direkt, `calendar.py` har `_RealDavAdapter`/`_PerUserGoogleAdapter` inline, `files.py`
kan bara lokal vault. Varje ny tjänst kräver att man petar i verktygsfilerna. Med ett
ramverk blir en integration en självständig modul som *registrerar* sig — verktygen
rör man aldrig igen.

## 2. Nyckelbeslut

1. **Kapabilitets-gränssnitt, inte tjänst-gränssnitt.** Definiera små protokoll per
   *kapabilitet* — `MailBackend`, `CalendarBackend`, `FilesBackend`, `ContactsBackend`,
   `ChatBackend`, `IssueBackend` — som verktygen pratar med. En tjänst (Google,
   Nextcloud …) implementerar de kapabiliteter den stöder.
2. **Registret väljer adapter per projekt-resurs.** `acl.yaml`/`memaix.yaml` anger
   `type` per resurs (som redan i BACKENDS.md); en `ConnectorRegistry` mappar
   `type → factory` och bygger rätt adapter, med credentials via `config.secret`
   eller per-user token-store.
3. **Befintliga verktyg blir tunna.** `email_*`/`calendar_*`/`files_*` slutar
   instansiera backends själva och kallar `registry.get(project, "mail", user)`.
   Nuvarande IMAP/CalDAV/WebDAV/Google flyttas in som adaptrar bakom samma protokoll
   — beteendet är oförändrat (befintliga tester ska passera).
4. **Nya kapabiliteter är opt-in.** `ChatBackend`/`IssueBackend` m.fl. läggs till utan
   att röra kärnan; nya MCP-verktyg (t.ex. `chat_post`) läggs bredvid.
5. **Per-user och delat samexisterar.** En adapter deklarerar sin auth-modell
   (`shared` via `*_ref` eller `per_user` via token-store); registret väljer rätt
   token utifrån den inloggade användaren (BACKENDS.md §Auth).

## 3. Översikt

```
  email_* / calendar_* / files_* / (nya) chat_* / issue_*      (MCP-verktyg)
        │  registry.get(project, capability, user)
        ▼
  ConnectorRegistry   type → factory   (+ auth: shared | per_user)
        │
        ├─ mail:     imap · google · microsoft
        ├─ calendar: caldav · google · microsoft
        ├─ files:    webdav · local · google_drive · onedrive
        ├─ contacts: carddav · google · microsoft
        ├─ chat:     slack · nextcloud_talk · telegram
        └─ issues:   jira · linear · github
        │  varje adapter: credentials via config.secret / token_store
        ▼
  Adapter (implementerar kapabilitets-protokollet)
```

## 4. Kapabilitets-protokoll

`connectors/base.py` — Protocol per kapabilitet (duck-typat, som dagens `_dav`/`_imap`):

```python
class MailBackend(Protocol):
    def list(self, folder: str, limit: int) -> list[dict]: ...
    def read(self, uid: str) -> dict: ...
    def search(self, query: str, limit: int) -> list[dict]: ...
    def append_draft(self, msg_bytes: bytes) -> None: ...
    def send(self, msg) -> None: ...

class CalendarBackend(Protocol):    # motsvarar dagens _dav-duck-typ
    def list_events(self, start, end) -> list[dict]: ...
    def create_event(self, ...) -> dict: ...
    def update_event(self, id, **fields) -> dict: ...
    def delete_event(self, id) -> None: ...

class FilesBackend(Protocol):   list/read/write/search  (motsvarar files.py)
class ContactsBackend(Protocol): search(query) -> list[dict]; get(id) -> dict
class ChatBackend(Protocol):     post(channel, text); list_messages(channel, since)
class IssueBackend(Protocol):    list(query); create(item); update(id, **fields)
```

Gränssnitten speglar **dagens** verktyg exakt där de finns (mail/calendar/files), så
att flytten är en refaktor utan beteendeändring.

## 5. Register & factory

`connectors/registry.py`:

```python
@dataclass(frozen=True)
class ConnectorSpec:
    type: str                    # 'imap' | 'google' | 'nextcloud_talk' | ...
    capability: str              # 'mail' | 'calendar' | 'files' | 'contacts' | 'chat' | 'issues'
    auth: str                    # 'shared' | 'per_user'
    factory: Callable            # (resource_cfg, *, secret, token, user) -> adapter

class ConnectorRegistry:
    def register(self, spec: ConnectorSpec) -> None
    def get(self, acl, cfg, token_store, project, capability, user):
        """Slå upp projektets resurs-cfg, välj type→spec, lös credentials, bygg adapter.
        auth='shared' → config.secret(resource['*_ref']); auth='per_user' →
        token_store.load_one(user, provider, account)."""
```

Adaptrar registrerar sig i `connectors/catalog.py` (importeras vid uppstart) —
samma självregistrerings-mönster som förmåge-registret (#6).

## 6. Migrering av befintliga verktyg (ingen beteendeändring)

- `calendar.py`: flytta `_RealDavAdapter`, `_PerUserGoogleAdapter`, `_ICalAdapter`,
  `_FreeBusyAdapter` till `connectors/adapters/` och registrera dem. `_resolve_calendar_dav`
  i `server.py` blir `registry.get(..., "calendar", user)` (behåll `CalendarAuthRequired`).
- `email.py`: bryt ut `_make_mailbox` → en `imap`-adapter (`MailBackend`); `email_*`
  kallar `registry.get(..., "mail", user)`. Behåll `_imap`-injektionen för test.
- `files.py`: nuvarande lokal-vault blir en `local` `FilesBackend`; WebDAV/Drive/OneDrive
  läggs till som nya adaptrar (Nextcloud i nästa spec).
- Nya kapabiliteter (`chat`, `issues`, `contacts`) får nya MCP-verktyg i egna PR:er.

## 7. Nya integrationer (efter ramverket)

Prioriterad ordning (var och en = en adapter + ev. nya verktyg, isolerat testbar):
1. **Microsoft Graph** (mail/calendar/files) — störst affärsmarknad (BACKENDS.md fas 2).
2. **Google** (Gmail/Calendar/Drive) — utöka nuvarande kalender-Google till mail/filer.
3. **Nextcloud** (files/contacts/chat) — [FEATURE-NEXTCLOUD-BACKEND.md](FEATURE-NEXTCLOUD-BACKEND.md).
4. **Chat** (Slack/Telegram) — `chat_post`/`chat_read`; dubblar som notiskanal (#1).
5. **Issues** (Jira/Linear/GitHub) — `issue_*`, tvåvägssynk mot backlog.
6. **Mötestranskript** — en `TranscriptSource` som matar text → #2-index + #4-regler.

## 8. Säkerhet & integritet

- **Credentials aldrig mot AI:n** — adaptrar hämtar hemligheter via `config.secret`/
  token-store serverside (BACKENDS.md-principen); loggas aldrig.
- **Per-user isolering** — `auth='per_user'` väljer token för den inloggade användaren;
  fel användare kan aldrig nå annans token (samma ACL som idag).
- **Projektskopning per konto** — ett länkat konto är inte automatiskt tillgängligt i
  varje projekt. `account_scopes` (token-store) avgör vilka projekt som får använda
  kontot, **per kapabilitet** — mail och kalender skopas var för sig. Grinden sitter i
  `registry.get()/get_all()`s `per_user`-grenar, så alla mail- och kalenderverktyg
  omfattas utan att något verktyg ändras. Default är opt-in: ett nyss länkat konto syns
  ingenstans förrän ägaren delat det (`account_scope_set`). Delade `acl.yaml`-resurser
  berörs inte — de tillhör projektet, inte användaren. Konton som fanns före funktionen
  fick `'*'` i en engångsmigrering (`backfill_scopes_once`, markör i `schema_meta`).
- **Feltålighet** — adapterfel isoleras per anrop (timeout + tydligt fel), fäller inte
  gatewayen. Retry/backoff för nätverksanrop.
- **Utgående via Utkorgen** — `chat_post`, `issue_create`, `email_send` m.fl. som är
  utgående går genom Utkorgen (#3) när projektet är i `review`.
- **Datahemvist** — dokumentera per adapter var datan ligger (BACKENDS.md §Ärlig avvägning).

## Byggordning

1. **Protokoll** (`connectors/base.py`) — kapabilitets-Protocols.
2. **Register** (`connectors/registry.py`) — spec, register, `get` med auth-val.
3. **Migrera kalender** — flytta adaptrarna, koppla `server._resolve_calendar_dav`.
4. **Migrera mail + files(local)** — bakom registret; befintliga tester oförändrade.
5. **Katalog** (`connectors/catalog.py`) — självregistrering vid uppstart.
6. **Första nya adapter** (Microsoft Graph *eller* Nextcloud) som bevis på pluggbarhet.
7. **Config + docs** — utöka `acl.example.yaml`-resursformatet (finns i BACKENDS.md).
8. **CI** — grönt.

---

## Utvecklingsinstruktioner

Konventioner: se [FEATURE-PROACTIVE-BRIEF.md](FEATURE-PROACTIVE-BRIEF.md). Kör
`python -m pytest -q` från `gateway/`. **Kritiskt: fas 3–4 får inte ändra
beteende** — befintliga `test_email.py`/`test_calendar.py` ska passera oförändrade.

### Steg 1 — `connectors/base.py`
Paket `connectors/__init__.py` + Protocols enligt §4. Spegla dagens `_dav`/`_imap`-
duck-typer exakt. **Test** (`tests/test_connectors_base.py`): en minimal fejk-adapter
uppfyller `MailBackend`/`CalendarBackend` (strukturellt).

### Steg 2 — `connectors/registry.py`
`ConnectorSpec`, `ConnectorRegistry.register/get`. `get` läser `acl.resource(project,
capability)`, väljer `type`, löser credentials (`shared`→`config.secret(cfg['*_ref'])`,
`per_user`→`token_store.load_one`), bygger adaptern via factory. Injicerbara
`config`/`token_store` för test. **Test** (`tests/test_connectors_registry.py`):
`type='imap'` bygger imap-adapter med rätt secret; okänd type → tydligt fel;
`per_user` utan token → `CalendarAuthRequired`/None enligt kapabilitet.

### Steg 3 — Migrera kalender
Flytta de fyra kalenderadaptrarna till `connectors/adapters/calendar_*.py`, registrera
dem, och låt `server._resolve_calendar_dav` delegera till `registry.get(...,'calendar',user)`.
**Test:** hela `test_calendar.py` passerar oförändrat; ett nytt test bygger adaptern via
registret.

### Steg 4 — Migrera mail + files(local)
✅ **Mail:** `server.py`'s `email_list`/`email_read`/`email_search`/
`email_create_draft` resolverar mailboxen via `registry.get(...,"mail",user)`
(en `_with_mail_backend`-wrapper som håller resolutionen innanför
`_audited`'s try/except, så ett okonfigurerat projekt fortfarande
audit-loggas identiskt med innan). `email_send` rör SMTP direkt — inte en
registrerad kapabilitet, orört. `tools/email.py`'s egna `_make_mailbox` finns
kvar oförändrad (används fortfarande av `catalog.py`'s `imap`-factory och
som fallback när `_imap` inte injiceras, t.ex. i enhetstester som kallar
`tools/email.py` direkt). **Test:** `test_email.py` oförändrat;
`test_email_server.py` nytt, täcker registret-bygget på server-lagret.

**Files(local) — avsiktligt inte migrerat, inte glömt:** `"files"`-kapabiliteten
är redan upptagen av Nextcloud-WebDAV (`nc_files_*`, se
FEATURE-NEXTCLOUD-BACKEND.md); den lokala valvet har en helt annan resursform
(bar sökväg i `acl.yaml`, inte `{type,url,...}`) och är en *ytterligare*
filkälla, inte samma kapabilitet under ett nytt namn. Att flytta
`tools/files.py` hit skulle antingen kollidera med webdav-resursen eller
kräva en ny kapabilitetsnyckel (`"vault"`) + schemaändring i `acl.yaml` —
ett produktbeslut, inte en mekanisk refaktor, så det lämnas som öppet
framtida arbete istället för att gissas fram.

### Steg 5 — `connectors/catalog.py`
Självregistrera alla inbyggda adaptrar; importera från `server.py`. **Test:**
katalogen registrerar minst imap/caldav/google/local; `registry.get` hittar dem.

### Steg 6 — Första nya adapter (bevis)
✅ **Microsoft Graph mail** — `connectors/adapters/mail_microsoft.py`'s `GraphMailAdapter`,
registrerad som `ConnectorSpec(type="microsoft", capability="mail", auth="per_user")` i
`catalog.py`. Byggd helt utan att röra `tools/email.py` — bevis på att en ny extern integration
verkligen bara kräver en ny adapter + registrering.

Graphs REST-API (JSON, mapp-id:n, `$search`/`$filter`) ser inget ut som IMAP, men
`connectors/base.py`'s `MailBackend` (och `tools/email.py`'s faktiska `_imap`-användning)
speglar imap_tools exakt: `.folder.set(namn)`, `.fetch(criteria, mark_seen=, limit=)` med
kriteriesträngarna `"ALL"` / `f"UID {id}"` / `f'BODY "{query}"'`, samt `.append(msg_bytes,
flags, folder=)`. Adaptern översätter: en liten parser för de tre kriteriesträngarna
`tools/email.py` någonsin skickar, en `.folder`-proxy som mappar mappnamn mot Graphs
välkända mapp-id:n, och ett meddelande-omslag som exponerar samma attribut
(`uid`/`subject`/`from_`/`date_str`/`seen`/`to`/`cc`/`text`/`html`) som imap_tools-meddelanden
har. v1-omfång: läsning (list/read/search) + append-till-Drafts — allt `tools/email.py`
anropar `_imap` för. `email_send` ligger kvar på SMTP; `In-Reply-To`-trådning tappas
medvetet vid utkast-skapande (Graph v1.0 saknar ett enkelt sätt att sätta godtyckliga
MIME-headers på ett nytt meddelande) — en dokumenterad brist, inte en tyst.

Auth är `per_user`: ett projekt sätter sin `mailbox`-resurs `type: microsoft`, och
gatewayen använder den inloggade användarens egna länkade `microsoft`-konto (samma
OAuth-länkningsflöde `account_link`/`account_link_callback` redan hanterar). `server.py`'s
`_ensure_fresh_microsoft_mail_token` uppdaterar en föråldrad access_token innan registret
läser den — `registry.get()`'s `per_user`-gren laddar bara vad som redan finns lagrat, den
uppdaterar inget själv (samma ansvarsuppdelning som `_resolve_calendar_dav`'s
Google-uppdatering). **Test:** `test_mail_microsoft_adapter.py` (mockad HTTP mot adaptern
isolerat) + `test_mail_microsoft_server.py` (token-uppdatering + registret end-to-end).

### Steg 6b — IMAP per-user + multi-account mail

✅ **IMAP som per-user-connector (parallellt med den delade).** Fram till nu var
`type="imap"` alltid `auth="shared"` — ett projekts mailbox-resurs, en
credential via `config.secret`. Nu kan en enskild användare *dessutom* länka
sin egen IMAP-brevlåda, precis som Microsoft-kontot i Steg 6, fast utan
OAuth. Ny adapter `connectors/adapters/mail_imap_user.py` (`build_mailbox`)
bygger en riktig `imap_tools.MailBox` från ett token-store-objekt
(`{host, user, password, port?}`) istället för från `acl.yaml`. Registrerad i
`catalog.py` som en *egen* type, `ConnectorSpec(type="imap_user",
capability="mail", auth="per_user", provider="imap")` — parallell med, inte
en ersättning för, den delade `type="imap"`-specen. De två har olika
`(capability, type)`-nycklar i registret och räknas därför aldrig som samma
källa i `get_all()`.

**Länkning utan OAuth.** IMAP har ingen auktoriseringsserver att skicka
användaren till, så `account_link(provider="imap")` returnerar inte en
`/link/imap?state=...`-URL utan en länk till `/app/settings#accounts`, där
ett formulär (`web/pages/settings.html` + `web/static/settings.js`) postar
`{account_email, host, user, password, port?}` till den nya routen `POST
/app/api/accounts/link-imap` (`web/api/accounts.py:api_accounts_link_imap`
→ `tools/account.py:account_link_imap`). Lösenordet är aldrig ett
MCP-tool-argument, loggas aldrig, och returneras aldrig — varken i
länksvaret, i felsvar, eller i `account_list`s output (token-storen krypterar
det vid vila; se `test_accounts_imap_web.py` och `test_account.py` för
uttryckliga negativa tester på det här).

**Flera mailkällor samtidigt.** `server.py`'s `_mail_backend` gick från
`registry.get(..., "mail", user)` till `registry.get_all(..., "mail", user)`
— med en enda källa (dagens vanliga fall: en delad IMAP eller ett länkat
Microsoft-konto) degenererar den till `sources[0][1]` direkt, noll
indirektion, byte-identiskt med innan (`test_email_server.py`/
`test_mail_microsoft_server.py` oförändrade och gröna). Med 2+ källor (t.ex.
projektets delade IMAP + en användares egen länkade IMAP, eller flera
länkade konton) fångar `connectors/adapters/mail_multi.py`s
`MultiMailBackend` upp dem: `fetch()` frågar alla källor och slår ihop
resultatet, varje meddelandes `uid` prefixas med en källetikett
(`"{label}|{uid}"`, `|` valt som separator eftersom etiketten själv redan
innehåller `:`) så att `email_read` kan dirigera en `UID {id}`-läsning
tillbaka till rätt källa. `append` (utkast) går alltid till den första
källan — samma en-brevlåda-semantik som innan, bara med ett explicit "vilken"
nu när det finns fler att välja på. `email_send`/SMTP är oberört och
avsiktligt utanför den här leveransen.

**Isolering bevisad, inte bara påstådd.** `test_mail_multi_account_server.py`
kör hela vägen genom den riktiga katalog-wiringen (inte en handbyggd fejk som
`test_email_server.py`): en användare utan länkat IMAP-konto ser bara
projektets delade brevlåda, aldrig en annan användares länkade konto
(`test_user_without_linked_imap_account_sees_only_shared_mailbox`,
`test_bobs_linked_account_never_appears_for_alice`), och ett separat test
(`test_token_store_queries_are_scoped_to_the_calling_user`) bevisar direkt
att `TokenStore.list_accounts(user)` filtrerar på `memaix_user` snarare än
att bara lita på verktygslagrets output.

### Steg 6c — Gmail som per-user mailkälla

✅ **Gmail API mail** — `connectors/adapters/mail_google.py`'s `GmailAdapter`, registrerad som
`ConnectorSpec(type="google_mail", capability="mail", auth="per_user", provider="google")`.
Samma översättningsjobb som Graph-adaptern, mot ett API som skiljer sig på andra punkter:
etiketter (`INBOX`/`DRAFT`) istället för mappar, `seen` härlett ur frånvaron av `UNREAD` i
`labelIds`, och en brödtext som ligger base64url-kodad nere i ett nästlat MIME-part-träd.
`users.messages.list` returnerar bara id:n, så varje meddelande kostar ett extra anrop —
därför sätter `limit` även `maxResults`, så att taket begränsar antalet hämtningar och inte
bara den färdiga listan.

Till skillnad från Graph-adaptern **tappas inte `In-Reply-To`**: `drafts.create` tar rå
RFC-822, så hela meddelandet inklusive trådningsheadern går fram oförändrat.

Notera `provider="google"` skilt från `type="google_mail"`. Provider är namnet i
token-lagret, och ett länkat Google-konto är *ett* konto — samma OAuth-token bär både
kalender och mail. Typen är det som pekas ut i acl.yaml. Att hålla isär dem är det som gör
att samma konto kan delas till ett projekt för kalender men inte för mail (§8).

Ett projekt behöver ingen `mailbox`-resurs alls för att använda Gmail: registrets
per-user-svep hittar länkade konton via token-lagret. Därför läser `tools/email.py`
avsändaradressen via `_inbox_address` (tom om resursen saknas) istället för det strikta
`_mailbox_cfg` — acl.yaml-resursen bar två orelaterade saker, inloggningsuppgifter och en
läsbar adress, och bara den första är obligatorisk. Saknas adressen utelämnas `From`-headern
helt (inte satt till tom sträng), och Gmail stämplar det autentiserade kontot självt.

**Scope-backfill, viktigt:** `LEGACY_PER_USER_CAPABILITIES` i `catalog.py` är en *fryst*
historisk ögonblicksbild, inte något som härleds ur registret. `google` står där med enbart
`["calendar"]`. Om den listan istället räknades fram ur registret skulle registreringen av
den här adaptern retroaktivt ha delat ut mail-åtkomst till varje projekt som redan hade ett
länkat Google-konto för kalender — precis det opt-in:et i §8 lovar att inte göra. Lägg
därför aldrig till en capability där när du lägger till en adapter.

**Test:** `test_mail_google_adapter.py` (mockad HTTP mot adaptern isolerat) +
`test_mail_google_server.py` (scope-grinden, token-uppdatering och ett Gmail-only-projekt
end-to-end).

### Steg 7 — Config + docs
Bekräfta att `acl.example.yaml`-resursformatet (BACKENDS.md §Config) räcker; lägg
ev. `auth: per_user`-flagga per resurs. Registrera doket i `docs/INDEX.md` (gjort);
uppdatera BACKENDS.md-fasrutan att peka hit.

### Steg 8 — Kör allt
`cd gateway && python -m pytest -q` + `python3 scripts/check-docs-index.py`.

### Acceptanskriterier
- [x] `email_*` fungerar oförändrat via registret (befintliga tester gröna); `calendar_*` kvar
      (dokumenterat skäl ovan), `files_*` (lokal vault) migreras inte hit (dokumenterat skäl ovan).
- [x] En ny adapter läggs till genom att registrera en `ConnectorSpec` — utan att röra verktygsfilerna
      (visat av contacts/webdav-files/tasks/deck/notes-adaptrarna, alla tillagda utan ändringar i
      `server.py`'s befintliga `email_*`/`calendar_*`-verktyg).
- [x] Ett projekt kör IMAP-mail, ett annat Microsoft Graph-mail, samtidigt (registret väljer per
      resurs `type`; visat av `test_mail_microsoft_server.py`).
- [x] `per_user`-adapter väljer rätt token för inloggad användare (TokenStore nycklas på
      `(user, provider, account)`; fel användare kan inte nå en annans `microsoft`-token).
- [ ] Utgående adapter-åtgärder (chat/issue/mail-send) går via Utkorgen (#3) i review-läge —
      `email_send` var redan utkorgs-gated innan detta ramverk; `chat`/`issue` har inga adaptrar än.
- [x] Credentials exponeras aldrig mot AI:n/loggar; adapterfel isoleras; hela sviten (714 tester) +
      docs-index grön.

---

## Framtida arbete
- Adapter-SDK dokumenterad för tredjepart (skriv din egen connector).
- Health-/capability-introspektion per adapter (visas i förmåge-registret #6).
- Rate-limit/kvot per extern tjänst (respektera API-gränser).
- OAuth-app-registrering guidas av wizarden (BACKENDS.md fas 5).
