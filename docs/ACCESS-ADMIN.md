# Projekt och medlemmar via MCP

Skapa projekt, gör människor till ägare av ett enskilt projekt och bjud in nya personer — allt
från din AI-klient, utan att röra `acl.yaml` för hand.

## Rollmodell

| Roll | Vad den får |
|---|---|
| **Systemadmin** (`admin: true`) | Implicit ägare av alla projekt. Enda som kan skapa projekt. |
| **Projektägare** (`owner` på ett projekt) | Hantera medlemmar och bjuda in — bara i *det* projektet. |
| `collaborator` / `reader` | Som tidigare; ingen åtkomsthantering. |

En projektägare kan aldrig skapa projekt, röra andra projekt eller ändra en systemadmin.
Ett konto som bjuds in får aldrig `admin`.

## Verktyg

| Verktyg | Vem | Effekt |
|---|---|---|
| `project_create(name)` | systemadmin | Nytt projekt med eget git-valv (`/srv/vaults/<namn>`). Du blir ägare, ingen annan får något. Namn: 2–32 tecken `a-z 0-9 - _`. |
| `project_members(project)` | projektägare | Lista medlemmar och roller. |
| `project_member_set(project, member, role)` | projektägare | Ändra roll, eller `role: null` för att ta bort. |
| `user_invite(project, invitee, role, email?)` | projektägare | Skapa ett inloggningskonto begränsat till projektet. |

`project_member_set` och `user_invite` **köas i utkorgen** och körs först när en människa godkänner
(`outbox_approve` eller webbens utkorg). Behörigheten kontrolleras igen vid godkännandet, så en
ägare som degraderats i mellantiden kan inte få en gammal åtgärd genomförd.
Skyddsregel: den sista icke-admin-ägaren i ett projekt kan inte ta bort sig själv
(systemadmin kan alltid ändra).

## Inbjudan med eget lösenord

1. `user_invite` → godkänn i utkorgen.
2. Godkännandet returnerar `invite_url` (`https://<public_url>/app/invite/<token>`).
   Skicka länken till personen.
3. Personen väljer eget lösenord (minst 12 tecken) på sidan. Länken gäller en gång och i
   7 dagar; en ny inbjudan ogiltigförklarar den gamla.
4. Därefter kopplar personen in Memaix i sin AI-klient (se [AI-CLIENTS.md](AI-CLIENTS.md)) och loggar in
   med användarnamnet och lösenordet.

Säkerhet: bara SHA-256 av token sparas (`memaix-invites.db`), länken kan aldrig sätta lösenord på ett
konto som redan har ett, och sidan svarar `no-store` utan Referer. Tokenen finns i klartext i utkorgens
resultatrad tills den används — samma förtroendezon som `acl.yaml`.

## Gränser (kända)

- Webb-UI:t (`/app`) har egen inloggning via `MEMAIX_ALLOWED_USERS`; inbjudna konton loggar in via
  MCP/OAuth, inte där.
- Ingen e-postutskick av länken ännu — du levererar den själv.
- Ta bort ett helt konto: sätt `disabled` i admin-UI:t.

## Drift

Login-appen läser `acl.yaml` när filen ändras, så nya konton kan logga in utan omstart.
Därför monteras hela `config/`-katalogen skrivskyddat (inte filen): `AclWriter` byter fil atomärt och
en fil-bindmount skulle annars peka på den gamla.
`MEMAIX_VAULTS_DIR` (standard `/srv/vaults`) styr var nya valv skapas.
