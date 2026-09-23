#!/usr/bin/env sh
# Memaix auto-installer.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Inspektera gärna detta skript innan du kör det — det är öppen källkod och gör inget dolt.
#
# Användning i dag (repot är privat, så det finns ingen publik installationsadress än):
#   git clone https://github.com/jimlov-poc-labs/memaix.git
#   cd memaix
#   ./install.sh
#
# Oövervakad (leverantör/headless/CI), från en klonad katalog:
#   MEMAIX_PROFILE=trial ./install.sh --yes
#
# Den publika adressen för `curl … | sh` fastställs när repot publiceras. Tills
# dess finns ingen sådan URL. Skriptet fungerar ändå fristående: körs det utanför
# en klon hämtar det koden från MEMAIX_REPO (kräver läsrätt till repot).
#
# Miljövariabler:
#   MEMAIX_REPO      git-URL att klona från (default: jimlov-poc-labs/memaix)
#   MEMAIX_DIR       katalog att klona till när skriptet inte körs inifrån en klon
#   MEMAIX_PROFILE   trial | selfhost | managed  (endast med --yes; alias: solo, team)
#   MEMAIX_DOMAIN    krävs för selfhost/managed med --yes
#   Fler (admin-användare, lösenord, projekt): docs/QUICK-INSTALL.md

set -eu

REPO="${MEMAIX_REPO:-https://github.com/jimlov-poc-labs/memaix.git}"
DIR="${MEMAIX_DIR:-memaix}"
UNATTENDED=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) UNATTENDED=1 ;;
    *) echo "✗ Okänt argument: $arg (giltigt: --yes)"; exit 2 ;;
  esac
done

echo "▸ Memaix-installer"

# 1. Förkontroll: Docker + Compose v2 + python3 (wizard och hälsokontroll).
if ! command -v docker >/dev/null 2>&1; then
  echo "✗ Docker krävs men saknas. Installera Docker och kör igen:"
  echo "  https://docs.docker.com/get-docker/"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "✗ Docker Compose v2 krävs (ingår i moderna Docker-versioner)."
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 krävs för wizarden och hälsokontrollen."
  exit 1
fi
if ! command -v make >/dev/null 2>&1; then
  echo "✗ make krävs (t.ex. apt install make)."
  exit 1
fi

# 2. Hitta koden. Körs skriptet inifrån en klon (./install.sh) används den och
#    ingen andra klon görs. Annars: befintlig $DIR, sist en ny grund klon.
is_checkout() {
  [ -f "$1/docker-compose.yml" ] && [ -f "$1/scripts/bootstrap.py" ]
}
SELF_DIR=""
case "$0" in
  */*) SELF_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)" ;;
esac
if [ -n "$SELF_DIR" ] && is_checkout "$SELF_DIR"; then
  cd "$SELF_DIR"
elif is_checkout "."; then
  :
elif is_checkout "$DIR"; then
  cd "$DIR"
else
  if [ -e "$DIR" ]; then
    echo "✗ $DIR finns men är ingen Memaix-klon. Välj en annan MEMAIX_DIR."
    exit 1
  fi
  command -v git >/dev/null 2>&1 || { echo "✗ git krävs för att hämta koden."; exit 1; }
  echo "▸ Hämtar Memaix → $DIR"
  git clone --depth 1 "$REPO" "$DIR"
  cd "$DIR"
fi
echo "▸ Installerar i $(pwd)"

# 3. Setup — wizarden genererar all config + hemligheter.
#    Redan konfigurerad? Hoppa över: en ny körning skulle rotera Hydras
#    databaslösenord medan postgres-volymen behåller det gamla.
if [ -f .env ] && [ -f config/memaix.yaml ] && [ -f config/acl.yaml ]; then
  echo "▸ Config finns redan — hoppar över wizarden (flytta undan .env och config/ för att börja om)."
elif [ "$UNATTENDED" = "1" ]; then
  echo "▸ Oövervakad setup (profil: ${MEMAIX_PROFILE:-trial})"
  python3 scripts/bootstrap.py --init --yes
else
  python3 scripts/bootstrap.py --init
fi

# 4. Kör + verifiera.
make up
make doctor WAIT=180

echo "✓ Klart. Se utskriften ovan för hur du kopplar in din AI."
