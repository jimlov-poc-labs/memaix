# Bokningswidgeten — lägg mötesbokning på vilken sida som helst

Memaix bokningsflöde (kalendergrid, formulär, captcha, bekräftelse) serveras
som **en fil** från gatewayen. Sidan som vill ha bokning skriver en div och en
script-tagg. Inget bygge, inget npm-paket, ingen kopierad JavaScript.

```html
<div data-memaix-booking="DIN-SLUG"></div>
<script src="https://mcp.example.se/embed/booking.js" defer></script>
```

## Varför den finns

Innan detta hade memaix.se/boka och jimlov.se/boka varsin handkopierad version
av samma flöde. Två kopior betyder två ställen för en bugg att bo på och inget
ställe att skriva ett test — vilket är precis hur båda sajterna sköt samma
trasiga tidsgrid samma dag. En fil från gatewayen betyder att en rättning når
alla sidor vid deploy, inte vid nästa gång någon kommer ihåg.

Widgeten hämtar **färdiga tider** från `/book/{slug}/times`. Den delar inte
längre upp lediga fönster själv; det gör `booking/slotting.py`, under test.
Se den modulens docstring för tidszons- och sommartidsreglerna.

## Attribut på monteringspunkten

| Attribut | Krävs | Betydelse |
|---|---|---|
| `data-memaix-booking` | ja | Bokningslänkens slug. Publik: den pekar ut en bokningssida, den ger inga rättigheter. |
| `data-lang` | nej | `sv` eller `en`. Utan den läses `<html lang>`, annars engelska. |
| `data-locale` | nej | BCP-47-tagg för datum/tid-formatering, t.ex. `sv-SE`. Utan den följer den `data-lang`. |
| `data-days` | nej | Hur långt fram kalendern visar. Utan den länkens `max_days_ahead`, annars 60 dagar. |
| `data-gateway` | nej | Annan gateway-origin än den script-taggen laddades från. Behövs i praktiken bara vid lokal utveckling. |

Flera widgetar på samma sida fungerar — varje monteringspunkt har eget
tillstånd. Captcha-scriptet laddas en gång oavsett hur många de är.

## Utseende

Widgeten sätter layouten och inget annat. Färger, radie och typsnitt är
CSS-variabler som sidan skriver över på monteringspunkten:

```css
[data-memaix-booking] {
  --mxb-accent: #3D7A8F;   /* knappar, valda tider */
  --mxb-text: #1a1a1a;
  --mxb-muted: #6b7280;
  --mxb-surface: #fff;
  --mxb-border: rgba(0,0,0,0.12);
  --mxb-radius: 10px;
  --mxb-font: inherit;     /* ärver sidans typsnitt som standard */
}
```

Allt är namnrymdat under `.mxb`, så sidans egen CSS-reset når inte in och
widgetens stilar läcker inte ut.

## Konfiguration på bokningslänken

Länkfilen ligger i `config/booking_links/<slug>.json` (se
`booking/links.py` för samtliga fält). De som rör inbäddning:

```json
{
  "project": "proj",
  "user": "alice",
  "duration_min": 30,
  "origins": ["https://kund.example"],
  "turnstile_site_key": "0x4AAA…",
  "consent_text": "Jag samtycker till att …"
}
```

- **`origins`** — vilka sidor som får anropa gatewayen för den här länken.
  Krävs: en webbläsare vägrar annars anropet, och widgeten visar "något gick
  fel" utan att sidan får veta varför. `jimlov.se` och `memaix.se` ligger
  hårdkodade i `booking/routes.py` och behöver inte listas — de är
  hårdkodade just för att ett stavfel i en länkfil inte ska kunna släcka
  bokningen på de sajter som redan är i drift.
- **`turnstile_site_key`** — Turnstiles *site*-nyckel, den publika halvan.
  Utan den vägrar widgeten visa formuläret, för gatewayen verifierar ändå
  token på serversidan och skulle svara 403 vid inskick. Hemligheten sätts
  separat under `memaix.booking.turnstile_secret_ref`. Utelämnas fältet
  används `memaix.booking.turnstile_site_key` för hela gatewayen.
- **`consent_text`** — exakt den text besökaren godkänner. Sparas ordagrant
  med bokningen. Utelämnas den använder widgeten sin egen inbyggda text på
  sidans språk.

## Endpoints widgeten använder

| Metod | Väg | Svar |
|---|---|---|
| `GET` | `/embed/booking.js` | Själva widgeten. `Cache-Control: no-cache` — inbäddaren skriver en versionslös script-tagg en gång och tänker aldrig på den igen, så färskheten måste komma från omvalidering. |
| `GET` | `/book/{slug}/config` | Tidszon, längd, grid, captcha-nyckel, mötesformer. Inget hemligt: mötesformernas egen `config` (t.ex. värdens telefonnummer) utelämnas medvetet. |
| `GET` | `/book/{slug}/times` | Bokningsbara starttider, färdigt uppdelade. |
| `POST` | `/book/{slug}` | Bokningen. |

`/book/{slug}/slots` finns kvar och returnerar råa lediga fönster. Den är
ersatt av `/times` men lever tills båda de befintliga sajterna gått över till
widgeten — dagen `/slots` börjar returnera halvtimmesbitar är dagen båda
sajterna visar ett grid av dubbletter.

## Mötesformer

Har värden konfigurerat flera mötesformer (Google Meet, Zoom, telefon) visar
widgeten en väljare. Har hen en enda visas ingen — ett val med ett alternativ
är inte ett val, och gatewayen väljer ändå standardformen.

## Bevakning

Gatewayen kan själv kontrollera att de publika bokningslänkarna går att boka
och mejla en varning när de inte gör det. En tom bokningssida ser ut som en
fungerande sida för besökaren, så utan bevakning upptäcks felet först när
någon klagar.

| Nyckel under `memaix.booking` | Betydelse |
|---|---|
| `watch_enabled` | `true` startar bakgrundsloopen. Default av. |
| `watch_to` | Mottagarens mejladress. Utan den skickas inget. |
| `watch_project` | Projektet vars mejlkanal (`mailbox`/`smtp`) skickar varningen. |

Loopen kör var 30:e minut och frågar varje länk om bokningsbara tider de
kommande 60 dagarna. Varningen skickas när en länk saknat tider i två
kontroller i rad, eller felar, och upprepas som mest en gång per dygn tills
länken är frisk igen. Mejlet nämner länkens användare och projekt men aldrig
sluggen. Det anger också troliga orsaker, till exempel ett långt
kalenderevent som blockerar allt efter ett visst datum.
