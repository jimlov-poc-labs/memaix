# Koppla in AI-klienter

Din Memaix connector-URL är `https://mcp.din-domän.se` (det du satte som `public_url` vid
`make init`). Den läggs in en gång på webben — synkar sedan till mobil och desktop.

Memaix använder **OAuth 2.1 med PKCE** för autentisering. Klienten måste stödja
OAuth-autentiserade remote MCP-servrar (inte bara localhost/stdio). Se kolumnen "OAuth" nedan.

---

## Snabb-matris

| Klient | Lägsta plan | OAuth remote MCP | Steg |
|---|---|---|---|
| **Claude** (claude.ai) | Pro ($20/mån) | ✓ | [→](#claude-claudeai) |
| **Claude Desktop** | Pro | ✓ HTTP | [→](#claude-desktop) |
| **Mistral Le Chat** | Free | ✓ | [→](#mistral-le-chat) |
| **Perplexity** | Pro ($20/mån) | ✓ | [→](#perplexity) |
| **ChatGPT** | Plus ($20/mån) | ✓ | [→](#chatgpt-openai) |
| **Cursor** | Hobby (gratis) | ✓ HTTP | [→](#cursor) |
| **VS Code + Copilot** | Copilot ($10/mån) | ✓ HTTP | [→](#vs-code-github-copilot) |
| **Gemini** (app) | Advanced ($20/mån) | Begränsad | [→](#gemini) |
| **Zed** | Gratis | ✓ HTTP | [→](#zed) |

> **OAuth vs HTTP**: "OAuth" = klienten hanterar hela OAuth-flödet (rekommenderat).
> "HTTP" = klienten skickar en statisk Bearer-token du genererar manuellt.

---

## Claude (claude.ai)

**Plan:** Pro, Max, Team eller Enterprise. Free stöder inte custom connectors.

1. Gå till **claude.ai** → klicka på ditt namn uppe till höger → **Settings**.
2. Välj fliken **Connectors** (eller "Integrations" beroende på version).
3. Klicka **Add custom connector**.
4. Klistra in din connector-URL: `https://mcp.din-domän.se`
5. Klicka **Connect** — webbläsaren öppnar en OAuth-login på din Memaix-instans.
6. Logga in med ditt admin-lösenord → klicka Godkänn.
7. Konnektorn visas som **Connected** ✓.

**Synk:** Connectors satta på webben synkar automatiskt till Claude iOS-appen och Claude Desktop.

**Prova:** Skriv "kör whoami i Memaix" i en ny konversation — ska returnera ditt användarnamn
och dina projekt.

**Tips:** Lägg till ett Projects system prompt med Memaix-instruktioner (se `vault-template/shared/assistant-manual.md`).

---

## Claude Desktop

**Plan:** Kräver Claude-prenumeration (Pro eller högre).

Enklast: lägg in connector via claude.ai webben (ovan). Claude Desktop plockar upp den
automatiskt via kontosynken och sköter OAuth-flödet själv.

Claude Desktop stöder också remote HTTP MCP med en statisk Bearer-token, men tänk på tre saker
som gör att en handgjord token ofta nekas:

1. **Audience krävs.** Gatewayn godtar bara JWT med `aud` = `https://mcp.din-domän.se`
   (med eller utan avslutande snedstreck). Klienten måste vara registrerad med den audiencen,
   och token-anropet måste skicka `audience=` — annars saknar token `aud` och nekas.
2. **`sub` måste finnas i acl.yaml.** Token-subjektet mappas till en användare via
   `users.<user>.oauth_subjects`, eller till en begränsad service-klient via `service_clients`
   (se [Service-klienter](#service-klienter-client_credentials) nedan). En token med okänt `sub` nekas.
3. **Tokenen går ut.** Hydras access-token lever en timme som standard; någon "långlivad"
   token finns inte. För maskiner, använd en service-klient som hämtar ny token själv.

**Manuell JSON:** Redigera `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "memaix": {
      "type": "http",
      "url": "https://mcp.din-domän.se",
      "headers": {
        "Authorization": "Bearer <din-token>"
      }
    }
  }
}
```

---

## Mistral Le Chat

**Plan:** Free räcker för att koppla in och läsa. Pro ($15/mån) höjer rate limits markant.

1. Gå till **chat.mistral.ai** → Settings → **Connectors** (eller "MCP Servers").
2. Klicka **Add connector** → välj "Custom MCP".
3. Ange URL: `https://mcp.din-domän.se`
4. Klicka **Connect** → OAuth-flöde öppnas → logga in på din Memaix.
5. Klart.

**Not:** Mistral är generellt snäll mot MCP-servrar — bra för att testa att konnektorn fungerar
utan att lägga pengar på ett Pro-konto.

---

## Perplexity

**Plan:** Pro ($20/mån) eller Enterprise.

1. Gå till **perplexity.ai** → inställningar → **AI Tools** eller **Connected services**.
2. Lägg till MCP-server → ange `https://mcp.din-domän.se`.
3. Följ OAuth-flödet.

> **Obs:** Perplexitys MCP-stöd är fokuserat på sökning/research-mode. Verktyg som `memory_write`
> och `backlog_add` fungerar men kan kräva att du ber Perplexity explicit använda dem.

---

## ChatGPT (OpenAI)

**Plan:** Plus ($20/mån) för personligt bruk; Team/Enterprise för organisation.

1. Gå till **chatgpt.com** → klicka på ditt namn → **Settings** → **Connectors** (eller "Tools").
2. Klicka **Add** → välj **Custom MCP server**.
3. Ange: `https://mcp.din-domän.se`
4. Välj autentiseringsmetod: **OAuth** (om tillgängligt) eller **API key** (Bearer-token).
5. Följ instruktionerna → logga in via OAuth-flödet på din Memaix.

> **Not:** ChatGPT kräver att servern stöder OAuth 2.0 med PKCE — vilket Memaix gör.
> UI:t för MCP-connectors varierar beroende på din plan och region.

---

## Cursor

**Plan:** Hobby (gratis) inkluderar MCP-stöd. Pro ($20/mån) för fler AI-tokens.

Cursor stöder HTTP MCP via config-filen — OAuth-flöde hanteras inte automatiskt, använd Bearer-token.

**Generera token:** Logga in på din Memaix via webbläsaren (`https://mcp.din-domän.se/oauth2/auth...`)
och kopiera access-token från OAuth-svaret, eller extrahera den ur claude.ai om du redan kopplade
dit.

Redigera `.cursor/mcp.json` (global) eller `.mcp.json` (projektspecifik):

```json
{
  "mcpServers": {
    "memaix": {
      "url": "https://mcp.din-domän.se",
      "headers": {
        "Authorization": "Bearer <din-access-token>"
      }
    }
  }
}
```

Eller via Cursor UI: **Settings → MCP → Add server** → HTTP → klistra in URL och token.

---

## VS Code + GitHub Copilot

**Plan:** GitHub Copilot Individual ($10/mån) eller Business ($19/user/mån).

MCP-stöd i VS Code kräver Copilot-tillägg v1.250+ (maj 2025+).

1. Öppna Command Palette (`Cmd+Shift+P`) → **GitHub Copilot: Add MCP Server**.
2. Välj **HTTP** → ange URL: `https://mcp.din-domän.se`
3. Välj autentisering: **Bearer token** → klistra in din Memaix-token.
4. Spara — servern visas under **Copilot Chat → Tools**.

Alternativt, redigera `.vscode/mcp.json`:

```json
{
  "servers": {
    "memaix": {
      "type": "http",
      "url": "https://mcp.din-domän.se",
      "headers": {
        "Authorization": "Bearer <din-access-token>"
      }
    }
  }
}
```

---

## Gemini

**Plan:** Google One AI Premium ($20/mån) för Gemini Advanced; Gemini Enterprise för organisation.

Gemini-appen har begränsat MCP-stöd för tredjepartsservrar. Det tillförlitligaste sättet:

**Via Gemini CLI** (gratis, open source):
```bash
npm install -g @google/gemini-cli
gemini mcp add memaix https://mcp.din-domän.se --auth bearer --token <din-token>
gemini chat
```

**Via Google AI Studio** (aistudio.google.com):
Experimentellt stöd för MCP-servrar under Tools. Flödet liknar ChatGPT ovan.

> Gemini-appens inbyggda MCP-stöd för OAuth remote-connectors är under aktiv utveckling (2026).
> Kolla Googles release notes för senaste status.

---

## Zed

**Plan:** Gratis (open source-editor).

Zed har inbyggt MCP-stöd via `settings.json`:

```json
{
  "context_servers": {
    "memaix": {
      "command": {
        "path": "npx",
        "args": ["-y", "mcp-remote", "https://mcp.din-domän.se"]
      }
    }
  }
}
```

`mcp-remote` (npm-paket) hanterar OAuth-flödet och token-caching lokalt. Första gången öppnas
en webbläsare för OAuth-login.

---

## Service-klienter (client_credentials)

För automation utan människa vid tangentbordet (n8n, cron-jobb, skript). En service-klient är en
Hydra-klient med grant `client_credentials`. Den **agerar som en användare** (så att den ser den
användarens länkade konton, t.ex. Gmail) men får **bara** anropa de verktyg och projekt som står
i dess allowlist.

**1. Skapa klienten via Hydras admin-API** på Memaix-värden (inte via publik DCR):

```bash
docker exec memaix-hydra-1 hydra create oauth2-client \
  --endpoint http://localhost:4445 \
  --name n8n-kvitton \
  --grant-type client_credentials \
  --audience https://mcp.din-domän.se --audience https://mcp.din-domän.se/ \
  --token-endpoint-auth-method client_secret_basic \
  --format json
```

Behåll Hydras genererade UUID som `client_id` (det blir tokenens `sub`). Hemligheten visas en
gång: lägg den direkt i klientens credential store, aldrig i acl.yaml eller i git.

> Innan en långlivad klienthemlighet skapas: kontrollera att Hydra inte körs med `--dev` och
> `LOG_LEAK_SENSITIVE_VALUES: "true"` (docker-compose.yml), annars kan hemligheter och tokens
> hamna i loggen.

**2. Lägg in UUID:t i `config/acl.yaml`:**

```yaml
service_clients:
  <hydra-client-uuid>:
    acts_as: jimmy            # måste finnas under users:
    tools:                    # bara dessa verktyg; saknas listan får klienten ingenting
      - email_search
      - email_read
      - email_attachments
      - email_attachment_get
      - email_export_pdf
    projects: [jimlov]        # bara dessa projekt (måste finnas under projects:)
```

**3. Starta om gatewayn.** acl.yaml läses vid start. En felaktig `service_clients`-sektion
stoppar starten med ett valideringsfel, till exempel om `acts_as` saknas eller inte är en
användare, om ett projekt inte finns, om en okänd nyckel står med, eller om samma UUID också
står som `oauth_sub`/`oauth_subjects` hos en användare (ett subjekt är antingen en inloggning
eller en service-klient, aldrig båda).

**4. Hämta token och anropa MCP:**

```bash
curl -s -u "$CLIENT_ID:$CLIENT_SECRET" https://mcp.din-domän.se/oauth2/token \
  -d grant_type=client_credentials -d audience=https://mcp.din-domän.se
```

Anropa sedan `https://mcp.din-domän.se/` (streamable HTTP) med `Authorization: Bearer <jwt>` och
`Accept: application/json, text/event-stream`: först `initialize`, sedan `tools/list` och
`tools/call` med samma `Mcp-Session-Id`. I n8n: OAuth2-credential med grant *Client Credentials*,
Token URL `https://mcp.din-domän.se/oauth2/token`, Basic auth och body-parametern
`audience=https://mcp.din-domän.se`.

**Vad begränsningen gör:**

- `tools/list` visar bara verktygen i `tools`. Resurser och prompts visas inte och kan inte läsas.
- Ett verktyg utanför listan, eller ett anrop med ett `project` utanför `projects` (eller utan
  `project`), nekas med ett tydligt fel: `service client <uuid> may not call tool 'email_send'`.
  Nekandet loggas i audit-loggen med klientens UUID.
- Ett verktyg som inte tar något `project` (t.ex. `whoami`) tillåts bara om det står i `tools`.
  `whoami` är bra för ett första röktest, men visar `acts_as`-användarens alla projekt.
- Behörigheten per länkat konto (`account_scope_set` / scopes per capability) gäller ovanpå:
  klienten når bara ett Gmail-konto i ett projekt som kontot delats med.
- Lyckade anrop loggas som `acts_as`-användaren och delar dennes rate limit.
- Kontrollen sker i MCP-dispatchen (`_MemaixMCP` i `server.py`), så den täcker alla verktyg,
  även de som inte går via `_tool_call`.

Återkalla: ta bort posten ur `service_clients` och starta om, och radera klienten i Hydra
(`hydra delete oauth2-client <uuid> --endpoint http://localhost:4445`).

---

## Felsökning

**"Authorization failed" / "ofid_..."**
Vanligaste orsaker och fix → se [SECURITY.md](SECURITY.md) och [EXPOSE.md](EXPOSE.md).

**Klienten stöder inte OAuth**
Generera en token manuellt och använd Bearer-header (se Claude Desktop / Cursor ovan). Tokenen
måste ha rätt `aud` och ett `sub` som finns i acl.yaml, och den går ut efter en timme.

**Klienten hittar inte verktygen**
Kör `whoami` i klienten för att bekräfta att konnektorn är aktiv. Om det misslyckas: kontrollera
att `public_url` matchar den URL du angett i klienten (inklusive protokoll, utan avslutande slash).

**Rate limit**
Standard: 60 req/min per användare, 120 req/min per projekt. Konfigurerbart i `memaix.yaml`.
