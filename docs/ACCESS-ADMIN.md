# Projekt och medlemmar via MCP

Skapa projekt, gör människor till ägare av ett enskilt projekt och bjud in nya personer — allt
från din AI-klient, utan att röra `acl.yaml` för hand.

## Rollmodell

| Roll | Vad den får |
|---|---|
| **Systemadmin** (`admin: true`) | Implicit ägare av alla projekt. Enda som kan skapa projekt. |
| **Projektägare** (`owner` på ett projekt) | Hantera medlemmar och bjuda in — bara i *det* projektet. |
| `collaborator` / `reader` | Som tidigare; ingen åtkomsthantering. |

En projektägare kan aldrig skapa projekt, röra andra projekt eller ändra en systemadmin.
Ett konto som bjuds in får aldrig `admin`.

## Verktyg

| Verktyg | Vem | Effekt |
|---|---|---|
| `project_create(name)` | systemadmin | Nytt projekt med eget git-valv (`/srv/vaults/<namn>`). Du blir ägare, ingen annan får något. Namn: 2–32 tecken `a-z 0-9 - _`. Är `nextcloud_provision` satt i `memaix.yaml` får projektet även ett eget Nextcloud-konto (`files:`, se `SECRETS.md`). Med `require_files: true` i `memaix.yaml` nekas skapandet i stället om filarean inte kan skapas (inget projekt blir kvar). |
| `project_members(project)` | projektägare | Lista medlemmar och roller. |
| `project_member_set(project, member, role)` | projektägare | Ändra roll, eller `role: null` för att ta bort. |
| `user_invite(project, invitee, role, email?)` | projektägare | Skapa ett inloggningskonto begränsat till projektet. |
| `user_reset_link(project, member)` | projektägare | Återställningslänk för en medlem som redan har lösenord. |

`project_member_set`, `user_invite` och `user_reset_link` **köas i utkorgen** och körs först när en människa godkänner
(`outbox_approve` eller webbens utkorg). Behörigheten kontrolleras igen vid godkännandet, så en
ägare som degraderats i mellantiden kan inte få en gammal åtgärd genomförd.
Skyddsregel: den sista icke-admin-ägaren i ett projekt kan inte ta bort sig själv
(systemadmin kan alltid ändra).

## Inbjudan med eget lösenord

1. `user_invite` → godkänn i utkorgen.
2. Godkännandet returnerar `invite_url` (`https://<public_url>/app/invite/<token>`).
   Skicka länken till personen.
3. Personen väljer eget lösenord (minst 12 tecken) på sidan. Länken gäller en gång och i
   7 dagar; en ny inbjudan ogiltigförklarar den gamla.
4. Därefter kopplar personen in Memaix i sin AI-klient (se [AI-CLIENTS.md](AI-CLIENTS.md)) och loggar in
   med användarnamnet och lösenordet.

## Återställningslänk

`user_reset_link` → godkänn i utkorgen → `reset_url` (samma sida och samma regler som inbjudan:
en gång, 7 dagar, minst 12 tecken). Det är det enda sättet en länk får byta ett befintligt lösenord;
en vanlig inbjudningslänk gör det aldrig. Målet måste vara medlem i projektet och icke-admin, och en
projektägare kan bara återställa konton vars alla behörigheter ligger i projekt hen själv äger
(annars kan en ägare ta över ett konto som når andra projekt). Systemadmin kan återställa alla
icke-admin-konton. Det gamla lösenordet gäller tills länken används.

Säkerhet: bara SHA-256 av token sparas (`memaix-invites.db`), länken kan aldrig sätta lösenord på ett
konto som redan har ett, och sidan svarar `no-store` utan Referer. Tokenen finns i klartext i utkorgens
resultatrad tills den används — samma förtroendezon som `acl.yaml`.

## Gränser (kända)

- Webb-UI:t (`/app`) har egen inloggning via `MEMAIX_ALLOWED_USERS`; inbjudna konton loggar in via
  MCP/OAuth, inte där.
- Ingen e-postutskick av länken ännu — du levererar den själv.
- Ta bort ett helt konto: sätt `disabled` i admin-UI:t.

## Drift

Login-appen läser `acl.yaml` när filen ändras, så nya konton kan logga in utan omstart.
Login-appen monterar `acl.yaml` som fil; `AclWriter` skriver i samma fil (samma inode, `.bak1` först) och
en omkopierad fil skulle annars lämna login-appen med den gamla. Hela `config/` monteras inte: där ligger servicekontonycklar.
`MEMAIX_VAULTS_DIR` (standard `/srv/vaults`) styr var nya valv skapas.

## Koppla teamets mejl — hela flödet, från färdig instans till fungerande koppling

Din instans kör, du är admin. Så kopplar du mejl.

### Första frågan: ett delat teamkonto, eller var och ens egen inkorg?

Det finns två helt olika mekanismer i Memaix, och de blandas lätt ihop:

| Vill du... | Mekanism | Vem ser mejlet |
|---|---|---|
| Ett **gemensamt** konto hela teamet läser som samma brevlåda (t.ex. `info@dittforetag.se`) | `cli.py mailbox-set` (nedan) — skriver `acl.yaml`:s delade `mailbox`-resurs | Alla med åtkomst till projektet |
| **Din egen** inkorg, kopplad till ditt konto, synlig för dig i de projekt du väljer | `link-imap` (denna sida) | Bara du — varje person kör det för sig själv |

`link-imap`/`account_link_imap` är den mekanism som redan finns färdigbyggd (BACKENDS.md,
PER-USER-OAUTH.md, FEATURE-CONNECTOR-FRAMEWORK.md §6b) — den ger **en** person **sin egen**
brevlåda, aldrig ett delat teamkonto. Vill alla i teamet ha mejl i samma projekt kör var och en
`link-imap` en gång för sitt eget konto; gatewayen slår ihop varje persons egna källor med
projektets delade mailbox (om en finns) när just den personen frågar — ingen ser någon annans
länkade konto (`FEATURE-CONNECTOR-FRAMEWORK.md`, `test_bobs_linked_account_never_appears_for_alice`).

### Ett delat teamkonto (den första raden i tabellen)

```sh
printf '%s\n' "$PW" | docker compose exec -T gateway \
  python -m memaix_gateway.cli mailbox-set acme info@acme.se \
  --host imap.purelymail.com
```

Lösenordet läses från stdin, aldrig argv (samma mönster som `link-imap`). Kommandot:

- vägrar om `acme` inte redan finns som projekt i `acl.yaml`,
- vägrar om projektet redan har en `mailbox:`-resurs — lägg till `--force` för att medvetet
  byta ut den (t.ex. vid lösenordsrotation eller byte av brevlåda),
- skriver lösenordet till en egen hemlighetsfil under `config/secrets/` (katalog `0700`, fil
  `0600`, skapas aldrig tyst över en befintlig fil utan `--force` — samma mönster som
  Nextcloud-provisioneringens hemlighetsfiler, SECRETS.md),
- skriver `acl.yaml`s `mailbox:`-resurs via samma atomiska `AclWriter` (backup `.bak1-3`) som
  övrig ACL-administration använder — aldrig en egen parallell YAML-skrivare.

Resultatet i `acl.yaml`:

```yaml
projects:
  acme:
    mailbox:
      host: imap.purelymail.com
      user: info@acme.se
      password_ref: "file:/app/config/secrets/mailbox-acme"
```

Valfria flaggor:

- `--smtp-host`/`--smtp-port` — skriver en syskonresurs `smtp:` (host/port) som `email_send`/
  `email_forward`/`email_reply` använder för utgående; utan den används mailboxens egen host på
  port 587 med STARTTLS (ingen `imap.`→`smtp.`-gissning för delade mailboxar, till skillnad från
  `link-imap`). SMTP-leveransen kör alltid STARTTLS — port 465 (implicit TLS) stöds inte här.
- `--port` — tas emot och sparas på resursen för symmetri med `link-imap`, men läses i dagsläget
  **inte** av den delade IMAP-adaptern (`tools/email.py`s `_make_mailbox`), som alltid kopplar på
  standardporten. Känd begränsning, inte en bugg.

**Starta om gatewayen efteråt** (`docker compose restart gateway`): till skillnad från login-appen,
som läser om `acl.yaml` live, cachar den körande gatewayprocessen `Acl` i minnet
(`server._get_acl`/`reload_acl`) och ser bara en ändring skriven från en annan process (som denna
CLI) efter omstart.

`password_ref` stöder även `env:`/`vault:`/`kms:` (docs/SECRETS.md) om du hellre redigerar för
hand, men `mailbox-set` skriver alltid `file:` — det går att läsa om direkt (ingen omstart krävs
för att *läsa* den nya hemligheten, bara för att gatewayen ska se den nya `mailbox:`-resursen i
`acl.yaml`).

**OBS:** `mailbox-set` är en instansbred admin-operation (den som kan köra `docker compose exec`
mot gatewayen), inte en projektägaroperation — det finns ingen inloggad "actor" i CLI:t att
kontrollera mot, till skillnad från `project_member_set` m.fl. i `access_admin.py`. Skaladgång till
containern är gränsen här, precis som för `link-imap` och `backend-status`.

### Var och ens egen inkorg (resten av det här avsnittet)

Resten av det här avsnittet gäller den andra kolumnen: en enskild persons egen koppling, körd av
en admin åt dem (eller av dem själva, om de har skalaccess).

### Steg 1 — hitta vad som väntar på koppling (valfritt, men ett bra första steg)

Om du (eller den som satte upp instansen) redan skrivit in i `acl.yaml` vilka projekt som väntar
på en mejlkoppling (`projects.<namn>.awaiting_link: ["mail"]` — bara en lapp, inget lösenord),
kan du se listan:

```sh
docker compose exec -T gateway python -m memaix_gateway.cli backend-status
```

Visar t.ex. `acme: mail — väntar på koppling`, eller `acme: mail — kopplad för jimmy
(imap/jimmy@acme.se)` om någon redan länkat. Ingen `awaiting_link` satt alls → kommandot säger att
inget väntar; det är inte samma som "allt är kopplat", bara att ingenting är deklarerat som
väntande. `--project <namn>` filtrerar till ett projekt.

### Steg 2 — länka mejlboxen

Du behöver IMAP-uppgifterna för kontot (host, användarnamn, lösenord — en app-lösenord om kontot
har tvåstegsverifiering, se nedan). Lösenordet skickas **aldrig** som kommandoradsargument — det
läses från standard in, en rad:

```sh
read -rs PW                              # skriv lösenordet, syns inte, hamnar inte i historiken
printf '%s\n' "$PW" | docker compose exec -T gateway \
  python -m memaix_gateway.cli link-imap \
  --user jimmy --account jimmy@acme.se --host imap.purelymail.com \
  --projects acme --verify
unset PW
```

`-T` krävs när stdin kommer från en pipe (`printf ... | docker compose exec -T ...`) —
utan `-T` vägrar docker med "the input device is not a TTY". Körs kommandot utan pipe
(bara `docker compose exec gateway python -m memaix_gateway.cli link-imap ...`, inget `-T`)
frågar det i stället efter lösenordet interaktivt (`getpass`, syns inte på skärmen) — enklare
för en enstaka körning där du sitter kvar vid terminalen. `--verify` loggar in över IMAP innan något sparas, så ett fel host/port/lösenord
ger ett tydligt fel direkt (`IMAP-inloggning misslyckades: <feltyp>`, aldrig lösenordet) istället
för att först upptäckas nästa gång `email_list` körs. `--projects` är vilka projekt kontot blir
synligt i för just `--user`; `*` betyder alla projekt personen redan har åtkomst till.

Kommandot skriver aldrig ut lösenordet — bekräftelsen är `Länkade jimmy@acme.se för jimmy; mejl
delas med: acme`.

### Steg 3 — bekräfta

Kör `backend-status` igen (steg 1) om projektet har `awaiting_link` satt — raden ska nu säga
`kopplad för <user> (imap/<konto>)`. Annars: be personen (eller kör själv) ett vanligt `email_list`-
anrop mot projektet i din AI-klient.

### Särskilda leverantörer

- **Microsoft 365:** lösenordsbaserad IMAP fungerar inte mot Exchange Online/M365 —
  `link-imap` är inte vägen dit. Använd OAuth-kopplingen i stället
  (`account_link(provider="microsoft")`, se PER-USER-OAUTH.md).
- **Gmail / Google Workspace:** `link-imap` ÄR Gmail-vägen (PER-USER-OAUTH.md) — Googles
  Gmail-API används inte (restricted scope, kräver betald CASA-granskning). Kontot behöver
  tvåstegsverifiering och ett app-lösenord (inte det vanliga lösenordet) som `--account`/
  `--host imap.gmail.com`. Memaix läser mappen flaggad `\\All` och skriver utkast till
  `\\Drafts` (flaggorna, inte de svenska/engelska mappnamnen).
- **Purelymail och de flesta andra IMAP-leverantörer:** vanligt lösenord eller app-lösenord,
  `link-imap` fungerar rakt av.
