# Klient-IP bakom proxy

Gatewayen rate-limitar publika endpoints per klient-IP (`/hooks/{token}`
30/60 s, bokning läs 60/60 s, skapa och hantera 10/60 s). Bakom
Cloudflare -> cloudflared -> Caddy -> gateway är TCP-peern alltid en
container-IP, så utan rätt konfiguration delar alla klienter en hink.

## Hur det fungerar

- Caddy (`caddy/Caddyfile`, `servers`): `trusted_proxies` är cloudflared
  (standard Dockers privata intervall `172.16.0.0/12`, kan bytas via
  `CADDY_TRUSTED_PROXIES` om den variabeln ges till caddy-tjänsten).
  `client_ip_headers CF-Connecting-IP X-Forwarded-For` gör att Caddy bara från
  en betrodd peer tar klientadressen ur CF-Connecting-IP. Från andra peers
  ignoreras båda headrarna.
- Gatewayen (`safety/client_ip.py`, funktionen `client_ip(request)`): används av
  alla ställen som rate-limitar på klient-IP. Läser `X-Forwarded-For` bara när
  peern ligger i `FORWARDED_ALLOW_IPS` (komma-separerade IP/CIDR). Kedjan läses
  från höger; första icke-betrodda adressen är klienten.
- uvicorn körs med `proxy_headers=False` så att adressen bara tolkas på ett ställe.

## Fail closed

- `FORWARDED_ALLOW_IPS` saknas/tom: ingen proxy betrodd, peer-IP används.
- `*` godtas inte (ignoreras med varning).
- Trasig, tom eller spoofad `X-Forwarded-For` ger peer-IP, aldrig en ny hink.
- IPv4-mappade IPv6-adresser (`::ffff:a.b.c.d`) blir samma nyckel som IPv4.
  IPv6 grupperas inte per prefix; en klient med ett helt /64 kan rotera adress.

## Drift

`docker-compose.yml` sätter `FORWARDED_ALLOW_IPS` till `172.16.0.0/12` (override
i `.env`). Snäva in till nätets subnät om det är känt. Gatewayens port 8080 är
bara publicerad på 127.0.0.1; lokala anrop via Docker-bryggan räknas som betrodd
peer och kan alltså sätta X-Forwarded-For. Skydda aldrig interna endpoints enbart
med IP.

Efter ändring: starta om gateway och caddy (`docker compose up -d gateway caddy`),
verifiera i gatewayens logg/test att två olika klienter får olika hinkar.
