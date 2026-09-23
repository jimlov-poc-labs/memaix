# Memaix — bekvämlighetskommandon

.PHONY: init install install-no-nextcloud trial go-remote up down seed logs doctor docs-check

init:                  ## Front-dörren: ≤3 frågor → genererar all config + hemligheter, seedar demo
	python3 scripts/bootstrap.py --init

install:               ## Automatisk installation + Nextcloud-provisionering + vault-seed
	@command -v python3 >/dev/null || { echo "python3 krävs"; exit 1; }
	python3 scripts/bootstrap.py --tunnel

install-no-nextcloud:  ## Som install men utan medföljande Nextcloud (egen backend)
	python3 scripts/bootstrap.py --tunnel --no-nextcloud

trial:                 ## Tier 0: lokal utvärdering — stdio-MCP, inget tunnel/OAuth/domän
	python3 scripts/bootstrap.py --trial --no-nextcloud

go-remote:             ## Uppgradera en trial till mobil/multi-user (tunnel + Hydra OAuth)
	python3 scripts/bootstrap.py --tunnel

# Profilerna kommer från COMPOSE_PROFILES i .env (skrivs av wizarden). En äldre
# .env utan den raden får hydra-profilen, som bär gatewayen. Den tidigare
# hårdkodade "--profile tunnel --profile nextcloud" startade aldrig gatewayen.
up:                    ## Bygg och starta containrar (profiler enligt .env)
	@if grep -qs '^COMPOSE_PROFILES=' .env; then \
		docker compose up -d --build; \
	else \
		docker compose --profile hydra up -d --build; \
	fi

down:                  ## Stoppa containrar
	@if grep -qs '^COMPOSE_PROFILES=' .env; then \
		docker compose down; \
	else \
		docker compose --profile hydra down; \
	fi

seed:                  ## Bara seed-vaults (om de saknas)
	python3 -c "from scripts.bootstrap import load_acl, seed_vaults; seed_vaults(load_acl())"

logs:                  ## Följ gateway-loggar
	docker compose logs -f gateway

WAIT ?= 0
doctor:                ## Hälsokontroll — verifiera att stacken är grön (WAIT=s väntar in gatewayen)
	python3 scripts/bootstrap.py --doctor --wait $(WAIT)

docs-check:            ## Flagga om något docs/*.md saknas i INDEX.md
	python3 scripts/check-docs-index.py
