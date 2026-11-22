# Per-användar-OAuth — "koppla din egen Gmail/M365"

Detaljspec för Fas 4 i `BACKENDS.md`: hur en person länkar sitt eget Google-/Microsoft-konto, hur
Memaix lagrar och förnyar deras token, och hur gatewayen väljer rätt persons token vid varje anrop.

## Två OAuth-lager (blanda inte ihop dem)

```
   AI-klient ──OAuth(server)──►  Memaix  ──OAuth(klient)──►  Google / Microsoft
   "vem är du?"                  (gateway)                   "läs den här personens mejl"
```

1. **Memaix fronter en OAuth-server (ory Hydra)** mot AI:n (Claude/ChatGPT) — identifierar *vem som
   frågar*; gatewayen validerar tokens. (Del av stacken.)
2. **Memaix som OAuth-klient** mot Google/MS — hämtar delegerad åtkomst till *den personens* konto.

Det andra lagret är nytt och kan **inte** ske inuti AI-chatten (ingen webbläsar-redirect där). Det
kräver ett separat, engångs **länkningsflöde** via en sida Memaix själv serverar.

## Instansnivå: admin konfigurerar VILKEN OAuth-app (en gång)

Innan någon enskild person kan köra länkningsflödet nedan måste instansens admin först
berätta för Memaix VILKEN OAuth-app som ska användas. Varje kund driver sin **egen** app —
aldrig en Memaix-delad app — så att samtycket, granskningen och återkallningen ligger hos
kunden, inte hos oss.

- `setup_engine.py` (`defaults()`/`validate()`/`write_config()`) samlar in
  `google_oauth_client_id`/`google_oauth_client_secret` och
  `microsoft_oauth_client_id`/`microsoft_oauth_client_secret`/`microsoft_oauth_tenant_id`.
  Tomt = ingen provider konfigureras; `account_link` för den providern ger `unknown_provider`
  tills en admin fyller i detta och kör setup om.
- Resultatet skrivs till `config/memaix.yaml` under `oauth_providers.<provider>` (`client_id`
  i klartext, `client_secret_ref: "env:GOOGLE_CLIENT_SECRET"` / `env:MICROSOFT_CLIENT_SECRET"`)
  och själva hemligheten till `.env` (0600) — samma `*_ref`-mönster som `LLM_API_KEY`
  (se SECRETS.md). Klartextsecreten hamnar aldrig i YAML eller i setup-kvittot.
- Steg 3 i wizarden (ej byggt i detta uppdrag) kopplar frågorna i `bootstrap.py`/`setup_web.py`
  till denna motor.

### Vad en admin behöver hämta (på hög nivå — gränssnitten ändras, principen inte)

**Google (Google Cloud Console):**
1. Skapa (eller återanvänd) ett GCP-projekt för kundens egen Memaix-instans.
2. Aktivera OAuth-samtyckesskärmen. **Internal** (bara Workspace-tenantens egna användare)
   slipper Googles publika app-granskning helt — använd det om kunden kör Workspace.
   **External** kräver granskning för känsliga/restricted scopes (se nedan).
3. Skapa ett **OAuth client ID** av typen "Web application".
4. Lägg till redirect-URI: `https://<instansens-domän>/link/google/callback`
   (för en lokal trial: `http://localhost:8080/link/google/callback`).
5. Kopiera **Client ID** (slutar på `.apps.googleusercontent.com`) och **Client secret**
   in i setup.
6. **CASA-granskning:** kalenderscopes (`calendar.events`, `calendar.calendarlist.readonly`)
   räknas som *sensitive* och kräver bara Googles vanliga appgranskning. Gmail-scopes
   (`gmail.readonly` m.fl.) räknas som *restricted* och kräver dessutom en betald
   **CASA-säkerhetsgranskning** innan Google tillåter dem i produktion för en extern
   consent-skärm (Workspace-interna appar slipper detta). Det är kundens egen app och
   kundens eget ansvar att genomföra — Memaix begär därför Gmail bara som en explicit
   opt-in-capability (`account_link(capabilities=["mail"])`), aldrig i baslänkningen; se
   "Scopes" ovan för varför Gmail normalt länkas över IMAP i stället.

**Microsoft / Azure AD (Entra-portalen):**
1. Registrera en **app registration** i kundens EGNA Entra-tenant (BACKENDS.md: single-tenant
   Entra-app, inte multi-tenant/marketplace — en admin i kundens tenant godkänner den direkt,
   ingen publik verifiering krävs).
2. Lägg till en **redirect URI** (plattform: Web):
   `https://<instansens-domän>/link/microsoft/callback`.
3. Under **Certificates & secrets**, skapa en **client secret** och kopiera värdet direkt
   (det går inte att visa igen) — notera utgångsdatumet, rotation är admins ansvar.
4. Kopiera **Application (client) ID** och **Directory (tenant) ID** från appens Overview-sida.
5. Ge appen de delegerade Graph-behörigheterna som motsvarar scopes-listan (Mail.Read,
   Calendars.ReadWrite, offline_access) och godkänn admin consent i tenanten.
6. **Känt hål (inte löst i detta uppdrag):** gatewayens authorize-/token-URL är idag hårdkodad
   mot `login.microsoftonline.com/common/...` (`server_http.py`, `_refresh_microsoft_token` i
   `server.py`). Microsoft avvisar ofta `/common` för en strikt single-tenant app-registrering.
   `tenant_id` samlas in och skrivs till config redan nu för att inte tvinga en andra
   migrering senare, men gatewayen läser den inte än — se "Kantfall".

## Länkningsflödet (engångs per person & provider)

```
1. Användaren (eller en tool) upptäcker att kontot inte är länkat → får en länk-URL.
2. Användaren öppnar  https://mcp.<domän>/link/google   i en webbläsare.
3. Memaix startar OAuth mot Google/MS (PKCE, access_type=offline, prompt=consent).
4. Användaren loggar in hos Google/MS och godkänner scopes.
5. Google/MS redirectar tillbaka till Memaix med en code.
6. Memaix byter code → access_token + refresh_token, och lagrar dem KRYPTERAT,
   nyckel = (memaix_user, provider, konto).
7. Klart. Framtida verktygsanrop använder den lagrade token automatiskt.
```

Hur användaren upptäcker steg 1: ett verktygsanrop mot en olänkad backend returnerar felet
`auth_required` med `link_url`, och onboarding-intervjun erbjuder att länka direkt.

## Token-modell

- Lagra **refresh_token** (långlivad) krypterat; mint access_token vid behov och cacha kort.
- **Kryptering i vila:** envelope encryption. Datanyckel per post, huvudnyckel i `.env`/KMS.
  Klartext-token finns bara i minnet vid användning.
- **Nyckel:** `(memaix_user, provider, account_email)` — en person kan länka flera konton.
- **Refresh:** förnya access_token automatiskt; misslyckas refresh (återkallad/utgången) →
  markera kontot `needs_relink` och returnera `auth_required` nästa anrop.

## Val av token vid anrop

```
verktyg(project, …)
  → OAuth-subject  → memaix_user            (samma som RBAC)
  → projektets resurs: { type: google, auth: per_user }
  → slå upp (memaix_user, google) i token-store
  → mint access_token → anropa Gmail/Graph som den användaren
```

- `auth: per_user` → använd den **inloggade** användarens länkade token ("min inkorg").
- `auth: shared` → använd ett **delat** tjänstekonto för projektet (t.ex. gemensam supportbrevlåda).

## Scopes (minst privilegium)

| Behov | Google-scope | Microsoft Graph |
|---|---|---|
| Läsa mejl | `gmail.readonly` | `Mail.Read` |
| Skapa utkast | `gmail.compose` | `Mail.ReadWrite` |
| Skicka (bakom `allow_send`) | `gmail.send` | `Mail.Send` |
| Kalender | `calendar.events` + `calendar.calendarlist.readonly` | `Calendars.ReadWrite` |
| Filer (läs) | `drive.readonly` | `Files.Read.All` |
| Filer (skriv egna) | `drive.file` | `Files.ReadWrite` |
| Förnyelse offline | `access_type=offline` | `offline_access` |

**Gmail över IMAP i stället för Googles API (sedan 2026-10).** Gmail-scopes är *restricted* och kräver en
betald säkerhetsgranskning (CASA). Därför begär Google-länkningen bara kalenderscopes ovan. Ett Gmail-konto
länkas som brevlåda över IMAP med ett app-lösenord (inställningar → länka brevlåda → välj *Gmail*; kräver
tvåstegsverifiering på kontot, och en Workspace-admin kan ha stängt av app-lösenord). Memaix läser då
mappen flaggad `\All` (`[Gmail]/All Mail`, på svenska `[Gmail]/Alla mail`) och sparar utkast i mappen
flaggad `\Drafts` — Memaix går på flaggorna, inte på namnen. Redan länkade Google-konton behåller de
behörigheter de gavs tills de länkas om.

Begär bara det projektets verktyg faktiskt använder. Gmail/Drive-scopes är **restricted** → se
CASA-noten i `BACKENDS.md` (gäller bara publik konsument-Gmail i skala).

## MCP-verktyg för kontolänkning

| Verktyg | Parametrar | Returnerar | Roll |
|---|---|---|---|
| `account_link` | `provider` | `{link_url, expires}` | alla (länkar sitt eget) |
| `account_list` | — | `[{provider, account, scopes, status}]` | alla |
| `account_unlink` | `provider, account` | `{ok}` | alla (sitt eget) |

Plus felet `auth_required` (med `link_url`) som vilket backend-verktyg som helst kan returnera när
den inloggade användarens token saknas eller är ogiltig.

## Säkerhet

- Token krypterade i vila; klartext bara i minnet vid anrop; aldrig i loggar eller mot AI:n.
- **Återkallning:** `account_unlink` raderar token och återkallar hos providern där möjligt.
- **State + PKCE** i länkningsflödet mot CSRF/injection.
- **Redirect-URI** låst till instansens egen domän.
- Audit: logga länkning/avlänkning och vilket konto ett verktygsanrop använde.

## Kantfall

- **Refresh-token utgången/återkallad** → `needs_relink`, verktyg returnerar `auth_required`.
- **Konsument-Gmail i testing-läge:** Googles refresh-token slutar gälla efter 7 dagar → personen
  måste länka om. (Workspace/M365 har inte denna gräns.) Dokumentera tydligt.
- **Flera konton per person:** `account_list` visar alla; projektets config kan peka på vilket
  konto som gäller, annars fråga.
- **Admin återkallar i Workspace/tenant:** behandla som `needs_relink`.
- **Microsoft single-tenant-appen + `/common`-ändpunkten:** instanskonfigurationen samlar in
  `microsoft_oauth_tenant_id`, men `link_start`/`_exchange_code`/`_refresh_microsoft_token`
  pekar idag hårdkodat mot `login.microsoftonline.com/common/...`. Ej åtgärdat här —
  lägg in `provider_cfg.get("tenant_id", "common")` på de tre ställena som egen uppgift
  innan en kund med en strikt single-tenant-app testar `/link/microsoft`.

## Faser
1. **Länkningssida + token-store** (krypterad) för en provider (börja med Microsoft Graph).
2. **Token-val per anrop** (`auth: per_user`) + `auth_required`-felet med länk.
3. **account_link/list/unlink**-verktyg + onboarding-integration.
4. **Google-adapter** med intern-app-flöde (Workspace) och offline-refresh.
5. **Härdning:** envelope encryption, audit, återkallning, omlänknings-UX.

## Acceptanskriterier
- [ ] En person länkar sin egen Gmail/M365 via en webbläsarsida, en gång.
- [ ] Efter länkning når AI:n personens egen inkorg/kalender/filer utan att se token.
- [ ] Olänkad backend → `auth_required` med en fungerande `link_url`.
- [ ] Token lagras krypterat, refreshas automatiskt, och kan återkallas via `account_unlink`.
- [ ] Rätt persons token väljs utifrån den inloggade användaren vid varje anrop.
