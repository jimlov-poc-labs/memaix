# Exponera Memaix på nätet

Memaix behöver en HTTPS-URL som din AI-klient kan nå — antingen från molnet (claude.ai, ChatGPT)
eller från din enhet (Claude Desktop, lokal klient). Här är alla rimliga alternativ.

---

## Alternativ 1 — Cloudflare Tunnel (rekommenderas)

**Passar:** de flesta. Kräver ett gratis Cloudflare-konto och en domän hanterad av Cloudflare.

- Ingen öppen port på servern — tunneln är **utgående**.
- Auto-TLS, ingen certbot.
- Stabil URL: `mcp.dindomän.se`.
- Nackdel: trafiken passerar Cloudflares nätverk.

**Steg:**
1. Skapa en tunnel i [Cloudflare Zero Trust](https://one.cloudflare.com) → Networks → Tunnels →
   Create tunnel → Cloudflared.
2. Peka tunnel-hostname (`mcp.dindomän.se`) → `http://localhost:80` (eller `http://caddy:80` om
   du kör i Docker-nätverket).
3. Kopiera tunnel-token och ange det vid `make init` (lagras i `cloudflared-secrets/tunnel.token`).
4. **Stäng av "Block AI Bots"** för hostnamnet: Security → Bots → "Do not block (allow crawlers)".
   Bobrs blockeras Anthropics IP-intervall (160.79.x.x).

```
[Cloudflare edge] → [cloudflared daemon i Docker] → [Caddy :80] → [gateway :8080]
```

### 1b — Lokalt hanterad tunnel (rekommenderas för drift)

Token-läget ovan är **fjärrhanterat**: cloudflared läser ingress-reglerna från
Cloudflares API **vid uppstart** — inte live. Konfig-versionerna där har en
`source`: `cloudflare` betyder satt via dashboard/API, `local` betyder satt
från en config-fil. Om senaste versionen saknar hostname-regler (t.ex. en
oavsiktlig `source: local`-version som ersatte dashboard-reglerna) svarar
tunneln **503 på allt** — trots att den rapporterar sig som *healthy*.

Stadigare: låt ingress-reglerna vara en fil i din deploy, som en omstart
aldrig kan nolla:

1. `cloudflared tunnel login` (skapar `cert.pem`)
2. `cloudflared tunnel create memaix` (skapar `<UUID>.json`)
3. Kopiera `cloudflared/config.example.yml` → `cloudflared/config.yml`,
   fyll i UUID + hostname; lägg `<UUID>.json` i samma katalog.
4. `cloudflared tunnel route dns memaix mcp.dindomän.se`
5. `docker compose --profile tunnel-local up -d`

`cloudflared/` är gitignorerad (utom exempel-filen) — config + credentials är
hemligheter.

**Felsökning: tunneln "healthy" men allt svarar 503?** Ingress-reglerna är
tomma. Fjärrhanterad tunnel: kontrollera konfig-versionerna under Zero Trust →
Tunnels → din tunnel → status/konfiguration och återställ hostname-regeln
(eller byt till 1b). Lokalt hanterad: verifiera `ingress:` i config.yml och
starta om containern.

---

## Alternativ 2 — Egen domän + Caddy/nginx (befintlig server)

**Passar:** dig som redan har en server med ett domännamn och kör Caddy eller nginx.

Lägg till ett block i din befintliga Caddyfile:

```caddyfile
mcp.dindomän.se {
    reverse_proxy localhost:80   # peka på Memaix Caddy-container
}
```

Eller proxya direkt mot gateway (hoppa över intern Caddy):

```caddyfile
mcp.dindomän.se {
    reverse_proxy localhost:8080
}
```

TLS sköts av din befintliga Caddy. Sätt `tunnel.provider: none` i `memaix.yaml`.

**nginx + certbot:**
```nginx
server {
    listen 443 ssl;
    server_name mcp.dindomän.se;
    ssl_certificate     /etc/letsencrypt/live/mcp.dindomän.se/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.dindomän.se/privkey.pem;
    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
    }
}
```

```bash
certbot --nginx -d mcp.dindomän.se
```

---

## Alternativ 3 — Underkatalog på befintlig server

**Passar:** du vill inte ha en separat subdomän och har en befintlig webbserver.

Memaix kan köras under ett prefix, t.ex. `https://dindomän.se/memaix/`. Sätt i `memaix.yaml`:

```yaml
server:
  public_url: "https://dindomän.se/memaix"
```

Caddy-block på din befintliga server:

```caddyfile
dindomän.se {
    handle /memaix/* {
        uri strip_prefix /memaix
        reverse_proxy localhost:8080
    }
    # ... resten av din site
}
```

> **Obs:** OAuth-redirect-URI:er och MCP-connector-URL:en måste matcha prefixet exakt.
> Testa med `make doctor` att discovery-endpoints svarar rätt.

---

## Alternativ 4 — Tailscale Funnel

**Passar:** personligt bruk, hög integritet, vill inte exponera mot öppet internet.

[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) exponerar en lokal port via
`*.ts.net`-adressen utan öppna portar. Funkar med claude.ai (som anropar från molnet).

```bash
tailscale funnel 8080   # exponerar localhost:8080 på https://<maskin>.<tailnet>.ts.net
```

Nackdel: URL:en är maskinbunden och ändras om du byter maskin.

---

## Alternativ 5 — ngrok

**Passar:** snabb demo/test, eller om du inte har domän.

```bash
ngrok http 8080
```

Gratis tier ger en slumpmässig URL vid varje start. Betaltier ger stabil subdomän.
Sätt `server.public_url` till ngrok-URL:en i `memaix.yaml`.

---

## Alternativ 6 — Cloudflare Quick Tunnel (test)

Utan domän, utan konto — bara för att testa:

```bash
cloudflared tunnel --url http://localhost:8080
```

Ger en temporär `*.trycloudflare.com`-URL. Försvinner när processen stoppas.
Lägg in URL:en i `memaix.yaml` under `server.public_url` och `auth.issuer`.

---

## Jämförelse

| | Cloudflare Tunnel | Caddy/nginx | Underkatalog | Tailscale | ngrok | Quick tunnel |
|---|---|---|---|---|---|---|
| Öppen port | Nej | Ja | Ja | Nej | Nej | Nej |
| Stabil URL | Ja | Ja | Ja | Ja | Betalt | Nej |
| Eget domännamn | Rekommenderas | Krävs | Valfritt | Nej | Betalt | Nej |
| Auto-TLS | Ja | Ja (Caddy) | Via befintlig | Ja | Ja | Ja |
| Passar | De flesta | Befintlig server | Befintlig server | Personligt | Demo | Test |

---

## Säkerhet oavsett alternativ

- **Stäng av Bot Fight Mode** (Cloudflare): Anthropics IP-intervall klassas annars som AI-crawler.
- **Sätt `Host`-header rätt**: Caddy och nginx ska vidarebefordra `Host`-headern — Memaix
  validerar den mot `public_url` (DNS rebinding-skydd).
- **OAuth-redirect-URI** måste matcha `public_url` exakt (inklusive protokoll, port, eventuellt prefix).
- Se [SECURITY.md](SECURITY.md) för fullständig härdningslista.
