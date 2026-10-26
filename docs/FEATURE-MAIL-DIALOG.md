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
| `email_reply(item, body, reply_all=False, quote=True, confirm_reply_to=False, ...)` | owner + upplåst | `In-Reply-To`/`References`, `Re:`, svarar Reply-To (annars From); svara-alla lägger till To/Cc utom den egna adressen. Se "Reply-To och svara-alla" nedan. |
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

**Vad en "session" är.** Över HTTP: (användare, OAuth-klientens `client_id`, `Mcp-Session-Id`). mcp-SDK:n
vägrar sedan 1.27.2 ett session-id från en annan credential än den som skapade sessionen (därav golvet
`mcp>=1.27.2` i `pyproject.toml`). Memaix binder dessutom upplåsningen till `client_id` själv, så att den
håller även om SDK-kontrollen skulle försvinna. **Över stdio** är hela processen en session
(`stdio:{pid}`): alla chattar som delar samma stdio-process delar också upplåsningen. Kör en process per
chatt om det spelar roll.

**Kanaler.** Grundregeln: koden får inte hamna någonstans där modellen kan läsa den, och inget verktyg
får visa var den skickas. Felmeddelanden, loggar och audit nämner bara kanaltyp, aldrig URL, ämne eller token.

- `discord` (primär, Jimmys val 2026-10-26): en Discord-webhook, helst som `url_ref`. Koden ligger i
  `content` så att den syns i push-notisen. Måste vara `https://discord.com/api/webhooks/<id>/<token>`
  (eller discordapp.com, ptb., canary.). **Ingen query-sträng**: `?wait=true` får Discord att returnera
  meddelandet med id, och med URL + id går meddelandet att läsa tillbaka. Svaret läses aldrig.
- `ntfy`: ämnet (`topic` eller hellre `topic_ref`) måste vara minst 24 slumptecken (`A-Z a-z 0-9 _ -`),
  eftersom vem som helst som kan ämnet kan läsa det. `token_ref` skickas som `Authorization: Bearer`
  (för skyddade ämnen på egen server eller ntfy.sh-konto).
- `webhook`: generisk JSON-post `{subject, text}`, samma krav som discord på https och ingen query-sträng.
- `smtp`: ett eget avsändarkonto med `host`, `port`, `user`, `password_ref`, `from` och `to`.

Vägras:

- Kanaltypen `email` från notiserna: den skickar från en projektlåda som modellen kan läsa.
- En `ntfy`-/`discord`-/`webhook`-kanal med samma ämne eller webhook som **någon användares brief-kanal**.
  Brief-kanalerna sätter modellen själv via `brief_configure`, så de är kända för den. Jämförelsen
  normaliserar (ntfy.sh som standardserver, värdnamnets skiftläge, Discords värdalias via webhook-id).
- En `smtp`-kanal där `to`, `from` eller `user` hamnar i en låda som något mejlverktyg kan läsa (alla
  acl.yaml-lådor och alla länkade konton för alla användare). Adresser normaliseras först: skiftläge,
  `+tagg`, och för Gmail punkter och `googlemail.com`. Dessutom vägras **hela domänen** om en läsbar låda
  ligger där (catch-all och alias på egna domäner, Outlook, iCloud och Proton går inte att härleda), utom
  `gmail.com`/`googlemail.com` där normaliseringen täcker alla alias.

En vägrad begäran räknas mot `max_requests`, så att modellen inte kan pröva sig fram till kodkanalen genom
att ändra sin brief-kanal och se vilken begäran som vägras.

Brief-kanaler (som användaren och modellen sätter via `brief_configure` eller webben) får inte innehålla
några `*_ref`-fält: då kunde modellen låta servern slå upp en hemlighet och skicka den till en adress den
själv valt. Hemlighetsreferenser hör bara hemma i operatörens konfiguration.

`brief_status` och `brief_configure` maskerar ntfy-ämnen (`ab***`), webhook-URL:er (`https://värd/***`) och
`*_ref`. Brief-leveransens fel rapporteras bara som kanal- och feltyp.

I `memaix.yaml` (filens innehåll hamnar under `memaix.` i konfigurationen):

```yaml
mail_send_unlock:
  enabled: true
  ttl_hours: 4
  code_ttl_minutes: 10
  max_attempts: 5
  max_requests: 3
  request_window_minutes: 15
  channels:
    - {type: discord, url_ref: "env:UNLOCK_DISCORD_WEBHOOK"}
    # - {type: ntfy, topic_ref: "env:UNLOCK_NTFY_TOPIC", token_ref: "env:UNLOCK_NTFY_TOKEN"}
    # - {type: smtp, host: smtp.purelymail.com, port: 465, user: kod@annan.se,
    #    password_ref: env:UNLOCK_SMTP_PASSWORD, from: kod@annan.se, to: jimmy@annan.se}
mail_dialog:
  reply_all_max_cc: 20
```

**När upplåsningen är påslagen gäller den alla utgående vägar som en modell eller ett läst mejl kan
styra**, oavsett projektets flagga (granskning 2026-10-26: annars bestämmer det svagaste projektet):

- `email_send` från en modell kräver en upplåst session i **alla** projekt;
- `outbox_approve` (MCP-verktyget) av en köad `email_send`/`email_forward`/`email_reply` kräver en upplåst
  session. Kontrollen görs före godkännandet, så en vägrad åtgärd ligger kvar som `pending`;
- regelåtgärder (`rules`) som skickar mejl vägras, eftersom de saknar session. Det gäller även
  `notify`: dess e-postkanaler hoppas över;
- `notify` från en regel vägras helt om regeln inte utlöses av `schedule` eller `internal`. Det gäller
  `trigger.type: mail` (läst mejl) och `trigger.type: webhook`: en webhook bär det anroparen postade, och
  om n8n vidarebefordrar mejl är det samma auto-kedja. Okända eller saknade utlösartyper vägras också.
  Vi valde att vägra hellre än att låta ägaren märka vissa kanaler som tillåtna för regler: en sådan
  flagga vore ett fält till som modellen kan sätta via `brief_configure`, och spärren skulle behöva
  vakta det också. Vill du ha notiser från n8n med spärren på, låt n8n posta direkt till kanalen.
  Kan konfigurationen inte läsas räknas spärren som påslagen;
- `brief_configure` får inte lägga till en **ny** mottagare (e-postadress/projekt, webhook, ntfy-ämne) utan
  upplåst session och ägarroll. Att behålla, ta bort, ordna om eller byta format på redan sparade mottagare
  kräver ingen kod. En Discord-webhook känns igen på sitt id (värdalias räknas som samma) men bara över
  `https` på standardport; andra webhooks och ntfy måste stämma exakt i schema, värd, port, sökväg (ämne)
  och query. Identiteten räknas fram med samma URL-parser som `requests` skickar med (urllib3);
- forward och reply kräver som förut upplåst session.

Påverkas inte: godkännande i webbgränssnittet och tavlan, godkännande via elicitation (ett formulär som
människan själv besvarar i klienten), systemutskick (`_confirmed=True`: bokningsbekräftelser och
utkorgsåtgärder som en människa godkänt), brief-inställningar i webbgränssnittet (där sitter människan,
inte modellen) och **utskick till redan sparade brief-mottagare**: den schemalagda briefen, `brief_send_now`
och `notify` från `schedule`/`internal`-regler (utom till e-postkanaler, se ovan). Mottagare som sparades innan spärren slogs på räknas som sparade;
gå igenom dem (`brief_status`) innan du slår på `enabled`.

**Brief-kanaler och RBAC.** En e-postkanal skickar från projektets låda och kräver därför ägarroll i det
projektet (`project` är obligatoriskt), både när den sätts (MCP och webben) och vid varje utskick: en sparad
kanal i ett projekt där användaren inte längre är ägare hoppas över. Briefen tar bara med projekt som
användaren har grant i, oavsett vad `projects` säger. Med `enabled: false` gäller det gamla: `send_unlock: true` i
acl.yaml slår på kravet för `email_send` i just det projektet.

**Samspel med `allow_send`.** `allow_send: false` är fortfarande spärren per projekt. Utskick kräver
ägarroll i **det projekt lådan läses genom** (en upplåsning som ägare i ett projekt ger ingen rätt att
skicka från ett projekt där du är collaborator), `allow_send` **och** en upplåst session.

**Begränsningar att känna till.**

- **Kodkanalen kan vara läsbar via andra kopplingar än Memaix.** Memaix vägrar kanaler som någon av dess
  egna mejlverktyg kan läsa, men ser inte klientens övriga kopplingar. En SMTP-låda (eller en Gmail som
  koden går till) kan vara läsbar för modellen genom t.ex. claude.ai:s Gmail-koppling. Använd därför en
  **Discord-webhook i en kanal som inga botar kan läsa** som kodkanal.
- **Kodkanalens webhook måste vara en annan än brief-webhooken** (annat Discord-webhook-id; samma id med
  annan token eller värdalias räknas som samma webhook och vägras).
- **URL:er som två parsare kan läsa olika vägras överallt** (`safety/net.py`, både när kanalen sparas och
  före varje utskick, även för redan sparade kanaler och iCal-URL:er): bakstreck, blanksteg och
  kontrolltecken, användarinfo (`user@värd`), procentkodad värd, och värd som stdlib och urllib3 tolkar
  olika. Granskning 2026-10-26: `https://attacker.test\@discord.com/...` var discord.com för kontrollen
  men attacker.test för `requests`. En Discord-värd kräver dessutom `https` på standardport, och ett
  ntfy-ämne får bara innehålla A-Z, a-z, 0-9, `_` och `-`.
- **Reply-To-varningen är information, inte en spärr.** Modellen kan själv anropa `email_reply` med
  `confirm_reply_to=True`; den som läser chatten ska se varningen innan det sker.
- **Det finns ingen hård mottagar-allowlist.** En upplåst session kan skicka till vilken adress som helst.
  Projektets valfria `allowlist` (acl.yaml) spärrar inte, utan tvingar bara utskick till andra mottagare
  till granskning i utkorgen.

## Reply-To och svara-alla

- Om `Reply-To` pekar på en annan adress än `From` skickas inget. Svaret har `status: not_sent`, en
  varning och båda adresserna. Ett svar skulle annars gå till någon annan än den som syntes skriva, ett
  vanligt bedrägeriknep. Först när användaren sagt ja anropas `email_reply` igen med
  `confirm_reply_to=True`; då går svaret till Reply-To, och utkorgspolicyn gäller som vanligt.
- `reply_all=True` vägras om fler än `mail_dialog.reply_all_max_cc` (standard 20) kopiemottagare skulle
  få svaret. Inget kortas av i tysthet.

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
