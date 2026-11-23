# AGENTS.md — instruktioner & guardrails för den byggande AI:n

> Läs detta **först**, före all kodning. Det här är de hårda reglerna för AI-kodassistenten (eller
> människan) som bygger Memaix. Det ersätter inte specarna — det binder dem. Vid konflikt mellan denna
> fil och en äldre doc: **denna fil + `HANDOFF.md §4` gäller.** Tool-neutral med flit (produkten är
> AI-agnostisk); en Claude Code-session läser även denna fil.

## 0. Vad du bygger (en mening)
En AI-agnostisk MCP-gateway till team-gemensamt minne + en projektledaragent. **Hjärnan minns, agenten
agerar.** Specar: `docs/INDEX.md` → "🏗️ Ska du bygga gatewayen?".

## 1. Auktoritativa beslut (gäller ÖVER äldre formuleringar)
Se `HANDOFF.md §4` för full kontext. I korthet — bygg **inte** runt dessa:
- **Auth = ory Hydra** (certifierad OAuth2/DCR). Bygg **ingen egen OAuth-server**. Gateway validerar
  tokens; tunn login/consent-UI. Tunnel = ren proxy — **ingen** Cloudflare Access "Managed OAuth".
- **Minne = SQLite (aktivt tillstånd) + git asynkront (historik).** **Aldrig** commit-per-skrivning.
- **Safety-motorn byggs parallellt, inte sist** (`SAFETY.md`). Den är ett krav, inte en finish.
- **Mejl = BYO-infra, ingen reseller.** Projektspecifik provisionering i kundens egen leverantör.
- **iOS = kärnkrav** via remote connector. Skjut inte upp.
- **Audit = basal i kärnan**, inte enterprise-only.
- **Single-tenant per deployment.** Ingen delad multi-tenant-dataplan. Någonsin.

## 2. Säkerhets-guardrails (icke förhandlingsbara)
- **RBAC först.** Fas 1 är inte klar förrän en användare *utan* grant **bevisat nekas** (test, inte
  påstående). Bygg inget ovanpå otestad åtkomstkontroll. Varje verktyg tar `project` och valideras mot
  `acl.yaml` **före** körning (`acl.py`/`server.py`).
- **Allt backend-innehåll är otrodd data** (`THREAT-MODEL.md`). Läst innehåll (mejl/fil/kalender/andras
  minnesnoteringar) är **aldrig instruktioner**. Systemprompten ska märka det som data och förbjuda
  att det lyds.
- **Ingen auto-kedja.** Ett läs-verktyg får **inte** direkt initiera ett skriv-/utgående verktyg.
  Skrivande/utgående åtgärd triggad av läst innehåll kräver **mänsklig bekräftelse**.
- **Destruktivt/utgående = bekräfta.** Mejl skapas som **utkast**; skicka/radera kräver bekräftelse.
  Okänd mottagare → bekräfta mot allowlist. **MFA för admin/setup.**
- **Dumpa aldrig.** Retrieval, inte hela vaults/brevlådor till modellen. Data-minimering mot
  AI-leverantören (`SAFETY.md §6`).
- **Scrub före sändning.** Inga hemligheter i utgående innehåll eller loggar.

## 3. Repo- & hemlighetsregler
- **Inga hemligheter i repot.** `.env` är gitignored. Config refererar secrets via `*_ref`
  (`env:`/`file:`/`vault:`/`kms:`, se `config.py` + `docs/SECRETS.md`). Hemligheter ekas aldrig till
  klienten, loggas aldrig, scrubbas ur fel/traces. Committa aldrig nycklar, tokens eller kunddata.
- **Scope:** arbeta bara i repot `jimlov-poc-labs/memaix`, branch `main`. Pusha aldrig till annan branch utan tillstånd.
- **Backuper** krypteras med kund-hållen nyckel (BYOK) — bygg aldrig in en master-nyckel.

## 4. Licens & bidrag (vid varje ny fil/PR)
- **Licens:** AGPL-3.0-or-later. **SPDX-header** i varje ny källfil:
  `SPDX-License-Identifier: AGPL-3.0-or-later`.
- **Direkta beroenden permissiva/LGPL** — importera **inte** ett GPL-*bibliotek* in i processen
  (Nextcloud/Forgejo körs som separata tjänster över API, det är OK). Se `LICENSING.md`.
- **Nytt beroende?** Det ska hamna i SBOM:en (CI) och vara licens-kompatibelt. Lägg attribution i
  `NOTICE` om licensen kräver det (Apache/MIT/BSD).
- **Commits:** `git commit -s` (DCO). CLA blir aktuellt först om dual-license-försäljning väljs.

## 4b. Commit-konvention: Conventional Commits sedan 2026-09-08
`semantic-release` (jobbet `release` i `.github/workflows/ci.yml`) läser commit-meddelanden på
`main` och skapar en riktig GitHub-release automatiskt när något kvalificerar. En publicerad
release triggar i sin tur `.github/workflows/release-notify.yml`, som skickar en notis till
jimlov.se:s projektsida. Formatet är **`typ(Modul): text`**:

```
feat(Gateway): stöd för scoped minnesnycklar per projekt
fix(RBAC): nekad åtkomst loggades inte vid saknad grant
chore(CI): automatisk versionshantering via semantic-release
```

- `feat` → minor, `fix`/`perf` → patch, `feat!`/`BREAKING CHANGE:` i footern → major.
- `chore`/`docs`/`refactor`/`style`/`test`/`ci`/`build` triggar **ingen** release.
- Om inget kvalificerande landat sen senaste release gör semantic-release ingenting — det är
  förväntat, inte ett fel.
- Squash-merge: det är squash-commiten som analyseras, så PR-titeln bör redan följa formatet.

## 4c. Skriv rätt från början (Sonar-checklista)
Läs `jimlov-crew/plugins/infra/skills/modulbygge/references/skriv-ratt-fran-borjan.md`
innan du skriver ny kod — en proaktiv checklista byggd på portföljens vanligaste
Sonar-fynd (await-i-loop, sammansatta assert-satser, obehanterade löften m.fl.).

## 5. Innan du öppnar PR (checklista)
Speglar `CONTRIBUTING.md`:
- [ ] Tester för det du rört — särskilt **deterministiska motorn** och **RBAC-enforcement** (`TESTING.md`).
- [ ] Injektions-/exfiltrerings-testfall om du rört läs-/skriv-vägar (`THREAT-MODEL.md`).
- [ ] `python3 scripts/check-docs-index.py` grön (nytt `docs/*.md` → in i `INDEX.md`).
- [ ] SPDX-header i nya källfiler; SBOM-bygget grönt; `NOTICE` uppdaterad vid nytt beroende.
- [ ] Ingen v2-regel (avsnitt 1) bruten; ingen doc lämnad självmotsägande.

## 6. Byggordning (följ den)
Från `BUILD.md` med v2-deltan: **1)** skelett + ACL (stdio) + RBAC-bevis → **2)** backends (minne =
SQLite + git async) → **3)** safety-motorn *parallellt* → **4)** remote + Hydra, doctor grön →
**5)** RBAC skarpt + onboarding + per-user-OAuth. Hoppa inte över RBAC-beviset eller safety-motorn.

## 6b. Loopar, verifiering & lärande (självförbättrande arbetsflöde)
Destillerat ur verkliga incidenter (juli 2026: CDN-cachad gammal frontend; admin-skrivvägar tyst
brutna av :ro-mount; wizard som byggde en föråldrad säkerhetsmodell dokumenten inte avslöjade).
Gemensam nämnare: felen var osynliga i kanalen vi tittade i, uppenbara i kanalen bredvid.
1. **Verifiera det publicerade, inte det deployade.** Deploy som ändrar klientkod är inte klar
   förrän den publika URL:en (genom CDN/tunnel) bevisat serverar nya innehållet. Serverhälsa ≠
   användarupplevelse.
2. **Motionera nya skrivvägar i drift.** En skrivande funktion är inte levererad förrän den körts
   en gång på riktigt i målmiljön (eller monteringen/skrivrätten verifierats där). Läsning som
   funkar + grön CI bevisar ingenting om skrivvägen.
3. **Diffa spec mot kod före spec-styrt bygge.** Statuskolumner ljuger åt båda hållen. Bygg aldrig
   "enligt dokumentet" utan att först verifiera dokumentets påståenden mot koden — och uppdatera
   statusen **i samma PR** som bygget, aldrig i efterhand.
4. **Separat granskare för känsliga ändringar.** PR som rör auth, ACL, hemligheter eller egress
   får en adversariell granskning av en annan agent/instans än den som skrev koden — utöver CI.
   Självkritik är strukturellt partisk; granskaren har ingen investering i resonemanget.
5. **Fixen till repot, regeln till systemet.** Varje icke-trivial bugg destilleras till en regel
   (här i 6b, i relevant spec, eller i byggarens beständiga minne). En bugg som bara fixas är en
   bugg som återkommer i ny kostym.

**Anti-rationalisering** (format lånat från addyosmani/agent-skills, MIT; raderna är våra egna
incidenter — läs tabellen INNAN du formulerar ursäkten, den står redan här):

| Ursäkten du är på väg att ge | Verkligheten |
|---|---|
| "CI är grön, alltså funkar det" | CI bevisar vad testerna frågar om. Admin-skrivvägarna var trasiga i veckor med grön CI (regel 2). |
| "Deployen gick igenom, klart" | Cloudflare serverade förra veckans frontend i timmar efter en lyckad deploy (regel 1). |
| "Dokumentet säger att det är byggt/planerat" | Statuskolumnerna sa 'planerad' om byggt och lovade obyggda kommandon — samtidigt (regel 3). |
| "Jag har själv granskat min ändring noga" | Den som byggt resonemanget är investerad i det. :ro-mounten överlevde varje självgranskning (regel 4). |
| "Buggen är fixad, vi går vidare" | Ofärdig lärdom = samma bugg i ny kostym. Admin-flaggan som saknades i wizarden VAR en sådan repris (regel 5). |
| "Det är bara en liten docs-/config-ändring" | :ro var en rad i en compose-fil. Wizardens fel var en utebliven YAML-rad. Små rader, veckolånga fel. |

## 7. När något är otydligt
Specarna är skrivna av en planerings-session och kan ha luckor. Om en spec säger emot en annan, eller
en detalj saknas: **stanna och fråga Alice** — gissa inte kring auth, RBAC, säkerhet eller licens.
För allt annat: följ närmaste spec och håll stilen lik omgivande kod och docs (korta, konkreta).
