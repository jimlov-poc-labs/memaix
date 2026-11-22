# Backends — koppla in Gmail, M365 och andra

Många kunder har redan Gmail/Google Workspace eller Microsoft 365 för mejl, kalender och
dokument. Memaix möter dem där de är via **backend-adaptrar**: samma MCP-verktyg (`email_*`,
`calendar_*`, `files_*`), olika backend bakom.

## Adapter-modellen

Verktygen är backend-agnostiska. Varje projekts resurs pekar på en **adapter-typ**; gatewayen
routar anropet rätt. Du kan blanda per projekt — ett projekt på Nextcloud/Purelymail, ett annat
på Gmail, ett tredje på M365.

```
email_* / calendar_* / files_*   (oförändrade MCP-verktyg)
        │
        ▼   adapter väljs per projekt-resurs
  ┌───────────────┬───────────────┬───────────────┬───────────────┐
  │ imap_smtp     │ caldav/webdav │ google        │ microsoft     │
  │ (Purelymail,  │ (Nextcloud,   │ (Gmail/Cal/   │ (Graph: Mail/ │
  │  generisk)    │  generisk)    │  Drive API)   │  Cal/OneDrive)│
  └───────────────┴───────────────┴───────────────┴───────────────┘
```

## Stödmatris

| Tjänst | Mejl | Kalender | Filer | Auth |
|---|---|---|---|---|
| Purelymail / generisk | IMAP/SMTP | — | — | App-lösenord |
| Nextcloud / generisk | — | CalDAV | WebDAV | App-lösenord |
| **Google Workspace / Gmail** | Gmail API | Calendar API | Drive API | OAuth 2.0 (per användare) |
| **Microsoft 365** | Graph API | Graph API | OneDrive/SharePoint (Graph) | OAuth 2.0 / Entra ID |

## Auth — Memaix fronter en OAuth-server och är själv OAuth-klient

- **Mot AI-klienten** (Claude/ChatGPT/…) fronter Memaix en **certifierad OAuth-server (ory Hydra)**;
  gatewayen validerar tokens (bygger ingen egen).
- **Mot Google/Microsoft** blir Memaix en OAuth-**klient**: den begär delegerad åtkomst till
  användarens konto och lagrar/refreshar tokens serverside. Användaren godkänner *en gång* när de
  kopplar upp sig — typiskt vid sin connector-inloggning (länkad identitet).
- Backend-credentials (tokens) lagras serverside, exponeras aldrig mot AI:n — samma princip som
  app-lösenord idag.

## Den avgörande fördelen med single-tenant

Eftersom varje kund kör sin **egen instans**, registrerar de (eller du som leverantör) en **egen
OAuth-app** mot sin egen Gmail/M365:

- **Google Workspace:** registreras som en **intern app** i kundens egen organisation → **undantagen
  Googles verifiering och CASA-granskning** ($500–$4 500/år som annars gäller publika appar).
- **Microsoft 365:** registreras som en **single-tenant Entra-app** i kundens tenant → admin
  godkänner i sin egen tenant, ingen marketplace-verifiering krävs.

Med andra ord: self-host + single-tenant **flyttar bort den dyra publika app-verifieringen** från
Memaix-produkten. Kostnaden blir istället en engångs-OAuth-registrering per kund (som wizard/
installer guidar, eller som du gör i installationstjänsten).

### Att använda sin egen mail/dokument är ett förstklassigt scenario
Vi **utesluter inget** — alla får koppla sin egen Gmail/M365 och läsa sina dokument. Det enda
som skiljer är registreringsvägen per kontotyp:

- **Workspace / M365** (där affärskunderna finns): egen intern/single-tenant-app → ingen publik
  verifiering, ingen CASA. Ren väg.
- **Konsument-Gmail** (privat, ej Workspace): stöds också, men kan inte använda intern-app-
  undantaget. Vägar: (a) enskild självhostare kör Googles **testing-läge** med sig själv som
  testanvändare — fungerar, men refresh-token förnyas var 7:e dag (friktion); (b) som **publik
  tjänst i skala** krävs verifiering + CASA ($500–4 500/år).

CASA-kostnaden gäller alltså bara publik åtkomst till *privat* Gmail i skala — inte Workspace,
inte M365. Det är ett vägval per kund, inte en spärr.

## Bundlad Nextcloud vs extern Nextcloud (filarea per projekt)

Två helt olika vägar delar samma mekanism (`nextcloud_provision` i `memaix.yaml`,
`gateway/src/memaix_gateway/nextcloud_provision.py`, se `docs/SECRETS.md`):

- **Bundlad Nextcloud** — compose-profilen `nextcloud` i `docker-compose.yml`, startad t.ex. med
  `docker compose --profile hydra --profile nextcloud up -d`. Nextcloud-imaget bootstrapar sig
  själv med ett adminkonto från `NEXTCLOUD_ADMIN_USER`/`NEXTCLOUD_ADMIN_PASSWORD` i `.env` (det
  officiella `nextcloud`-imagets `docker-entrypoint.sh` kör `occ maintenance:install` automatiskt
  vid första start när dessa + en databasvariabel är satta — `docker-compose.yml` sätter
  `SQLITE_DATABASE` för just detta). Setup-motorn (`scripts/setup_engine.py::write_config`)
  **genererar lösenordet och skriver `nextcloud_provision`-blocket åt dig**, med `url:
  "http://nextcloud"` (det interna Docker-nätverksnamnet — porten 8081 i compose-filen är bara
  host-publiceringen, aldrig vad gatewayen själv anropar) och `admin_password_ref:
  "env:NEXTCLOUD_ADMIN_PASSWORD"`, som pekar på exakt det konto imaget bootstrapades med. Ingen
  manuell adminidentitet behövs — wizard-fråga 6 (`docs/WIZARD.md`) kopplar in detta val
  (`files_backend: bundled_nextcloud`) i båda bärarna (CLI och webb).
- **Extern, redan existerande Nextcloud** (kunden har redan en instans, bundlar ingen egen) — kräver
  fortfarande ett **riktigt adminkonto som kunden själv skapar** på just den instansen, och matar in
  `url`/`admin_user`/`admin_password_ref` för hand (`config/memaix.example.yaml` har ett exempel).
  Wizarden kan inte generera eller bootstrapa en adminidentitet på en server den inte äger — det är
  skillnaden mot den bundlade vägen ovan, inte en brist som ska byggas bort.

Båda vägarna använder `method: ocs` (OCS-provisioneringsAPI:t, med ett riktigt lösenord — inte ett
app-lösenord, som Nextcloud 32 vägrar för `addUser`). `method: occ_ssh` (ssh till värden, se
`docs/SECRETS.md`) är en separat, senare optimering och gäller inte den bundlade containern, som
saknar en SSH-server att nå in i.

På `method: ocs`-vägen kan ett upprepat `project_create`-anrop efter ett tidigare timeout nu
självläka: om hemlighetsfilen redan finns (lämnad kvar av ett avbrutet försök vars nätverksanrop
kan ha gått igenom innan vi gav upp att vänta) görs en levande, läsande OCS-kontroll mot kontot
`memaix-<namn>` med exakt det lösenordet. Stämmer den — kontot finns och svarar på just det
lösenordet — behandlas försöket som redan lyckat (samma svar, ingen ny skrivning). Stämmer den
inte (kontot saknas, eller lösenordet matchar inte) höjs samma fel som tidigare, och manuell
uppstädning krävs fortfarande. Gäller bara OCS-vägen, inte `occ_ssh`.

## Config (per projekt-resurs)

```yaml
projects:
  acme:
    mail:     { type: google,     account_ref: GOOGLE_ACME }     # gmail/workspace
    calendar: { type: google }
    files:    { type: google_drive, folder_id: "0A..." }
    vault: "/srv/vaults/acme"
  globex:
    mail:     { type: microsoft,  account_ref: MS_GLOBEX }       # m365 via Graph
    calendar: { type: microsoft }
    files:    { type: onedrive }                                 # eller sharepoint
    vault: "/srv/vaults/globex"
  internal:
    mail:     { type: imap, host: imap.purelymail.com, user: team@... , password_ref: PM_INT }
    calendar: { type: caldav,  url: "https://cloud.../calendars/team/work/" }
    files:    { type: webdav,  url: "https://cloud.../files/team/Internal/" }
    vault: "/srv/vaults/internal"
```

`*_ref` pekar på OAuth-token/klient-credentials i `.env` (aldrig i repot).

## Per användare eller delad?
- **Delad projektresurs** (t.ex. en gemensam supportbrevlåda): en service-/delad identitet.
- **"Koppla din egen Gmail/M365"**: per-användar-delegering — varje person godkänner Memaix mot
  sitt eget konto vid uppkoppling. Gatewayen väljer rätt token utifrån den inloggade användaren.
  Detta är mer komplext (token per användare) men ger "min assistent når min inkorg".

## Ärlig avvägning
- **För:** möter folk där de är, ingen migrering, lägre adoptionströskel.
- **Mot:** fler rörliga delar (extern OAuth, token-refresh, API-gränser), och **datan bor kvar hos
  Google/Microsoft**. "Äg din data"-löftet gäller då din *Memaix och ditt minne* — pekar du den mot
  Gmail lever den datan fortfarande i Google. Var tydlig med kunden om var data ligger.
- **Renaste self-host** (max dataägande) = Purelymail + Nextcloud. **Lägsta friktion** = deras
  befintliga Gmail/M365. Adapter-modellen låter varje kund välja, till och med per projekt.

## Faser
1. **Adapter-abstraktion** — bryt ut nuvarande IMAP/CalDAV/WebDAV bakom ett gemensamt interface.
2. **Microsoft Graph-adapter** — mail/kalender/OneDrive (störst affärsmarknad; basic auth dör 2026).
3. **Google-adapter** — Gmail/Calendar/Drive API, intern-app-flöde för Workspace.
4. **Per-användar-OAuth** — länkad identitet, token per användare, refresh. Detaljspec:
   [PER-USER-OAUTH.md](PER-USER-OAUTH.md).
5. **Wizard-stöd** — guida OAuth-app-registreringen (kan inte fullautomatiseras; konsol-steg hos
   Google/Microsoft) och skriv in credentials i `.env`.

## Acceptanskriterier
- [ ] Samma `email_*`/`calendar_*`/`files_*`-verktyg fungerar oavsett backend.
- [ ] Ett projekt kan köra Gmail, ett annat M365, ett tredje Nextcloud — samtidigt.
- [ ] Workspace-/M365-kund kopplas in utan publik app-verifiering (egen intern/single-tenant-app).
- [ ] Tokens lagras serverside, refreshas, och exponeras aldrig mot AI:n.
- [ ] Kunden informeras om var data ligger per backend-val.
