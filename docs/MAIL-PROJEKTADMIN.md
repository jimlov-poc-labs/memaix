# Standardmail: välkommen som projektadmin

Mall att skicka från hello@memaix.se när någon bjudits in som ägare av ett projekt
(`user_invite`). Byt ut `{{…}}`. Inbjudningslänken är personlig, gäller en gång och i
7 dagar — skicka den aldrig i grupp- eller kopietråd.

---

**Ämne:** Du har fått tillgång till {{projekt}} i Memaix

Hej {{namn}},

Du är nu projektadmin för **{{projekt}}** i Memaix — ett delat minne och en
arbetsyta som din AI-assistent kan använda. Du ser bara {{projekt}}, inget annat.

**1. Välj lösenord (en gång, giltig i 7 dagar)**
{{inbjudningslänk}}
Ditt användarnamn är **{{användarnamn}}**. Lösenordet väljer du själv, minst 12 tecken.

**2. Koppla in Memaix i din AI-assistent**
Adressen är alltid: `https://mcp.memaix.se`
Du loggar in med användarnamnet och lösenordet ovan när fönstret öppnas.

- **Claude (claude.ai):** Settings → Connectors → Add custom connector → klistra in adressen → Connect.
- **ChatGPT:** Settings → Connectors (slå på Developer mode om det behövs) → lägg till en egen
  MCP-server med adressen, autentisering OAuth.
- **Mistral Le Chat:** Settings → Connectors → Add custom MCP connector → adressen.

Menynamn ändras ibland; hittar du inte rätt, skriv så hjälper jag dig.

**3. Vad du kan göra**
Skriv vanlig svenska till assistenten, t.ex.:
- "Vad vet du om {{projekt}}?" — läser och söker i projektets minne.
- "Spara det vi beslutade i dag i {{projekt}}" — skriver minnesanteckningar.
- "Lägg till ett kort i backlogen: …" / "Visa backloggen" — att göra-lista med status.
- "Vilka har tillgång till {{projekt}}?" — medlemslistan.
- "Bjud in {{epost}} som läsare/medarbetare" — ny person, får egen länk. Förfrågan
  köas och genomförs först när den godkänns i Memaix utkorg (säkerhetsspärr).
- "Ta bort {{användare}} från {{projekt}}" — återkallar åtkomst (köas på samma sätt).

Som projektadmin kan du hantera medlemmar i {{projekt}} men inte skapa nya projekt eller
se andra projekt.

**Tänk på:** lägg inte lösenord eller känsliga personuppgifter i minnet. Är något oklart
eller får du ett felmeddelande — svara på det här mailet.

Vänliga hälsningar
Memaix · hello@memaix.se
