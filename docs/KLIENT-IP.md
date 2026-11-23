# Klient-IP bakom proxy

Gatewayen rate-limitar publika endpoints per klient-IP (`/hooks/{token}`
30/60 s, bokning läs 60/60 s, skapa och hantera 10/60 s). Bakom
Cloudflare -> cloudflared -> Caddy -> gateway är TCP-peern alltid en
container-IP, så utan rätt konfiguration delar alla klienter en hink.

## Hur det fungerar

- Caddy (`caddy/Caddyfile`, `servers`): `trusted_proxies` är cloudflared
  (standard Dockers privata intervall `172.16.0.0/12`; styrs av
  `CADDY_TRUSTED_PROXIES`, som compose skickar in till caddy-tjänsten. Snäva
  gärna in till det exakta Docker-subnätet). Gatewayen får
  `X-Forwarded-For: <ev. klientstyrt>, <riktig klient>, <cloudflared-IP>` och
  läser från höger. `client_ip_headers CF-Connecting-IP` påverkar bara Caddys
  egen `{client_ip}` och loggar, inte vad gatewayen ser.
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
  IPv6-klienter nycklas på sitt /64-nät, så rotation inom prefixet ger ingen ny
  hink. Följd: delade mobilnät/NAT64 med gemensamt /64 kan dela hink.
- Poster med `%` (scope-id), portar eller tomma poster i kedjan räknas som trasiga
  och ger peer-IP.

## Drift

`docker-compose.yml` sätter `FORWARDED_ALLOW_IPS` till `172.16.0.0/12` (override
i `.env`). Snäva in till nätets subnät om det är känt. Gatewayens port 8080 är
bara publicerad på 127.0.0.1; lokala anrop via Docker-bryggan räknas som betrodd
peer och kan alltså sätta X-Forwarded-For. Skydda aldrig interna endpoints enbart
med IP.

Efter ändring: starta om gateway och caddy (`docker compose up -d gateway caddy`),
verifiera i gatewayens logg/test att två olika klienter får olika hinkar.
