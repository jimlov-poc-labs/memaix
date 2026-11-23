# Kvalitet

Nivå: A
Täckningsgolv: 0 (ej uppmätt eller satt ännu — ingen `--cov-fail-under` i `gateway/pyproject.toml`.
  Sätts till en mätt nivå när den tas, och höjs sedan men sänks aldrig.)
Kritiska filer: gateway/src/memaix_gateway/auth/, gateway/src/memaix_gateway/safety/, gateway/config-example/acl.yaml
Sabotörtester i CI: nej — krävs av nivå A, saknas ännu
Mutationskontroll: nej — krävs av nivå A (veckovis), saknas ännu
Standard: https://github.com/jimlov-poc-labs/kvalitet/blob/main/STANDARD.md

Nivå A beror på behörighet (RBAC mot `acl.yaml`, se `AGENTS.md`) och persondata
i det delade minnet, inte betalning.

## Oberoende AI-granskning: inte tillgänglig via kvalitet@v1

Detta repo är **publikt**. `jimlov-poc-labs/kvalitet` (där `review@v1` och
`sonar-gate@v1` bor) är **privat**. GitHub tillåter inte ett publikt repos
workflow att läsa ett privat repos composite action utan en PAT (bekräftat,
inte antaget). Att lägga in en PAT i ett publikt repos secrets för att läsa ett
privat repo är en egen avvägning Jimmy inte har tagit ställning till ännu.

Resultat: ingen oberoende AI-granskning eller Sonar-kvalitetsspärr via den
delade standarden i det här repot, trots nivå A. Sonar-skanning i sig körs
redan (`.github/workflows/sonarqube.yml`, egen konfiguration mot
`sonar.jimlov.se`) — det som saknas är den **blockerande** gate-komponenten
och motpartsgranskningen som nivå A annars kräver.

Väntar på Jimmys beslut om synlighet eller en annan lösning (t.ex. en egen,
publik kopia av bara review-komponenten) innan detta kan lösas riktigt.
