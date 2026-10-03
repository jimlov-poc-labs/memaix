# Memaix-dokumentation — börja här

Läs i den ordning som passar din roll.

## 🧭 Är du beslutsfattare / vill förstå värdet?
1. [PRODUCT.md](PRODUCT.md) — vad det är, problem som löses, use cases, 5 branscher, pitch.
2. [FOR-LEDNINGSGRUPPEN.md](FOR-LEDNINGSGRUPPEN.md) — en sida utan teknik: fråga & följ upp projekt.
3. [ADDON-PROJECT-MANAGEMENT.md](ADDON-PROJECT-MANAGEMENT.md) — PM-agenten (agil + vattenfall), affärsvärde.
4. [ENTERPRISE.md](ENTERPRISE.md) — enterprise-tiern: SSO/SCIM, audit, flotthantering.
5. [LEGAL.md](LEGAL.md) — juridik & ansvar: GDPR-roller, DPA, AI som underbiträde (checklista).
6. [LICENSING.md](LICENSING.md) — komponentlicenser + vilken licens vår egen kod ska ha.

## 🏗️ Ska du bygga gatewayen?
0. [AGENTS.md](../AGENTS.md) — **läs först:** bindande guardrails för den byggande AI:n (v2-beslut, säkerhet, licens, byggordning).
1. [ARCHITECTURE.md](ARCHITECTURE.md) — hur delarna hänger ihop.
2. [MCP-API.md](MCP-API.md) — gränssnittskontraktet: vad man kan göra via MCP.
3. [BUILD.md](BUILD.md) — bygg-ordning, moduler, faser för kärnan.
4. [ADDON-PM-BUILD.md](ADDON-PM-BUILD.md) + [MCP-API-PM.md](MCP-API-PM.md) — PM-modulen: bygg-spec + signaturer.
   · [PM-AGENT.md](PM-AGENT.md) — agent-arkitektur & best practice för PM-agenten.
   · [PM-PLANNING-ENGINE.md](PM-PLANNING-ENGINE.md) — planeringsmotor: resurser, allokering, what-if, rapport.
   · [PM-DATA-MODEL.md](PM-DATA-MODEL.md) — konkret SQLite-schema (resurser, uppgifter, scenarier, plan).
5. [SAFETY.md](SAFETY.md) — drift-säkerhet: rate limit, circuit breaker, concurrency, context, retention.
   · [THREAT-MODEL.md](THREAT-MODEL.md) — AI-hot: prompt injection & exfiltrering (största säkerhetsluckan).
   · [MEMORY-RETRIEVAL.md](MEMORY-RETRIEVAL.md) — persistent minne: semantiskt retrieval-lager (embeddings) över vaulten, säkert (MEX-008).
   · [TESTING.md](TESTING.md) — teststrategi: motor-korrekthet, RBAC, idempotens, injection, eval.
6. [REVIEW-RESPONSE.md](REVIEW-RESPONSE.md) — v2-justeringar efter oberoende granskning.
7. [PACKAGING.md](PACKAGING.md) — connector vs skill vs plugin; integrerad paketering.
   · [MEMAIX-PLUGIN.md](MEMAIX-PLUGIN.md) — konkret plugin-innehåll + automation (hooks/server/cron).
8. [OPEN-GAPS.md](OPEN-GAPS.md) — kritisk självgranskning: säkerhet/UX/programmatiska luckor.
   · [DEVELOPMENT-PROPOSALS.md](DEVELOPMENT-PROPOSALS.md) — kodgranskning av gatewayen: buggar, säkerhetsfynd, 10 förslag.
   · [FEATURE-PROACTIVE-BRIEF.md](FEATURE-PROACTIVE-BRIEF.md) — funktion #1: proaktiv brief & notiser (design + byggspec).
   · [FEATURE-SEMANTIC-SEARCH.md](FEATURE-SEMANTIC-SEARCH.md) — funktion #2: enhetlig semantisk sökning/RAG med källhänvisning (design + byggspec).
   · [FEATURE-APPROVAL-OUTBOX.md](FEATURE-APPROVAL-OUTBOX.md) — funktion #3: utkorg med bekräftelse för utgående åtgärder (design + byggspec).
   · [FEATURE-AUTOMATION-RULES.md](FEATURE-AUTOMATION-RULES.md) — funktion #4: stående instruktioner & automationsregler (design + byggspec).
   · [FEATURE-UNDO-TIMELINE.md](FEATURE-UNDO-TIMELINE.md) — funktion #5: ångra & åtgärdstidslinje (design + byggspec).
   · [FEATURE-DISCOVERABILITY.md](FEATURE-DISCOVERABILITY.md) — funktion #6: upptäckbarhet, guide & förmåge-register (design + byggspec).
   · [FEATURE-CONNECTOR-FRAMEWORK.md](FEATURE-CONNECTOR-FRAMEWORK.md) — funktion #7: pluggbara backend-adaptrar (design + byggspec).
   · [FEATURE-NEXTCLOUD-BACKEND.md](FEATURE-NEXTCLOUD-BACKEND.md) — funktion #8: Nextcloud som förstklassig backend (design + byggspec).
   · [FEATURE-PM-ENGINE.md](FEATURE-PM-ENGINE.md) — funktion #9: PM-planeringsmotor & agent (byggspec).
   · [FEATURE-LLM-ENGINE.md](FEATURE-LLM-ENGINE.md) — funktion #10: LLM-motorn — server-side modell, verktygsbrygga, egen chatt (design + byggspec).
   · [SELF-IMPROVING-SYSTEM.md](SELF-IMPROVING-SYSTEM.md) — inriktning: självövervakning, minnestrappan, destillatrutin, eval-svit (loopar över befintliga organ).
   · [FEATURE-AGENT-TEAM.md](FEATURE-AGENT-TEAM.md) — funktion #11: agent-teamet — flera agenter som ett lag runt boarden, handoff via vaulten, läge B (design + byggspec).
   · [ROADMAP.md](ROADMAP.md) — implementeringsroadmap: bygg-ordning, faser, beroenden.
   · [WEB-UI-SPEC.md](WEB-UI-SPEC.md) — webb-UI helhetspec: IA, rollmatris, ASCII-mockups, komponentbibliotek, byggordning (Fable-genererad).
   · [FEATURE-WEB-UI-FOUNDATION.md](FEATURE-WEB-UI-FOUNDATION.md) — MEX-022 Fas A: mörkt tema, app-shell, hem-dashboard (design + byggspec).
   · [FEATURE-WEB-UI-MVP.md](FEATURE-WEB-UI-MVP.md) — MEX-022+023 MVP: board i shell, inställningar/konton, minnesutforskare, per-user-login (design + byggspec).
   · [FEATURE-WEB-UI-OUTBOX-AND-ADMIN.md](FEATURE-WEB-UI-OUTBOX-AND-ADMIN.md) — MEX-024 Fas C: utkorg-UI, kortmodal med kommentarer/poäng, admin-läsvyer (design + byggspec).
   · [FEATURE-WEB-UI-PHASE2.md](FEATURE-WEB-UI-PHASE2.md) — MEX-025 Fas D: global sökning, brief-inställningar, admin-skriv, MFA (design + byggspec).
9. [CODE-WORKFLOW.md](CODE-WORKFLOW.md) — backlog → kod → merge; forge-agnostiskt (GitHub/GitLab/Forgejo).

## 🚀 Ska du installera och driva?
0. [USER-MANUAL.md](USER-MANUAL.md) — **användarmanual**: installation, konfiguration, verktygskatalog, säkerhetsmodell och vanliga arbetsflöden i ett dokument. Börja här om du vill *använda* Memaix.
1. [INSTALL.md](INSTALL.md) — installation (automatisk + manuell).
   · [QUICK-INSTALL.md](QUICK-INSTALL.md) — **ett kommando i terminalen** (curl … | sh).
   · [EXPOSE.md](EXPOSE.md) — alla sätt att exponera Memaix: Cloudflare Tunnel, Caddy, Tailscale, ngrok, underkatalog.
   · [SETUP-SIMPLIFICATION.md](SETUP-SIMPLIFICATION.md) — tre nivåer; börja smått (trial → remote).
   · [IMPORT.md](IMPORT.md) — kallstart & import (största adoptionshindret).
2. [WIZARD.md](WIZARD.md) — den guidade uppsättningen, steg för steg.
   · [SETUP-UI.md](SETUP-UI.md) — webb-wizard vs native app + säkerhetsdesign.
3. [AUTO-INSTALLER.md](AUTO-INSTALLER.md) — hela planen för det auto-installerande systemet.
   · [DOCTOR.md](DOCTOR.md) — verifiering & hälsokontroll (alla checkar, rapportformat).
   · [OBSERVABILITY.md](OBSERVABILITY.md) — drift-insyn: loggar, metrics, larm.
4. [SECURITY.md](SECURITY.md) — härdning + kända fallgropar (OAuth, Cloudflare).
   · [SECURITY_REVIEW_2026-07-25.md](SECURITY_REVIEW_2026-07-25.md) — extern granskning: 16 säkerhetsfynd, kodkvalitet, tester, drift. Åtgärdsordning sist.
   · [SECRETS.md](SECRETS.md) — hemligheter: lagring (env/file/vault/kms), kryptering i vila, rotation, scrubbing.
5. [AI-CLIENTS.md](AI-CLIENTS.md) — koppla in Claude, ChatGPT, Mistral m.fl.
   · [CHOOSE-YOUR-LLM.md](CHOOSE-YOUR-LLM.md) — **vilken LLM & hur** (beslutsguide för nedladdaren).
   · [ACCESS-MODES.md](ACCESS-MODES.md) — använda Memaix *utan* egen AI (server-modell, lokal modell, GUI).
   · [LOCAL-MODEL.md](LOCAL-MODEL.md) — lokal öppen modell: vilka modeller & hårdvara (utvärdering).
6. [BACKENDS.md](BACKENDS.md) — koppla in Gmail, M365, Nextcloud m.fl. (adaptrar).
7. [PER-USER-OAUTH.md](PER-USER-OAUTH.md) — koppla din egen Gmail/M365 (länkning, token, refresh).
8. [SELF-HOST-STACK.md](SELF-HOST-STACK.md) — topologi (Nextcloud-samlokalisering) + mejl-provisionering.
9. [MAIL.md](MAIL.md) — mejlstrategi: leverantörer, reseller-postur, transaktionsmejl, slutanvändar-UI.
10. [SYSTEM-MAIL.md](SYSTEM-MAIL.md) — systemmejl: config, avsändardomän/DKIM, mallar.
   · [ACCESS-ADMIN.md](ACCESS-ADMIN.md) — skapa projekt, projektägare, inbjudningar med eget lösenord.
   · [MAIL-PROJEKTADMIN.md](MAIL-PROJEKTADMIN.md) — standardmail till ny projektadmin.
11. [BACKUP.md](BACKUP.md) — backup & återställning (vaults, config, hemligheter, Nextcloud).
12. [UPDATE.md](UPDATE.md) — uppdatering: versionsmigrering, rollback, nedtid.
13. [FEATURE-BOOKING-EMBED.md](FEATURE-BOOKING-EMBED.md) — bokningswidgeten: bädda in mötesbokning på en extern sida med en div och en script-tagg.

## 💼 Ska du sälja installation/hosting?
1. [BUSINESS-CASE.md](BUSINESS-CASE.md) — kostnad, pris, kritisk bedömning (publik-OSS-verklighet).
   · [SWOT.md](SWOT.md) — styrkor/svagheter/möjligheter/hot + strategisk syntes.
2. [SERVICE-PROVIDERS.md](SERVICE-PROVIDERS.md) — affärsmodellen: en instans per kund, på kundens infra.
3. [WHITE-LABEL.md](WHITE-LABEL.md) — kör under kundens eget namn, domän och tunnel.

## Snabbkarta
| Fråga | Läs |
|---|---|
| Vad är det och varför? | PRODUCT, FOR-LEDNINGSGRUPPEN |
| Vad kan man göra via MCP? | MCP-API, MCP-API-PM |
| Hur byggs det? | ARCHITECTURE, BUILD, ADDON-PM-BUILD |
| Hur bygger man en specialiserad agent (PM)? | PM-AGENT |
| Connector, skill eller plugin? | PACKAGING |
| Backlog → kod → merge / GitHub-alternativ? | CODE-WORKFLOW |
| Plugin-innehåll & automation (hooks)? | MEMAIX-PLUGIN |
| Resursplanering, allokering, konsekvensanalys? | PM-PLANNING-ENGINE |
| Hur installerar/driver jag? | QUICK-INSTALL, INSTALL, WIZARD, SETUP-UI, AUTO-INSTALLER, SECURITY, AI-CLIENTS |
| Hur verifierar jag installationen? | DOCTOR |
| Hur lagrar/roterar jag API-nycklar & lösenord? | SECRETS, SETUP-UI |
| Vilka säkerhetsbrister finns kända? | SECURITY_REVIEW_2026-07-25, SECURITY |
| Kan vi koppla in Gmail/M365 & egna konton? | BACKENDS, PER-USER-OAUTH |
| Vilken LLM ska jag välja & hur? | CHOOSE-YOUR-LLM |
| Använda Memaix utan egen AI? | ACCESS-MODES |
| Köra på lokal modell — vilka & vilken hårdvara? | LOCAL-MODEL |
| Self-host: Nextcloud + mejl-provisionering? | SELF-HOST-STACK |
| Mejlstrategi & systemmejl? | MAIL, SYSTEM-MAIL |
| Backup & återställning? | BACKUP |
| Hur uppdaterar jag säkert? | UPDATE |
| Hur tjänar vi pengar? | BUSINESS-CASE, SERVICE-PROVIDERS, WHITE-LABEL, ADDON-PROJECT-MANAGEMENT |
| Kostnad, pris & lönsamhet? | BUSINESS-CASE |
| Styrkor/svagheter/möjligheter/hot? | SWOT |
| Vad krävs för storföretag? | ENTERPRISE |
| Skydd mot skenande AI / concurrency? | SAFETY |
| Persistent/semantiskt minne (retrieval)? | MEMORY-RETRIEVAL |
| Hur ser jag att en instans är frisk? | OBSERVABILITY, DOCTOR |
| Vad ändrades efter extern granskning? | REVIEW-RESPONSE |
| Vad har vi missat (kritiskt)? | OPEN-GAPS |
| Kod-buggar/säkerhetsfynd & förbättringsförslag? | DEVELOPMENT-PROPOSALS |
| Proaktiv brief/notiser — design & byggspec? | FEATURE-PROACTIVE-BRIEF |
| Semantisk sökning/RAG — design & byggspec? | FEATURE-SEMANTIC-SEARCH |
| Utkorg/bekräftelse för utgående — design & byggspec? | FEATURE-APPROVAL-OUTBOX |
| Automationsregler/stående instruktioner — design & byggspec? | FEATURE-AUTOMATION-RULES |
| Ångra/åtgärdstidslinje — design & byggspec? | FEATURE-UNDO-TIMELINE |
| Hur guidas användaren / vad kan man göra? | FEATURE-DISCOVERABILITY |
| I vilken ordning byggs allt? | ROADMAP |
| Pluggbara integrationer / backend-adaptrar? | FEATURE-CONNECTOR-FRAMEWORK |
| Nextcloud som förstklassig backend? | FEATURE-NEXTCLOUD-BACKEND |
| Avancerad PM-motor (kritisk linje, what-if)? | FEATURE-PM-ENGINE |
| Juridik, GDPR & ansvar? | LEGAL |
| Licenser (komponenter + vår kod)? | LICENSING |
