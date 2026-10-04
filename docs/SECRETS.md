# Hemligheter — lagring, upplösning, rotation

Hur Memaix tar emot, lagrar och använder hemligheter (LLM-API-nycklar, mejl-/OAuth-credentials,
backup-nycklar) **utan** att de hamnar i repot, i loggar eller i klartext i drift. Kompletterar
`SETUP-UI.md` (hur de matas in) och `SECURITY.md` (härdning).

## Grundregler (icke förhandlingsbara)
- **Aldrig i repot.** `.env` är gitignored. Config-YAML innehåller bara en **referens** (`*_ref`),
  aldrig ett värde (se `gateway/src/memaix_gateway/config.py`).
- **Aldrig i klartext mot klienten.** UI visar `••••• (satt)`, erbjuder *rotera/ersätt* — **aldrig
  "visa nyckel"**. Ekas aldrig tillbaka, loggas aldrig.
- **Scrubbas i drift.** Värdet lever bara i processminnet; rensas ur loggar, felmeddelanden och traces
  (kopplar till secret-scrubbing i `THREAT-MODEL.md`).

## Pluggbar upplösning (`secret(ref)` med prefix)
`config.secret()` tolkar ett prefix och hämtar värdet från vald källa. Resten av koden ber bara om
`secret(ref)` och bryr sig inte om var det bor:

| Nivå | Källa | För vem | `*_ref`-form |
|---|---|---|---|
| **Bas** | `.env`, `chmod 600`, service-user, utanför repot | solo / liten self-host | `env:OPENROUTER_KEY` |
| **Bättre** | Docker/Podman secrets · systemd `LoadCredential` (tmpfs, syns ej i `ps`/`/proc`) | seriös self-host | `file:/run/secrets/llm_key` |
| **Bäst** | **OpenBao / HashiCorp Vault** (öppen källkod) eller moln-KMS (GCP Secret Manager / AWS) | managed / reglerad | `vault:memaix/llm#key` |

Bakåtkompatibelt: ett bart namn utan prefix tolkas som `env:` (nuvarande beteende).

**Per-projekt Nextcloud-hemligheter använder `file:`-schemat.** Med `nextcloud_provision` i
`memaix.yaml` skapar `project_create` ett eget Nextcloud-konto per projekt. Det genererade lösenordet
skrivs till `<config-katalogen>/secrets/nc-<projekt>` (katalog `0700`, fil `0600`, skrivs aldrig över)
och refereras i `acl.yaml` som `password_ref: "file:<sökväg>"`. Admin-lösenordet för
provisioneringen anges separat via `admin_password_ref`. Lösenordet returneras och loggas aldrig.

## Kryptering i vila (om värdet lagras lokalt)
- Lagras en hemlighet i SQLite/fil → kryptera blobben (libsodium/age sealed box) med en **KEK som inte
  ligger bredvid datan** (envelope encryption via KMS/Vault). Bara ciphertext i vila.
- **Backuper:** krypteras med **kund-hållen nyckel (BYOK)** — bygg aldrig in en master-nyckel
  (`BACKUP.md`, `AGENTS.md §3`).

## Scoping & least privilege
- Per-projekt / per-användare-nyckel där det går (projekt-LLM-nyckel ≠ delad). BYO-nyckel per användare
  lagras per användare, krypterad.
- LLM-nyckeln för PMA:n är en server-hemlighet; lös via `*_ref`, lägg aldrig i `memaix.yaml` i klartext.

## Rotation & revokering
- Rotera **utan omdeploy** — hämta om från store, eller via det efemära admin-läget (`SETUP-UI.md`).
- **Audit** på vem som ändrade/roterade. Revokera vid offboarding.
- Idempotent rotation — pågående anrop bryts inte mitt i.

## Transport
- Hemligheter rör sig bara över localhost/tunnel, alltid **TLS**. Aldrig i klartext över nätet.

## Rekommendation
- **Self-host solo:** `env:` + `.env chmod 600` (nuvarande mönster) räcker.
- **Managed / reglerad kund:** **OpenBao** (eller kundens moln-KMS) via det pluggbara `secret()`-mönstret
  + envelope encryption.

## Acceptanskriterier
- [ ] Inga hemligheter i repot eller i klartext-YAML; allt via `*_ref`.
- [ ] `secret()` stödjer `env:`/`file:`/`vault:`/`kms:` (bart namn = `env:`).
- [ ] Hemligheter ekas aldrig till klienten och scrubbas ur loggar/fel/traces.
- [ ] Lokalt lagrade hemligheter krypterade i vila med separat KEK; backup-nyckel kund-hållen.
- [ ] Rotation utan omdeploy + audit på ändring; revokering vid offboarding.
