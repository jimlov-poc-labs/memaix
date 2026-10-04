#!/usr/bin/env bash
# Slå på/av underhållsbannern i webbappen (läses av /app/api/me, uppdateras
# hos inloggade användare inom ~1 min, ingen omstart behövs).
#   ops/maintenance.sh on "Memaix startar om kl 22:00 och är nere ca 1 minut."
#   ops/maintenance.sh off
set -euo pipefail
file="${MEMAIX_DATA_DIR:-$(dirname "$0")/../data}/maintenance.json"
case "${1:-}" in
  on)
    [ -n "${2:-}" ] || { echo "ange meddelandetext" >&2; exit 2; }
    python3 -c 'import json,sys; print(json.dumps({"message": sys.argv[1]}))' "$2" > "$file.tmp"
    mv "$file.tmp" "$file"
    echo "banner på: $2" ;;
  off)
    rm -f "$file"
    echo "banner av" ;;
  *) echo "användning: $0 on \"text\" | off" >&2; exit 2 ;;
esac
