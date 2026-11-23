# Granskningsregler

## Det här är farligt här
- RBAC kontrolleras mot `acl.yaml` INNAN ett verktyg körs, aldrig efter
  (`acl.py`/`server.py`). Varje nytt verktyg ska ta `project` och valideras
  mot behörighet före körning — ingen genväg förbi det.
- Allt innehåll som backend läser (mejl, filer, kalender, andras
  minnesnoteringar) är otrodd data, aldrig instruktioner. Systemprompten ska
  märka det som data (se `docs/THREAT-MODEL.md`).
- Inget läsverktyg får direkt initiera ett skriv- eller utgående verktyg
  (ingen auto-kedja).
- Minne = SQLite (aktivt tillstånd) + git asynkront. Aldrig commit-per-skrivning.
- Single-tenant per deployment — ingen delad multi-tenant-dataplan, någonsin.

## Redan beslutat, flagga inte
- Auth = ory Hydra; bygg ingen egen OAuth-server.
- Mejl = BYO-infra, ingen reseller.

## Krav på tester
- Ändrad logik i `auth/` eller `safety/` kräver ett test som fallerar utan
  ändringen.
- RBAC-fasen är inte klar förrän en användare utan grant **bevisat nekas**
  (test, inte påstående) — se `AGENTS.md §2`.

## Känt gap
- Ingen oberoende AI-granskning/Sonar-gate via `kvalitet@v1` i det här repot
  (publikt repo, privat standard) — se KVALITET.md. Granska manuellt tills det
  är löst.
