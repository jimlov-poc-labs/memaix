# Paketerad installation (utan lokalt bygge)

Repot (`jimlov-poc-labs/memaix`) är publikt. Den här sidan beskriver ett
alternativ till `docs/QUICK-INSTALL.md`: istället för att `install.sh` bygger
gateway- och login-app-imagen lokalt (`docker compose build`), hämtar
`install-release.sh` redan publicerade, signerade images från GHCR. Snabbare,
och du behöver inte ha hela bygg-toolchainen på maskinen — bara Docker.

Klona repot precis som i `QUICK-INSTALL.md`, kör sedan `./install-release.sh`
istället för `./install.sh`:

```sh
git clone https://github.com/jimlov-poc-labs/memaix.git
cd memaix
MEMAIX_PROFILE=trial ./install-release.sh --yes
```

Applikationen själv (gatewayen och login-appen) kommer som färdigbyggda,
signerade container-images från GHCR — se nästa avsnitt. Admin bygger
aldrig något lokalt i det här spåret.

**Testat lokalt (2026-11-22, på en skild testmaskin, isolerad mot drift):**
`./install-release.sh --yes` kört rakt av efter en klon av repot.
`docker compose pull` (bart,
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

Varje image:

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

`install-release.sh` kör ALDRIG `docker compose build`. Den hämtar
(`docker compose pull`) och startar de publicerade images via
`docker-compose.release.yml` — en override som nollar `build:` (kräver
Docker Compose ≥ v2.24 för `!reset`) och pekar `image:` på GHCR. Samma
`docker-compose.yml` fungerar oförändrat för utvecklare som bygger lokalt
(`make up`, ingen override).

Lås en version: sätt `MEMAIX_IMAGE_TAG=v1.24.0` i `.env` innan `make
up-release`.

## Källkoden i imagen

Memaix är AGPL-3.0-or-later (`LICENSE`), och repot är publikt — så det här är
mest en teknisk notering, inte en sekretessfråga: `pip install .` i
gateway-imagen lägger `.py`-filerna i klartext i `site-packages`;
login-app-imagen kopierar `app.py` m.fl. oförändrade. Den som drar en
publicerad image kan läsa källan i den precis som i repot.

## Känt läge / inte testat

- GHCR-pull och `cosign verify`/`verify-attestation` mot en VERKLIGT
  publicerad image är otestat tills den första riktiga releasen kör
  `publish-images.yml`. Verifieringen ovan byggde images lokalt med samma
  Dockerfiles och simulerade publicerings-taggen.
- Multi-arch (arm64) byggs inte — bara `linux/amd64`. Flaggat, inte beslutat.
- Keyless cosign-signering skriver repots namn, workflowets sökväg och ref
  till den publika Rekor-transparensloggen (Sigstore) — ingen sekretessfråga
  eftersom repot redan är publikt, bara nämnt för fullständighetens skull.
- Att sätta GHCR-paketen till publika är ett manuellt, engångs UI-steg i
  GitHub (orgbehörighet krävs) — görs inte av workflowet.
