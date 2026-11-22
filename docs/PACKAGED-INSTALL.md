# Paketerad installation (utan källkodsåtkomst)

Käll-repot (`jimlov-poc-labs/memaix`) är och förblir PRIVAT — det är ett
uttryckligt beslut, inte ett missat steg. Den här sidan beskriver den väg en
extern kund (eget team, egen admin, ingen GitHub-åtkomst till repot) ändå kan
gå för att driva sin egen Memaix-instans.

Se `docs/QUICK-INSTALL.md` för den väg som förutsätter repo-åtkomst
(`git clone` + `./install.sh`, bygger lokalt).

## Vad en extern admin faktiskt behöver

Inte hela repot. Dessa filer, ~150 kB totalt, innehåller ingen
applikationskällkod (ingen `gateway/src`, ingen `login-app/*.py`), och de
publiceras i det publika repot
[`jimlov-poc-labs/memaix-installer`](https://github.com/jimlov-poc-labs/memaix-installer):

```
docker-compose.yml
docker-compose.release.yml
install-release.sh
Makefile
.env.example
config/*.example.yaml
caddy/Caddyfile
cloudflared/config.example.yml
scripts/bootstrap.py
scripts/setup_engine.py
scripts/setup_page.py
scripts/setup_web.py        # krävs för `make go-remote` / tunnel-läget
vault-template/
```

Applikationen själv (gatewayen och login-appen) kommer som färdigbyggda,
signerade container-images från GHCR — se nästa avsnitt. Admin bygger
aldrig något lokalt i det här spåret.

**Testat lokalt (2026-11-22, på en skild testmaskin, isolerad mot drift):**
`./install-release.sh --yes` kört rakt av i en katalog som bara innehöll
ovanstående bunt — inget käll-repo på maskinen. `docker compose pull` (bart,
ingen `-f`-flagga, bara `COMPOSE_FILE` i `.env`) löste korrekt till enbart
`ghcr.io/jimlov-poc-labs/memaix` + `memaix-login-app`, och
`docker compose config --images` + `grep -c build:` (0 träffar) bekräftade
att inget `build:`-avsnitt fanns kvar. Images fanns inte på GHCR ännu, så
pull nekades där — förväntat. Med samma images byggda lokalt (samma
Dockerfiles) och taggade som om de kom från GHCR gick `make up-release`
sedan `make doctor` via samma `.env`/Makefile-väg, ingen manuell `-f`,
och blev grön (gateway, login-app, caddy, postgres, hydra). Se avsnittet
"Känt läge" nedan för vad som INTE testades.

## Images: publicerade, signerade, med SBOM

Vid varje publicerad release bygger `.github/workflows/publish-images.yml`
två images och publicerar dem till GitHub Container Registry:

- `ghcr.io/jimlov-poc-labs/memaix:<version>` (även `:latest`)
- `ghcr.io/jimlov-poc-labs/memaix-login-app:<version>` (även `:latest`)

Repot som bygger dem är privat, men **image-synligheten i GHCR sätts separat
från repo-synligheten** — paketen görs publika oavsett. Varje image:

- är signerad keyless med `cosign` (OIDC via GitHub Actions, ingen hanterad
  nyckel). Verifiera utan någon GitHub-åtkomst:

  ```sh
  cosign verify \
    --certificate-identity-regexp '^https://github.com/jimlov-poc-labs/memaix' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    ghcr.io/jimlov-poc-labs/memaix:<version>
  ```

- har en SPDX-SBOM attesterad mot sin digest (`cosign verify-attestation
  --type spdxjson …`), så sårbarhetsskanning går att göra utan källkoden.

## Installera

```sh
git clone https://github.com/jimlov-poc-labs/memaix-installer.git
cd memaix-installer
MEMAIX_PROFILE=trial ./install-release.sh --yes
```

`install-release.sh` kör ALDRIG `docker compose build`. Den hämtar
(`docker compose pull`) och startar de publicerade images via
`docker-compose.release.yml` — en override som nollar `build:` (kräver
Docker Compose ≥ v2.24 för `!reset`) och pekar `image:` på GHCR. Samma
`docker-compose.yml` fungerar oförändrat för utvecklare som bygger lokalt
(`make up`, ingen override).

Lås en version: sätt `MEMAIX_IMAGE_TAG=v1.24.0` i `.env` innan `make
up-release`.

## Var bunten publiceras — beslutat 2026-11-22

Releaser (och deras bifogade tillgångar, t.ex. SBOM) på ett privat repo ärver
repots synlighet — de blir INTE publika bara genom att publiceras. GitHub
erbjuder ingen inställning för "gör den här releasen publik men repot
privat". Bunten (`docker-compose.yml`, `docker-compose.release.yml`,
`install-release.sh` och resten, aldrig applikationskod) ligger därför i ett
eget, litet, publikt repo: **`jimlov-poc-labs/memaix-installer`**.

```sh
git clone https://github.com/jimlov-poc-labs/memaix-installer.git
```

Det repot hålls i synk manuellt för nu (inget CI-flöde kopierar över filer
automatiskt vid release ännu) — flagga som uppföljning om bunten börjar
driva isär från käll-repots `docker-compose.yml`/`install-release.sh`.

## Källkoden är inte hemlig — Jimmys beslut 2026-11-22

Repot-privat skyddar historik, interna diskussioner och ej släppt arbete —
INTE den körbara koden, och det är avsiktligt, inte en lucka att täppa till.
`pip install .` i gateway-imagen lägger `.py`-filerna i klartext i
`site-packages`; login-app-imagen kopierar `app.py` m.fl. oförändrade. Den
som drar en publicerad image kan läsa källan i den.

Memaix är AGPL-3.0-or-later (`LICENSE`). AGPL §13 kräver att källkoden för
den körande versionen erbjuds till varje användare som interagerar med
instansen över ett nätverk, oavsett om imagen "försvåras". Att minifiera
eller dölja koden i imagen löser alltså ingen juridisk skyldighet och
kostar bara underhåll — därför görs det inte.

## Känt läge / inte testat

- GHCR-pull och `cosign verify`/`verify-attestation` mot en VERKLIGT
  publicerad image är otestat tills den första riktiga releasen kör
  `publish-images.yml`. Verifieringen ovan byggde images lokalt med samma
  Dockerfiles och simulerade publicerings-taggen.
- Multi-arch (arm64) byggs inte — bara `linux/amd64`. Flaggat, inte beslutat.
- Keyless cosign-signering skriver repots namn, workflowets sökväg och ref
  till den publika Rekor-transparensloggen (Sigstore). Det avslöjar inte
  källkod, men avslöjar att detta privata repo finns och när det releasar.
- Att sätta GHCR-paketen till publika är ett manuellt, engångs UI-steg i
  GitHub (orgbehörighet krävs) — görs inte av workflowet.
