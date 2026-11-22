#!/usr/bin/env sh
# Memaix paketerad installer — kör mot en PUBLICERAD GHCR-image, utan att
# någonsin klona käll-repot (som är privat).
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Inspektera gärna detta skript innan du kör det.
#
# Skillnad mot install.sh: install.sh klonar källkoden och bygger imagen
# lokalt (docker compose build). Det kräver läsrätt till memaix-repot, som är
# privat. Det här skriptet bygger ALDRIG något lokalt — det hämtar bara de få
# driftfiler som behövs (compose-filer, .env-mall, Caddyfile, config-mallar,
# bootstrap.py) från den publika installer-bunten, och startar sedan de
# publicerade images från ghcr.io/jimlov-poc-labs/memaix och
# memaix-login-app.
#
# Användning:
#   git clone https://github.com/jimlov-poc-labs/memaix-installer.git
#   cd memaix-installer
#   ./install-release.sh --yes
#
# Miljövariabler:
#   MEMAIX_DIR          katalog att installera i (default: memaix-release)
#   MEMAIX_IMAGE_TAG     vilken publicerad version (default: latest)
#   MEMAIX_PROFILE, MEMAIX_DOMAIN: se docs/PACKAGED-INSTALL.md
#
# Bunten (denna fil + compose-filer + config-mallar + bootstrap.py, ingen
# applikationskod) publiceras i jimlov-poc-labs/memaix-installer (publikt
# repo, beslutat 2026-11-22) — eftersom GitHub-releaser på det privata
# käll-repot ärver samma synlighet och aldrig blir publika. Se
# docs/PACKAGED-INSTALL.md i käll-repot för helheten.

set -eu

DIR="${MEMAIX_DIR:-memaix-release}"
UNATTENDED=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) UNATTENDED=1 ;;
    *) echo "✗ Okänt argument: $arg (giltigt: --yes)"; exit 2 ;;
  esac
done

echo "▸ Memaix paketerad installer (publicerad image, ingen källkod)"

# 1. Förkontroll.
if ! command -v docker >/dev/null 2>&1; then
  echo "✗ Docker krävs men saknas. Installera Docker och kör igen:"
  echo "  https://docs.docker.com/get-docker/"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "✗ Docker Compose v2 krävs (ingår i moderna Docker-versioner)."
  exit 1
fi
# docker-compose.release.yml kräver !reset-stöd (Compose Specification ≥
# 2.24). Fail closed: ett tomt eller otolkbart versionssvar nekas, det
# tillåts aldrig tyst (ett "0" hade annars passerat varje jämförelse nedan).
COMPOSE_VER="$(docker compose version --short 2>/dev/null)"
COMPOSE_VER="${COMPOSE_VER#v}"
MAJOR="${COMPOSE_VER%%.*}"
REST="${COMPOSE_VER#*.}"
MINOR="${REST%%.*}"
case "$MAJOR" in ''|*[!0-9]*) MAJOR="" ;; esac
case "$MINOR" in ''|*[!0-9]*) MINOR="" ;; esac
if [ -z "$MAJOR" ] || [ -z "$MINOR" ]; then
  echo "✗ Kunde inte läsa Docker Compose-versionen (fick \"$COMPOSE_VER\"). Kräver ≥ v2.24."
  exit 1
fi
if [ "$MAJOR" -lt 2 ] || { [ "$MAJOR" -eq 2 ] && [ "$MINOR" -lt 24 ]; }; then
  echo "✗ Docker Compose $COMPOSE_VER är för gammal (kräver ≥ v2.24 för docker-compose.release.yml)."
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 krävs för setup-guiden och hälsokontrollen."
  exit 1
fi
if ! command -v make >/dev/null 2>&1; then
  echo "✗ make krävs (t.ex. apt install make)."
  exit 1
fi

# 2. Hitta bunten. Körs skriptet inifrån en redan uppackad bunt (samma
#    katalog som docker-compose.yml) används den som den är.
is_bundle() {
  [ -f "$1/docker-compose.yml" ] && [ -f "$1/docker-compose.release.yml" ] && [ -f "$1/scripts/bootstrap.py" ]
}
SELF_DIR=""
case "$0" in
  */*) SELF_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)" ;;
esac
if [ -n "$SELF_DIR" ] && is_bundle "$SELF_DIR"; then
  cd "$SELF_DIR"
elif is_bundle "."; then
  :
elif is_bundle "$DIR"; then
  cd "$DIR"
else
  echo "✗ Hittar inte installer-bunten (docker-compose.yml + docker-compose.release.yml + scripts/bootstrap.py)."
  echo "  Packa upp bunten själv och kör skriptet inifrån den katalogen, eller sätt"
  echo "  MEMAIX_DIR till en redan uppackad bunt. Se docs/PACKAGED-INSTALL.md."
  exit 1
fi
echo "▸ Installerar i $(pwd)"

export MEMAIX_IMAGE_TAG="${MEMAIX_IMAGE_TAG:-latest}"

# 3. Setup — samma guide som install.sh, bygger bara config + hemligheter,
#    aldrig applikationen (den kommer redan byggd från ghcr.io).
if [ -f .env ] && [ -f config/memaix.yaml ] && [ -f config/acl.yaml ]; then
  echo "▸ Config finns redan — hoppar över wizarden (flytta undan .env och config/ för att börja om)."
elif [ "$UNATTENDED" = "1" ]; then
  echo "▸ Oövervakad setup (profil: ${MEMAIX_PROFILE:-trial})"
  python3 scripts/bootstrap.py --init --yes
else
  python3 scripts/bootstrap.py --init
fi

# Se till att MEMAIX_IMAGE_TAG finns i .env så att framtida `make up-release`
# (utan att sätta variabeln för hand) låser samma version.
if ! grep -qs '^MEMAIX_IMAGE_TAG=' .env 2>/dev/null; then
  echo "MEMAIX_IMAGE_TAG=${MEMAIX_IMAGE_TAG}" >> .env
fi

# COMPOSE_FILE i .env (läses av Compose CLI:t självt, oavsett vilket
# kommando som körs sen) — INTE bara i den här processens miljö. Annars
# diskar varje annat bart `docker compose`-anrop (bootstrap.py, `make logs`,
# en framtida `make up` av misstag) override:n och försöker bygga lokalt
# igen (build: finns inte i bunten) eller dra `image:` från basfilen.
if ! grep -qs '^COMPOSE_FILE=' .env 2>/dev/null; then
  echo "COMPOSE_FILE=docker-compose.yml:docker-compose.release.yml" >> .env
fi

# 4. Hämta de publicerade images, kör, verifiera. Aldrig --build.
docker compose pull
make up-release
make doctor WAIT=180

echo "✓ Klart. Se utskriften ovan för hur du kopplar in din AI."
