"""Memaix lokal setup-webb — first-boot-wizarden i webbläsaren.

SPDX-License-Identifier: AGPL-3.0-or-later

Startas av setup.sh / setup.ps1, aldrig direkt exponerad. Säkerhetsmodellen
(SETUP-UI.md, obligatorisk):

1. Binder 127.0.0.1 (eller 0.0.0.0 bakom --container, där containern
   publiceras enbart på värdens 127.0.0.1).
2. Engångstoken krävs på varje request (konstant-tids-jämförelse).
3. Självavstängande: servern stänger av sig när installationen skrivits,
   och vägrar starta om config redan finns (ingen stående yta i drift).
4. Hemligheter går åt ett håll: lösenord/tunnel-token ekas aldrig tillbaka,
   loggas aldrig.
5. Ingen extern frontend: server-renderad HTML, inline CSS, sju rader JS.

Bara stdlib — noll beroenden på värden utöver Python 3.
"""

from __future__ import annotations

import hmac
import html
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_engine as engine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_COOKIE = "memaix_setup"

_CSS = """
body{font-family:-apple-system,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;
     max-width:640px;margin:2rem auto;padding:0 1rem}
h1{font-size:1.4rem} h2{font-size:1rem;margin-top:1.6rem;color:#94a3b8}
fieldset{border:1px solid #2d3044;border-radius:8px;margin:1rem 0;padding:1rem}
label{display:block;margin:.6rem 0 .2rem} small{color:#94a3b8}
input[type=text],input[type=password],input[type=email]{width:100%;padding:.5rem;
     background:#1a1d27;border:1px solid #2d3044;border-radius:6px;color:#e2e8f0}
button{background:#4f46e5;color:#fff;border:0;border-radius:6px;padding:.7rem 1.4rem;
     font-size:1rem;margin-top:1rem;cursor:pointer}
.err{background:#3b1219;border:1px solid #7f1d1d;border-radius:6px;padding:.6rem 1rem;margin:.4rem 0}
code{background:#1a1d27;padding:.15rem .4rem;border-radius:4px}
"""

_TOGGLE_JS = """
function trk(){var t=document.querySelector('input[name=track]:checked').value;
document.getElementById('remote').style.display=(t==='1')?'none':'block';}
document.querySelectorAll('input[name=track]').forEach(function(r){r.onchange=trk});trk();
function ai(){var v=document.querySelector('input[name=llm_choice]:checked').value;
document.getElementById('ai-api').style.display=(v==='api')?'block':'none';
document.getElementById('ai-endpoint').style.display=(v==='endpoint')?'block':'none';}
document.querySelectorAll('input[name=llm_choice]').forEach(function(r){r.onchange=ai});ai();
"""


def _page(title: str, body: str, js: str = "") -> bytes:
    script = f"<script>{js}</script>" if js else ""
    return (
        f"<!doctype html><html lang='sv'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}{script}</body></html>"
    ).encode()


def _form(values: dict, errors: list) -> bytes:
    v = {k: html.escape(str(x)) for k, x in values.items()}
    err_html = "".join(f"<div class='err'>{html.escape(e)}</div>" for e in errors)
    checked = {1: "", 2: "", 3: ""}
    checked[int(values.get("track", 1))] = "checked"
    llm_choice = str(values.get("llm_choice", "byo"))
    if llm_choice not in ("byo", "api", "endpoint"):
        llm_choice = "byo"  # okänt val: felet visas separat, radioknappen faller tillbaka visuellt
    llm_checked = {c: ("checked" if c == llm_choice else "") for c in ("byo", "api", "endpoint")}
    llm_provider_api = v.get("llm_provider", "") if llm_choice == "api" else ""
    llm_model_api = v.get("llm_model", "") if llm_choice == "api" else ""
    llm_endpoint_v = v.get("llm_endpoint", "") if llm_choice == "endpoint" else ""
    llm_model_endpoint = v.get("llm_model", "") if llm_choice == "endpoint" else ""
    body = f"""
{err_html}
<form method="post" action="/apply" autocomplete="off">
<fieldset><h2>1 · Vad vill du göra?</h2>
<label><input type="radio" name="track" value="1" {checked[1]}> Prova lokalt
  <small>— stdio, inget tunnel/OAuth/domän, ~5 min</small></label>
<label><input type="radio" name="track" value="2" {checked[2]}> Self-host (mobil/team)
  <small>— tunnel + OAuth, nås från telefon/team</small></label>
<label><input type="radio" name="track" value="3" {checked[3]}> Installera åt kund
  <small>— som self-host, eget domännamn</small></label>
</fieldset>

<fieldset id="remote"><h2>2 · Domän &amp; tunnel</h2>
<label>Din domän <input type="text" name="domain" value="{v.get('domain','')}"
  placeholder="mcp.företag.se"></label>
<label>Cloudflare tunnel-token <small>(valfri — lägg till senare)</small>
  <input type="password" name="tunnel_token" value=""></label>
</fieldset>

<fieldset><h2>3 · Admin-konto</h2>
<label>Användarnamn <input type="text" name="admin_user" value="{v.get('admin_user','admin')}"></label>
<label>Lösenord <small>(minst 8 tecken)</small>
  <input type="password" name="password" value=""></label>
<label>Bekräfta lösenord <input type="password" name="password2" value=""></label>
</fieldset>

<fieldset><h2>4 · Vilken AI driver Memaix?</h2>
<label><input type="radio" name="llm_choice" value="byo" {llm_checked['byo']}> Min egen AI
  <small>— Claude/ChatGPT/Mistral-appen ansluter till Memaix (BYO, default)</small></label>
<label><input type="radio" name="llm_choice" value="api" {llm_checked['api']}> API-nyckel
  <small>— Claude, ChatGPT, Gemini eller OpenRouter; betala per token</small></label>
<label><input type="radio" name="llm_choice" value="endpoint" {llm_checked['endpoint']}> Egen LLM-server
  <small>— på ditt lokala nät eller en molninstans (Ollama/vLLM/OpenAI-kompatibel)</small></label>
<div id="ai-api" style="display:none">
<label>Leverantör
  <select name="llm_provider_api">
    <option value="anthropic" {"selected" if llm_provider_api == "anthropic" else ""}>Claude (Anthropic)</option>
    <option value="openai" {"selected" if llm_provider_api == "openai" else ""}>ChatGPT (OpenAI)</option>
    <option value="google" {"selected" if llm_provider_api == "google" else ""}>Gemini (Google)</option>
    <option value="openrouter" {"selected" if llm_provider_api == "openrouter" else ""}>OpenRouter — en nyckel, alla leverantörer</option>
    <option value="mistral" {"selected" if llm_provider_api == "mistral" else ""}>Mistral</option>
  </select></label>
<label>Modellnamn <input type="text" name="llm_model_api" value="{llm_model_api}"
  placeholder="claude-sonnet-4-5 / gpt-… / gemini-…"></label>
<label>API-nyckel <input type="password" name="llm_api_key"></label>
</div>
<div id="ai-endpoint" style="display:none">
<label>Endpoint-URL <input type="text" name="llm_endpoint" value="{llm_endpoint_v}"
  placeholder="http://192.168.1.20:11434 eller https://din-molninstans:8000"></label>
<label>Modellnamn <input type="text" name="llm_model_endpoint" value="{llm_model_endpoint}"
  placeholder="qwen3-coder:30b / mistral-small …"></label>
</div>
</fieldset>

<fieldset><h2>5 · Valfritt <small>(bra defaultvärden)</small></h2>
<label>Produktnamn <input type="text" name="name" value="{v.get('name','Memaix')}"></label>
<label>Support-mejl <input type="email" name="support_email"
  value="{v.get('support_email','support@example.com')}"></label>
<label>Första projektet <input type="text" name="project_name"
  value="{v.get('project_name','shared')}"></label>
</fieldset>

<button type="submit">Generera config &amp; hemligheter</button>
<p><small>Hemligheter genereras på servern och visas aldrig här.
Wizarden stänger av sig själv när den är klar.</small></p>
</form>"""
    return _page("Memaix — installation", body, _TOGGLE_JS)


def _receipt(summary: dict) -> bytes:
    files = "".join(f"<li><code>{html.escape(f)}</code></li>" for f in summary["written"])
    if summary["track"] == engine.TRACK_TRIAL:
        next_step = ("<p>Trial-läge: startskriptet visar nu raden du klistrar in i "
                     "Claude Desktop (lokal MCP).</p>")
    else:
        next_step = (f"<p>Startskriptet reser nu stacken och kör hälsokontrollen. "
                     f"Din connector-URL: <code>{html.escape(summary['public_url'])}</code></p>")
    notice = f"<p><strong>{html.escape(summary['notice'])}</strong></p>" if summary.get("notice") else ""
    body = (f"<p>✓ Config och hemligheter genererade:</p><ul>{files}</ul>"
            f"{notice}{next_step}"
            f"<p><small>Setup-webben är nu avstängd — den här fliken kan stängas. "
            f"Kör om installationen: starta setup-skriptet igen.</small></p>")
    return _page("Memaix — klart", body)


class Handler(BaseHTTPRequestHandler):
    token = ""          # sätts av main()
    done = False

    def log_message(self, fmt, *args):  # aldrig query-strängar (token) i loggen
        sys.stderr.write(f"setup-web: {self.command} {urlparse(self.path).path}\n")

    def _authed(self) -> bool:
        qs = parse_qs(urlparse(self.path).query)
        candidate = qs.get("token", [""])[0]
        if not candidate:
            cookies = self.headers.get("Cookie", "")
            for part in cookies.split(";"):
                if part.strip().startswith(_COOKIE + "="):
                    candidate = part.strip()[len(_COOKIE) + 1:]
        return bool(candidate) and hmac.compare_digest(candidate, self.token)

    def _send(self, code: int, body: bytes, set_cookie: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        if set_cookie:
            self.send_header("Set-Cookie", f"{_COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self) -> None:
        self._send(403, _page("Memaix", "<p>Ogiltig eller saknad setup-token. "
                                        "Använd länken som setup-skriptet skrev ut.</p>"))

    def do_GET(self) -> None:
        if Handler.done:
            self._send(410, _page("Memaix", "<p>Installationen är klar — setup-webben är stängd.</p>"))
            return
        if not self._authed():
            self._deny()
            return
        self._send(200, _form(engine.defaults(), []), set_cookie=True)

    def do_POST(self) -> None:
        if Handler.done or urlparse(self.path).path != "/apply" or not self._authed():
            self._deny()
            return
        length = min(int(self.headers.get("Content-Length", 0)), 65536)
        fields = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        f = {k: v[0] for k, v in fields.items()}

        answers = engine.defaults()
        answers.update({
            "track": int(f.get("track", "1") or "1"),
            "name": f.get("name", "").strip() or "Memaix",
            "support_email": f.get("support_email", "").strip() or "support@example.com",
            "domain": f.get("domain", "").strip(),
            "admin_user": f.get("admin_user", "").strip(),
            "password": f.get("password", ""),
            "project_name": f.get("project_name", "").strip() or "shared",
            "tunnel_token": f.get("tunnel_token", ""),
        })
        if answers["track"] != engine.TRACK_TRIAL:
            answers["tunnel_provider"] = "cloudflare" if answers["tunnel_token"] else "none"

        llm_choice = f.get("llm_choice", "byo")
        if llm_choice == "byo":
            pass
        elif llm_choice == "api":
            answers["llm_provider"] = f.get("llm_provider_api", "").strip()
            answers["llm_model"] = f.get("llm_model_api", "").strip()
            answers["llm_api_key"] = f.get("llm_api_key", "").strip()
        elif llm_choice == "endpoint":
            # Ollama-endpoints är vanligast lokalt; vLLM/annat är OpenAI-kompatibelt.
            # Providern justeras enkelt i /app/admin efteråt — URL:en är det viktiga.
            answers["llm_provider"] = "openai-compatible"
            answers["llm_endpoint"] = f.get("llm_endpoint", "").strip()
            answers["llm_model"] = f.get("llm_model_endpoint", "").strip()
        else:
            # Okänt val ska ge ett tydligt fel, aldrig en tyst fallback till BYO.
            answers["llm_provider"] = "__unknown__"

        errors = engine.validate(answers)
        if answers["llm_provider"] == "__unknown__":
            errors.insert(0, "Okänt AI-val.")
        if f.get("password") != f.get("password2"):
            errors.insert(0, "Lösenorden matchar inte.")
        if errors:
            safe = {k: v for k, v in answers.items()
                    if k not in ("password", "tunnel_token", "llm_api_key")}
            safe["llm_choice"] = llm_choice
            self._send(200, _form(safe, errors))
            return

        summary = engine.write_config(answers, ROOT)
        engine.seed_vaults(ROOT, [answers["project_name"]])
        (ROOT / ".setup-result.json").write_text(json.dumps(summary, ensure_ascii=False))
        Handler.done = True
        self._send(200, _receipt(summary))
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", required=True)
    p.add_argument("--container", action="store_true",
                   help="binder 0.0.0.0 (containern publiceras på värdens 127.0.0.1)")
    p.add_argument("--force", action="store_true", help="tillåt körning trots befintlig config")
    args = p.parse_args()

    if (ROOT / "config" / "acl.yaml").exists() and not args.force:
        print("setup-web: config/acl.yaml finns redan — vägrar starta (kör med --force "
              "för att skriva över).", file=sys.stderr)
        return 1

    Handler.token = args.token
    host = "0.0.0.0" if args.container else "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"setup-web: lyssnar på http://127.0.0.1:{args.port}/ (avslutas när setup är klar)")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
