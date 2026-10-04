# Installation (självhostat)

> **Tre vägar — börja smått.** Tier 0: `make trial` (lokalt stdio-MCP, inget tunnel/OAuth/domän —
> utvärdera på minuter). Tier 1: full self-host (mobil/team) nedan. Tier 2: managed (du installerar
> åt kund). Detaljer & förenklingsstrategi: [SETUP-SIMPLIFICATION.md](SETUP-SIMPLIFICATION.md).

## Krav
- En server du kontrollerar (egen maskin eller egen molnhosting).
- Docker + Docker Compose.
- En domän du styr över (för OAuth-issuer och connector-URL).
- Ett sätt att exponera endpointen publikt: Cloudflare-tunnel (rekommenderas) eller egen
  reverse proxy med TLS.
- Backends: en IMAP/SMTP-brevlåda, och CalDAV/WebDAV (t.ex. Nextcloud) om du vill ha
  kalender och filer.

## Snabbaste vägen — automatisk installation

```bash
git clone <ditt-memaix-repo> memaix && cd memaix
cp config/brand.example.yaml  config/brand.yaml
cp config/memaix.example.yaml config/memaix.yaml
cp config/acl.example.yaml    config/acl.yaml      # fyll i projekt + användare
make install                                       # eller: python3 scripts/bootstrap.py --tunnel
```

`scripts/bootstrap.py` gör resten automatiskt:
- genererar saknade hemligheter i `.env` (OAuth-nyckel, NC-admin-lösenord),
- startar containrarna,
- **provisionerar Nextcloud från `acl.yaml`** — skapar en användare per projekt, mintar
  app-lösenord och skriver in dem i `.env`, skapar kalender per projekt,
- seedar minnesvaults från `vault-template/` och git-initierar dem.

Har kunden redan egen Nextcloud/filserver: `make install-no-nextcloud` (hoppar över NC,
provisionerar inget — du pekar `acl.yaml` mot befintliga backends själv).

> Validera occ-/OCS-stegen mot din Nextcloud-version första gången. Det är installations-
> automation, inte en svart låda — allt syns i `scripts/bootstrap.py`.

Återstår manuellt oavsett väg: skapa Cloudflare-tunneln och lägg tunnel-token i
`cloudflared-secrets/tunnel.token` (kan inte automatiseras utan Cloudflare-API-credentials), samt koppla in AI-klienten.

## Manuell väg (steg för steg)

1. **Klona och konfigurera**
   ```bash
   git clone <ditt-memaix-repo> memaix && cd memaix
   cp .env.example .env
   cp config/brand.example.yaml  config/brand.yaml
   cp config/memaix.example.yaml config/memaix.yaml
   cp config/acl.example.yaml    config/acl.yaml
   ```

2. **Fyll i config**
   - `config/memaix.yaml` — din domän (`public_url`), tunnel, vaults_dir.
   - `config/acl.yaml` — användare, grants, projekt och deras backends.
   - `config/brand.yaml` — namn/branding (valfritt, default Memaix).
   - `.env` — OAuth-signeringsnyckel (`openssl rand -hex 32`) och backend-lösenord.

3. **Exponera publikt**
   - **Cloudflare-tunnel:** skapa en tunnel, peka hostname → `http://gateway:8080`, lägg
     tunnel-token i `cloudflared-secrets/tunnel.token`. **Lägg ingen Access framför** och **stäng av Bot Fight
     Mode** för hostnamnet (se SECURITY.md).
   - **Egen reverse proxy:** sätt `tunnel.provider: none` och terminera TLS själv (Caddy/nginx).

4. **Starta**
   ```bash
   docker compose --profile tunnel up -d         # + --profile nextcloud om du vill ha Nextcloud
   ```

5. **Seed-vaults**
   - Kopiera `vault-template/` till `vaults/` och initiera git-repon per projekt.
   - Fyll `shared/` (manual, om-filer, skrivstil) eller låt onboarding-intervjun göra det.

6. **Koppla in din AI** — se [AI-CLIENTS.md](AI-CLIENTS.md).

## Verifiera
- `whoami` via AI:n returnerar rätt användare + grants.
- En extern testanvändare når bara sitt projekt.
- `email_send` är avstängt; inga backend-lösenord syns mot AI:n.

## Deploy utan överraskningar för användarna

- Caddy väntar upp till 20 s på att gateway/Hydra/login-app kommer tillbaka efter en omstart och visar annars en vänlig 503-sida (webbläsare laddar om sig själva, API-klienter får JSON med `Retry-After`).
- Gateway och login-app har healthcheck, så `docker compose --profile hydra up -d --build --wait` är klart först när de svarar.
- Förvarna inloggade användare: `ops/maintenance.sh on "Memaix startar om kl 22:00, nere ca 1 min."` före deployen och `ops/maintenance.sh off` efteråt. Bannern visas i webbappen inom ~1 min (filen `data/maintenance.json`, ingen omstart behövs).
