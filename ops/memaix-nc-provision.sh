#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Forced command for the gateway's provisioning ssh key on the Nextcloud host.
# authorized_keys line:
#   restrict,command="/usr/local/bin/memaix-nc-provision" ssh-ed25519 AAAA... memaix-gateway-provision
#
# Accepts exactly: user-add <memaix-name> <quota> <display name...>
# Password: first line of stdin (never argv). Exit 0 created, 10 already exists.
# Quota is capped at 20GB so a stolen key cannot fill the disk with one account.
set -euf
export LC_ALL=C

CONTAINER="${NC_CONTAINER:-nextcloud-app-1}"
set -- ${SSH_ORIGINAL_COMMAND:-}
[ "${1:-}" = "user-add" ] || { echo "usage: user-add <user> <quota> <display>" >&2; exit 2; }
user="${2:-}"; quota="${3:-}"; shift 3 2>/dev/null || exit 2
display="$*"

case "$user" in
  memaix-[a-z0-9]*) ;;
  *) echo "user must be memaix-<project>" >&2; exit 2 ;;
esac
printf '%s' "$user" | grep -Eq '^memaix-[a-z0-9][a-z0-9_-]{1,31}$' || { echo "bad user" >&2; exit 2; }
printf '%s' "$quota" | grep -Eq '^(([1-9]|1[0-9]|20) ?GB|[1-9][0-9]{0,3} ?MB)$' || { echo "bad quota (max 20GB)" >&2; exit 2; }
printf '%s' "$display" | grep -Eq '^[A-Za-z0-9 _-]{1,64}$' || { echo "bad display name" >&2; exit 2; }

IFS= read -r pass
[ "${#pass}" -ge 16 ] || { echo "password too short" >&2; exit 2; }

if docker exec -u www-data "$CONTAINER" php occ user:info "$user" >/dev/null 2>&1; then
  exit 10
fi

printf '%s\n' "$pass" | docker exec -i -u www-data "$CONTAINER" sh -c \
  'IFS= read -r OC_PASS; export OC_PASS; exec php occ user:add --password-from-env --display-name="$1" "$2"' \
  sh "$display" "$user" >/dev/null
if ! docker exec -u www-data "$CONTAINER" php occ user:setting "$user" files quota "$quota" >/dev/null; then
  docker exec -u www-data "$CONTAINER" php occ user:delete "$user" >/dev/null 2>&1 || true
  echo "quota step failed; account removed" >&2
  exit 3
fi
