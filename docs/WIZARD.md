# Setup-wizarden — steg för steg (`./setup.sh` eller `make init`)

Den följbara frågesekvensen som tar en nedladdare från `git clone` till körande. **Minimal,
progressiv, default på varje fråga.** Bara det valda spåret frågas. Wizarden **genererar all config +
hemligheter** — du redigerar ingen YAML. Bäraren (webb vs CLI): `SETUP-UI.md`. Förenklingsprincip:
`SETUP-SIMPLIFICATION.md`.

> **Status:** två bärare finns — `./setup.sh`/`.\setup.ps1` (lokal webb, rekommenderad) och
> `make init` (CLI). Båda kör `scripts/setup_engine.py`. Fråga 6 (backends) nedan är ännu inte
> implementerad i någon bärare; backends läggs till efteråt (docs/BACKENDS.md).

## Principer
- **Progressiv disclosure** — bara frågor som det valda spåret behöver.
- **Default på allt** — Enter accepterar; inget *måste* fyllas i för att komma igång.
- **Skjut upp det svåra** — backends/OAuth/personer kan läggas till *efter* att assistenten funkar.
- **Genererar allt** — config + hemligheter; ingen `openssl`, ingen filredigering.

## Frågorna (i ordning)

**1. Vad vill du göra?**
```
[1] Prova lokalt (default)        – stdio, inget tunnel/OAuth/domän, ~5 min
[2] Self-host (mobil/team)        – lägger tunnel + OAuth
[3] Installera åt en kund (managed)
```
→ styr vilka frågor som följer.

**2. Branding** *(valfri — default: Memaix)*
```
Eget namn? [Memaix]
```

**3. Domän & tunnel** *(bara spår 2/3)*
```
Din domän? (t.ex. mcp.dittforetag.se)
Tunnel?  [1] Cloudflare-token   [2] Quick-tunnel (test, ingen DNS)   [3] Egen reverse proxy
```

**4. Vilken AI ska driva Memaix?** *(se `CHOOSE-YOUR-LLM.md`)*
```
[1] Min egen AI (Claude/ChatGPT/Mistral)   – BYO, inget mer  (default spår 1)
[2] API-nyckel                              – provider + nyckel
[3] Lokal modell                            – endpoint + modellnamn
```

**5. Projekt & personer** *(default: ett projekt + du som owner)*
```
Projektnamn? [mitt-projekt]
Din mejl (owner)?
Lägga till fler personer nu? [n]      (annars senare: memaix user add)
```

**6. Mejl / kalender / filer?** *(default: senare)*
```
Koppla backends nu eller senare? [senare]
  Om nu:  [1] Bundlad Nextcloud   [2] Gmail/Workspace   [3] M365   [4] Egen IMAP/CalDAV
```

**7. Sammanfattning → kör**
```
Visar dina val → "Kör nu? [j]"
→ genererar config + hemligheter, reser stacken, kör doctor.
```

## Vad wizarden skriver ut efteråt
- **Spår 1 (trial):** den exakta raden att klistra in i Claude Desktop (lokal MCP) — klart.
- **Spår 2/3:** "Din connector-URL: https://… — lägg in i din AI (`CHOOSE-YOUR-LLM.md`)."
- Alltid: "Lägg till mejl/kalender senare: `memaix backend add`. Lägg till folk: `memaix user add`.
  Ta trial → mobil: `make go-remote`."

## Lägg till senare (progressivt — inget krav vid start)
- `memaix backend add` — koppla mejl/kalender/filer (en provider i taget)
- `memaix user add` — bjud in en person till ett projekt
- `make go-remote` — ta en lokal trial till mobil/multi-user

## Acceptanskriterier
- [ ] Trial-spåret kräver **≤ 2 frågor** till körande.
- [ ] Varje fråga har en default; inget måste fyllas i för att komma igång.
- [ ] Backends och personer kan läggas till **efter** att assistenten redan fungerar.
- [ ] Wizarden genererar all config + hemligheter; ingen manuell YAML-redigering.
- [ ] Avslutar med en konkret "så här kopplar du in din AI"-rad för det valda spåret.
