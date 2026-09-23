# Auto-install — ett kommando i Linux-terminalen

Snabbaste vägen från färsk Linux-maskin till körande Memaix: klona och kör ett skript.

## Så installerar du i dag
Repot är privat, så det finns ännu ingen publik installationsadress för `curl … | sh`.
Den fastställs när repot publiceras. Fram till dess — klona och kör:
```bash
git clone https://github.com/jimlov-poc-labs/memaix.git
cd memaix
less install.sh        # inspektera
./install.sh
```
Körs `install.sh` inifrån en klon används den katalogen; skriptet klonar inte en gång till.
Körs det fristående (utanför en klon) hämtar det koden från `MEMAIX_REPO` till `MEMAIX_DIR`
(default `./memaix`), vilket kräver läsrätt till repot.

## Vad skriptet gör (`install.sh`)
1. **Förkontroll:** Docker + Compose v2, `python3` och `make`. Saknas något → stoppar med tydlig
   instruktion (installerar **inte** Docker åt dig — det är invasivt och kräver root).
2. **Hittar koden:** klonen skriptet ligger i, annars `MEMAIX_DIR`, sist `git clone --depth 1`.
3. **Kör wizarden** (`bootstrap.py --init`, eller `--init --yes` oövervakat) — genererar all config
   + hemligheter, inga filer att redigera. Finns `.env` och `config/` redan hoppas steget över, så
   en omkörning roterar aldrig hemligheter som en befintlig databas redan använder.
4. **Reser stacken** (`make up` — bygger imagen, profiler från `COMPOSE_PROFILES` i `.env`) och
   **verifierar** (`make doctor WAIT=180` — väntar in gatewayen, kör sedan alla kontroller).
5. **Skriver ut** hur du kopplar in din AI.

## Oövervakad (leverantör / headless / CI)
```bash
MEMAIX_PROFILE=trial ./install.sh --yes
```
| Variabel | Betydelse | Default |
|---|---|---|
| `MEMAIX_PROFILE` | `trial`, `selfhost` (alias `solo`, `team`) eller `managed` | `trial` |
| `MEMAIX_DOMAIN` | domän, krävs för `selfhost`/`managed` | — |
| `MEMAIX_TUNNEL_TOKEN` | Cloudflare-tunneltoken (sätter tunnelprofilen) | ingen tunnel |
| `MEMAIX_ADMIN_USER` | adminanvändare | `admin` |
| `MEMAIX_ADMIN_PASSWORD` | adminlösenord (minst 8 tecken) | slumpas, se nedan |
| `MEMAIX_PROJECT` | första projektet | `shared` |
| `MEMAIX_NAME`, `MEMAIX_SUPPORT_EMAIL` | varumärke | `Memaix`, `support@example.com` |
| `MEMAIX_REPO`, `MEMAIX_DIR` | var koden hämtas/läggs när skriptet körs utanför en klon | `jimlov-poc-labs/memaix`, `memaix` |

Utan `MEMAIX_ADMIN_PASSWORD` slumpas ett lösenord och skrivs till `config/initial-admin-password`
(rättigheter 600, gitignorerad). Det skrivs aldrig till terminalen eller loggen — bara sökvägen.

### Vad CI verifierar
`.github/workflows/installer-e2e.yml` kör `MEMAIX_PROFILE=trial ./install.sh --yes` på en ren
ubuntu-runner vid varje PR och push till `main`, och failar om `make doctor` inte är grön.
Den kör installern en andra gång (omkörning ska vara säker), kontrollerar att `.env` har 600, att
inget hemligt värde syns i loggen och att installationen inte lämnar filer som git ser, och river
sedan ned stacken. **Täcks inte:** den interaktiva wizarden och webb-wizarden, `selfhost`/`managed`
(riktig domän, tunnel, TLS), inloggning och OAuth-flödet, Nextcloud, macOS/Windows.

## Säkerhet — ärligt om `curl | sh`
- Att pipa fjärrkod till skalet är bekvämt men du kör kod du inte läst. **Rekommendation:** ladda ner
  och inspektera först (ovan). Skriptet är **öppen källkod** på samma GitHub och gör inget dolt.
- När en publik adress finns ska skriptet **serveras över HTTPS**; pinna gärna en **version/checksum** och signera releasen.
- Det **auto-installerar inte Docker** (skulle kräva root och ändra systemet). Docker är den enda
  förkunskapen du själv sätter upp.

## Relation till resten
Installern är bara orkestreringen runt wizarden (`bootstrap.py --init`, `WIZARD.md`) — den lägger till
"förkontroll + hämta koden" så att allt blir **ett** kommando. Allt annat (config-generering,
hemligheter, stacken) gör `make init` / `make up` / `make doctor`.

## Acceptanskriterier
- [x] Ett kommando på en färsk Linux-maskin (med Docker) → körande instans (trial, verifierat i CI).
- [x] `--yes` ger oövervakad install för leverantör/CI utan frågor.
- [ ] Skriptet är auditbart; inspect-first-vägen dokumenterad.
- [ ] Stoppar med tydlig instruktion om Docker saknas; installerar det inte i smyg.
