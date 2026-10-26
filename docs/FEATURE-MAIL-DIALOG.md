# Dialogbaserad mejlklient + sändupplåsning per session

Status: byggd (gren `feat/mail-dialog-client`, 2026-10-26). Kod: `gateway/src/memaix_gateway/tools/mail_client.py`,
`tools/mail_unlock.py`, `connectors/mail_accounts.py`, verktygen i `server.py` (avsnittet
"Dialog mail client"). Tester: `gateway/tests/test_mail_dialog.py`.

## Verktyg

| Verktyg | Roll | Vad |
|---|---|---|
| `email_overview(projects?, unread_only=True, limit_per_box=20)` | collaborator | Rubriker (från, ämne, datum, läst, flaggad) per låda över alla lådor du kan läsa. Ingen brödtext. Varje meddelande får ett kort nummer `n`. |
| `email_set_flags(items, action, project?, folder="INBOX")` | collaborator | `read`, `unread`, `flag` (\Flagged = kräver åtgärd), `unflag`. |
| `email_archive(items, project?, folder="INBOX")` | collaborator | Flyttar till lådans SPECIAL-USE `\Archive`-mapp (reserv: kända namn som `Archive`/`Arkiv`). Skapar aldrig mappar; saknas mappen rapporteras det. |
| `email_move(items, destination, project?, folder="INBOX")` | collaborator | Flyttar till en befintlig mapp. |
| `email_forward(item, to, note="", mode="inline", ...)` | owner + upplåst | Inline med citat och originalets bilagor, eller hela originalet som `.eml` (`mode="attachment"`). |
| `email_reply(item, body, reply_all=False, quote=True, ...)` | owner + upplåst | `In-Reply-To`/`References`, `Re:`, svarar Reply-To (annars From); svara-alla lägger till To/Cc utom den egna adressen. |
| `email_send_unlock_request()` | owner | Skickar en engångskod till ägaren utanför chatten. |
| `email_send_unlock(code)` | owner | Låser upp den här MCP-sessionen. |
| `email_send_lock()` | alla | Låser direkt igen. |

`items` är nummer från `email_overview` (`"3"`, `"#3"`) eller `["all"]` (alla i senaste översikten). Med
`project` är `items` i stället id:n som `email_list`/`email_search` ger i det projektet.

Numren gäller per (användare, MCP-session) och är stabila hela sessionen. Ett flyttat eller arkiverat meddelande
tappar sitt nummer, eftersom UID:t inte längre gäller i INBOX. Numren är ingen säkerhetsgräns: varje
åtgärd kontrollerar rollen igen på projektet som numret lästes genom.

Varje låda läses en gång, under det första projektet (alfabetiskt) som får använda den, och loggas ut efteråt.
Lådor läses parallellt, högst 6 åt gången. En låda som inte svarar hamnar i `errors`, och de andra visas ändå.

Flaggor, flytt och utskick fungerar bara för IMAP-lådor (Purelymail, delade acl.yaml-lådor). Gmail- och
Outlook-kopplade konton syns i översikten men ger ett tydligt fel för skrivåtgärder.

## Utskick

Utskick går via lådans egen SMTP och sparas med `\Seen` i lådans `\Sent`-mapp (reserv: kända namn). Hittas
ingen Skickat-mapp är mejlet ändå skickat, och resultatet säger det.

- **Länkat IMAP-konto:** `smtp_host`/`smtp_port` från kontot. Saknas `smtp_host` härleds det från en
  IMAP-värd som börjar på `imap.` (`imap.purelymail.com` -> `smtp.purelymail.com`). Annars blir det fel.
  Port 587 = STARTTLS (standard), 465 = TLS. Certifikaten verifieras.
- **Projektets acl.yaml-låda:** `smtp`-resursen som `email_send` använder.

Varje utskick hamnar i tidslinjen (`email_send`, `email_forward`, `email_reply`; går inte att ångra) och i
auditloggen. Projektets utkorgspolicy gäller: en mottagare utanför `allowlist`, eller `outbox: review`,
gör att forward/reply vägras utan att något skickas, med hänvisning till `email_create_draft`. Utskicken
köas inte.

## Upplåsning per session

Beslut (Jimmy 2026-10-26): utskick låses upp per session, inte per mejl.

1. `email_send_unlock_request` skapar en 8-siffrig kod (`secrets`) och skickar den genom kanalerna i
   `memaix.mail_send_unlock.channels`. Verktygets svar innehåller aldrig koden. Servern sparar den bara som
   HMAC med en nyckel per process.
2. Ägaren skriver in koden i chatten. `email_send_unlock(code)` låser upp exakt (användare, Mcp-Session-Id)
   i `ttl_hours`.
3. Fel kod räknas. Vid `max_attempts` bränns koden och det krävs en ny begäran. Högst `max_requests`
   begäranden per `request_window_minutes`.

Nekas alltid:

- tjänsteklienter;
- serverns agentloop, som saknar MCP-session;
- HTTP utan `mcp-session-id`;
- den som inte är ägare i något projekt.

Ett mejl kan alltså inte låsa upp något. Tillståndet ligger i minnet, så en omstart låser alla sessioner.

**Kanaler.** `ntfy`, `webhook` eller `smtp`, där `smtp` är ett eget avsändarkonto med `host`, `port`,
`user`, `password_ref`, `from` och `to`. Kanaltypen `email` från notiserna går **inte**: den skickar från en
projektlåda som modellen kan läsa. En `smtp`-kanal vägras om `to`, `from` eller `user` är en adress som
något mejlverktyg kan läsa, alltså alla acl.yaml-lådor och alla länkade konton för alla användare. Annars
kunde modellen läsa koden själv.

```yaml
memaix:
  mail_send_unlock:
    enabled: true
    ttl_hours: 4
    code_ttl_minutes: 10
    max_attempts: 5
    max_requests: 3
    request_window_minutes: 15
    channels:
      - {type: ntfy, topic: <hemligt-ämne>, server: https://ntfy.sh}
      # - {type: smtp, host: smtp.purelymail.com, port: 465, user: kod@annan.se,
      #    password_ref: env:UNLOCK_SMTP_PASSWORD, from: kod@annan.se, to: jimmy@annan.se}
```

**Samspel med `allow_send`.** `allow_send: false` är fortfarande spärren per projekt. Forward och reply
kräver `allow_send` **och** en upplåst session. `email_send` fungerar som förut, utom i projekt med
`send_unlock: true` i acl.yaml: där kräver även `email_send` från en modell en upplåst session, och
regelåtgärder (`rules`) som skickar vägras. Systemutskick (`_confirmed=True`: bokningsbekräftelser och
utkorgsåtgärder som en människa godkänt) påverkas inte.

## Länka många lådor

Webbformuläret (`POST /app/api/accounts/link-imap`, cookie-auth) fungerar som förut och tar nu även emot
`smtp_host`/`smtp_port`. För många lådor finns ett admin-CLI som läser lösenordet från stdin, aldrig från argv:

```sh
printf '%s\n' "$PW" | docker compose exec -T gateway python -m memaix_gateway.cli link-imap \
  --user jimmy --account info@example.se --host imap.purelymail.com \
  --smtp-port 465 --projects memaix,jimlov [--verify]
```

Kommandot vägrar om `TOKEN_MASTER_KEY` saknas, eftersom raden annars krypteras med en tillfällig nyckel. Det
kräver `--projects`, eftersom nya konton annars inte delas med något projekt, och kontrollerar att användaren
har åtkomst till varje projekt. Lösenordet skrivs aldrig ut, inte heller när `--verify` misslyckas.

## Säkerhet

Allt mejlinnehåll är data (THREAT-MODEL.md). Översikten innehåller bara rubriker och bär en markering om
otrodd data. Inget verktyg läser brödtext som instruktioner, och inget läsverktyg startar ett utskick.
Utskick kräver ägarroll, `allow_send`, en upplåst session och att utkorgspolicyn tillåter det.
