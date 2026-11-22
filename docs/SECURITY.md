# Säkerhet & kända fallgropar

## Hårda regler
- **OAuth via ory Hydra, ingen Access framför.** Auth hanteras av **ory Hydra** (certifierad
  OAuth2/DCR), inte hemsnickrad kod — gateway validerar tokens. Lägg **inte** Cloudflare Access
  "Managed OAuth" MCP-portal framför endpointen — dokumenterad bugg där claude.ai **webb + mobil**
  misslyckas med OAuth medan desktop funkar mot samma URL. Tunneln ska vara en ren proxy.
  *(v2 — se `REVIEW-RESPONSE.md`.)*
- **Stäng av Bot Fight Mode / "Block AI training bots"** för MCP-hostnamnet, annars blockerar
  Cloudflare AI-leverantörens anrop.
- **Backend-credentials serverside.** Lösenord (IMAP/SMTP, WebDAV) ligger i `.env`, refereras via
  `*_ref` i acl.yaml, och exponeras aldrig mot AI:n.
- **`allow_send: false` som default.** AI:n skapar utkast; människan skickar.
- **Minst privilegium.** Externa medarbetare = exakt ett projekt. Verifiera isoleringen efter
  varje ACL-ändring.

## Exponeringsyta
- Endast `mcp.<domän>` ska vara publik. Gatewayens port bindas till localhost; tunnel/proxy
  hanterar inkommande.
- Logga verktygsanrop per användare + projekt (`features.audit_log: true`).

## Supply chain (publik repo + många beroenden)
Hydra, Nextcloud, Ollama, Python-libs och base-images = stor attackyta, särskilt med öppen kod.
- **Pinna beroenden** (versioner + hashar), generera **SBOM**, **image-scanning** i CI, och
  **signera releaser**. (OPEN-GAPS #5)

## AI-specifika hot
Prompt injection och exfiltrering (en AI som läser fientligt mejl/innehåll) hanteras separat i
**[THREAT-MODEL.md](THREAT-MODEL.md)** — den största säkerhetsluckan; läs den.

## Granska alltid
Memaix låter en AI agera på riktig data. Granska utkast och åtgärder innan de skickas eller
publiceras. `email_send` och andra skrivåtgärder bör ha mänsklig bekräftelse tills du litar på
flödet.

## Åtgärdat i säkerhetsgranskning (2026-07)

En genomgång av hela kodbasen ledde till följande härdningar (kärnan — den OAuth-skyddade
MCP-verktygsytan, ACL-isoleringen, path/SQL/XML-hanteringen — var redan solid):

- **Board-autentisering fail-closed.** Board-webb-UI:t vägrar nu starta med den inbyggda
  default-signeringshemligheten i HTTP-läge (sätt `HYDRA_SYSTEM_SECRET`), och lösenord binds
  per användare (`MEMAIX_LOGIN_PASSWORD_HASH_<USER>`) så ett delat lösenord inte kan autentisera
  som en *annan* tillåten användare. Den delade `MEMAIX_LOGIN_PASSWORD_HASH` gäller bara när exakt
  en användare är tillåten.
- **Webhook-endpointen.** Regel-tokens jämförs i konstant tid (`hmac.compare_digest`) och
  endpointen är rate-limitad per klient-IP (30/60 s) så token inte kan brute-forcas.
- **Regel-egress.** `notify`-regler kräver nu `owner` (utgående åtgärd) och alla regelutlösta
  åtgärder auditloggas (`rule_action:*`). Understreck-prefixade parametrar strippas i regelvägen så
  en regel aldrig kan sätta `_confirmed=true` och kringgå utkorgen.
- **SSRF-guard** (`safety/net.py`) på användar-angivna URL:er (iCal-hemlighet, webhook-kanaler):
  loopback/link-local/privata/reserverade adresser avvisas, både när URL:en sätts och precis före
  hämtning.
- **Identitet fail-closed i HTTP-läge:** en saknad OAuth-token nekar i stället för att falla tillbaka
  på `MEMAIX_USER`-env.
- **Utkorgen** visar bara köade åtgärder för den som har rollen att godkänna dem (en `reader` kan
  inte längre läsa köade mejltexter).
- **OAuth-callbackens** framgångssida HTML-escapar IdP-claims och läcker inte längre
  exception-detaljer.
- **CI** kör nu `pip-audit` (beroende-sårbarhetsskanning) utöver ruff/mypy/bandit och SBOM.

**Kvar (medvetet, policy- eller deploybeslut):** utkorgens default är fortfarande `auto` — sätt
`outbox: review` per projekt för att kräva godkännande av utgående åtgärder (se
[THREAT-MODEL.md](THREAT-MODEL.md) och [USER-MANUAL.md](USER-MANUAL.md) §5). Kryptering-i-vila av
utkorgens/sökindexets innehåll och HTTPS för intern JWKS-hämtning är deploy-/infrastrukturval.
