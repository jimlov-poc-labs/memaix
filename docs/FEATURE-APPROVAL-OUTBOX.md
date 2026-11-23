# Funktion #3 — Utkorg med bekräftelse (säker autonomi)

SPDX-License-Identifier: AGPL-3.0-or-later

Designdok + byggspec för en **utkorg**: allt som lämnar systemet (skicka mejl,
skapa/ändra kalenderhändelse, framtida webhooks) hamnar i en kö med
förhandsvisning och kräver ett mänskligt **godkänn** innan det utförs. Det gör
det tryggt att låta assistenten göra mer — och neutraliserar den största
säkerhetsrisken: prompt injection som exfiltrerar via assistentens egna förmågor
(`THREAT-MODEL.md`).

Byggs stegvis enligt [Byggordning](#byggordning) och
[Utvecklingsinstruktioner](#utvecklingsinstruktioner).

---

## 1. Vad användaren upplever

Assistenten "skickar" ett mejl → istället för att gå iväg direkt dyker det upp i
**Utkorgen** (i board:en och som notis via funktion #1) med hela innehållet
synligt: mottagare, ämne, text. Ett klick **Godkänn** skickar; **Avvisa** slänger.
Användaren kan tryggt säga *"förbered svaren"* utan att förlora kontroll över vad
som faktiskt lämnar systemet.

Läsande/utkast-åtgärder påverkas inte — bara sådant som är **utgående eller svårt
att ångra**.

---

## 2. Nyckelbeslut

1. **Gate vid tool-lagret.** Utgående verktyg (`email_send`, `calendar_create`,
   `calendar_update`, framtida webhooks) kollar en policy: om projektets läge är
   `review` → köa istället för att utföra, returnera `{pending: True, action_id}`.
   Läge `auto` behåller dagens beteende.
2. **Godkännande utförs av behörig användare.** Samma roll som verktyget kräver
   (t.ex. `email_send` = `owner`). Godkännarens identitet loggas (oavvislighet).
3. **Exekvering återanvänder samma kod.** Vid godkännande körs samma
   tool-funktion med en `_confirmed=True`-flagga som passerar gaten — ingen
   dubbelimplementation, ingen re-köning.
4. **Idempotent.** Ett godkännande utför åtgärden högst en gång (status-maskin +
   lås). Ett redan avgjort ärende kan inte avgöras igen.
5. **Mottagar-allowlist (valfri).** Även i `auto`-läge tvingas åtgärder till
   ej-listade mottagare/URL:er genom utkorgen. Exfiltreringsskydd.
6. **Utgång.** Ej avgjorda ärenden förfaller efter N timmar (default 72) →
   status `expired`.

---

## 3. Översikt

```
  email_send / calendar_create / calendar_update            (tool-lager)
        │
        ▼
  gate: policy(project, tool, args)  ── auto ──►  utför direkt (som idag)
        │ review  (eller ej-allowlistad mottagare)
        ▼
  ActionQueue.enqueue → pending_actions (SQLite)  → {pending, action_id}
        │
        │  (användaren ser i board/utkorg + notis via #1)
        ▼
  outbox_approve(id) ──► lås + status→approved ──► execute_pending()
        │                                              │  fn(..., _confirmed=True)
        ▼                                              ▼
  outbox_reject(id) ──► status→rejected           resultat sparas, audit-loggas
```

---

## 4. Datamodell

Ny SQLite-DB via env `MEMAIX_OUTBOX_DB` (default `/tmp/memaix-outbox.db`).

```sql
CREATE TABLE IF NOT EXISTS pending_actions (
    id          TEXT PRIMARY KEY,          -- uuid4 hex
    memaix_user TEXT NOT NULL,             -- vem som initierade
    project     TEXT NOT NULL,
    tool        TEXT NOT NULL,             -- 'email_send' | 'calendar_create' | ...
    args_json   TEXT NOT NULL,             -- JSON av verktygsargumenten
    preview     TEXT NOT NULL,             -- människoläsbar sammanfattning
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|executed|failed|expired
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    decided_by  TEXT,                      -- vem som godkände/avvisade
    decided_at  TEXT,
    result_json TEXT,                      -- verktygets returvärde eller felmeddelande
    reason      TEXT                       -- avvisningsorsak
);
CREATE INDEX IF NOT EXISTS idx_pending_scope ON pending_actions(project, status);
```

Statusmaskin: `pending → approved → executed` (eller `→ failed`),
`pending → rejected`, `pending → expired`. Övergångar från icke-`pending` är
förbjudna (returnera konflikt).

---

## 5. Policy

`outbox/policy.py`:

```python
def action_mode(cfg, acl, project, tool, args) -> str:
    """Returnera 'auto' eller 'review' för denna åtgärd."""
```

Källor (mest restriktiv vinner):
- Projektets `outbox`-läge i `acl.yaml`/`memaix.yaml` (`auto` default, `review`
  för att gate:a allt utgående).
- Global default i `memaix.yaml` (`memaix.outbox.default_mode`).
- **Allowlist-override:** om åtgärdens mottagare/URL inte finns i projektets
  `allowlist` → tvinga `review` oavsett läge.

Gatade verktyg och deras "mottagare" för allowlist-koll:
| tool | mottagare att kolla |
|------|--------------------|
| `email_send` | `to` + `cc` (e-postdomän/adress) |
| `calendar_create`/`calendar_update` | `attendees` (om några) |
| (framtid) `webhook_call` | mål-URL:s host |

---

## 6. Kö och exekvering

`outbox/queue.py` — `ActionQueue` (SQLite, samma mönster som övriga stores):

```python
class ActionQueue:
    def enqueue(self, user, project, tool, args: dict, preview: str, ttl_h=72) -> str  # returns id
    def get(self, action_id) -> dict | None
    def list(self, projects: list[str], status: str | None = None) -> list[dict]
    def claim_for_decision(self, action_id, decision: str, decided_by: str) -> dict | None
        # atomiskt: UPDATE ... SET status=? WHERE id=? AND status='pending'
        # rowcount==0 → redan avgjort (returnera None → konflikt)
    def record_result(self, action_id, status: str, result: dict) -> None
    def expire_due(self, now_iso: str) -> int
```

`outbox/execute.py` — `execute_pending(acl, action) -> dict`:
dispatch-tabell `tool → callable`, t.ex.
`{"email_send": t_email.email_send, "calendar_create": t_cal.calendar_create, ...}`.
Anropa med sparade args + `_confirmed=True`. Fånga fel → `status='failed'`,
spara felet. Audit-logga (`tool=f"outbox_execute:{tool}"`, ok).

**Preview-byggare** `outbox/preview.py` — `render_preview(tool, args) -> str`:
- email: `Till: … / Ämne: … / <de första raderna av body>`
- calendar: `Händelse: <titel> <start>–<end>, deltagare: …`

---

## 7. Tool-integration (gaten)

Lägg en `_confirmed: bool = False`-parameter (keyword-only) på de gatade
funktionerna och en gate i början:

```python
def email_send(acl, user_id, project, to, subject, body, cc=None,
               *, _smtp=None, _confirmed=False, _outbox=None, _cfg=None):
    acl.enforce(user_id, project, "owner")
    if not acl.resource(project, "allow_send"):
        raise RuntimeError("feature_disabled: allow_send is false")
    if not _confirmed and action_mode(_cfg, acl, project, "email_send", {...}) == "review":
        aid = (_outbox or _get_outbox()).enqueue(
            user_id, project, "email_send",
            {"to": to, "subject": subject, "body": body, "cc": cc},
            render_preview("email_send", {...}),
        )
        return {"pending": True, "action_id": aid,
                "note": "Väntar på godkännande i utkorgen"}
    # ... befintlig sändningslogik ...
```

Samma mönster för `calendar_create`/`calendar_update`. `_outbox`/`_cfg`
injiceras i test; i produktion hämtas via lata gettrar i `server.py`.

> Backwards-compat: default `default_mode='auto'` ⇒ befintliga tester och flöden
> är oförändrade tills en operatör slår på `review`.

---

## 8. MCP-yta och board

Nya verktyg i `server.py` (via `_tool_call`, projekt-scope från argument):

| Verktyg | Signatur | Roll |
|---------|----------|------|
| `outbox_list` | `(project: str\|None=None, status: str="pending")` | reader ser sina synliga projekt |
| `outbox_get` | `(action_id: str)` | som list |
| `outbox_approve` | `(action_id: str)` | roll som gatade verktyget kräver (t.ex. owner för email_send). **Mejlundantag:** köade `email_send`/`email_forward`/`email_reply` nekas alltid över MCP (`approval_web_only`); de godkänns bara i webben (sessionscookie) |
| `outbox_reject` | `(action_id: str, reason: str="")` | samma |

`approve` gör: `claim_for_decision(id, 'approved', user)` → om None: returnera
`{"conflict": True}`; annars `execute_pending` → `record_result` → returnera
resultatet. `list`/`get` ACL-filtreras på `visible_projects`.

**Board** (`board/routes.py`): ny route `GET /board/api/outbox?project=…` (lista)
och `POST /board/api/outbox/{id}` med `{decision: approve|reject, reason}`.
Godkännande enforce:ar `owner` (som board-PATCH i funktion #4/säkerhetsfixen).
Ny "Utkorg"-kolumn/panel i `board.html`. Notis via funktion #1 när något köas.

---

## 9. Säkerhet & integritet

- **Godkännare måste ha rätt roll** för den gatade åtgärden; identitet + tidpunkt
  loggas (oavvislighet).
- **Allowlist** för mottagare/URL:er per projekt stänger exfiltrering även i
  `auto`-läge.
- **Ingen bypass:** exekvering sker bara via `execute_pending` efter en godkänd
  status-övergång; `_confirmed=True` sätts aldrig av klient-input, bara internt.
- **Idempotens** via `claim_for_decision` (compare-and-set) — dubbelklick/retry
  utför en gång.
- **Utgång** hindrar att gamla, kanske manipulerade, ärenden ligger kvar och
  godkänns långt senare.
- Logga aldrig hela body/hemligheter i audit — bara `tool`, `action_id`, ok.
  Preview lagras i outbox-DB (samma skydd som vaulten).

---

## Byggordning

1. **ActionQueue** (`outbox/queue.py`) — SQLite + statusmaskin. *Isolerat testbart.*
2. **Preview** (`outbox/preview.py`) — rena renderare per tool.
3. **Policy** (`outbox/policy.py`) — `action_mode` + allowlist.
4. **Execute** (`outbox/execute.py`) — dispatch + `_confirmed`.
5. **Gate i verktygen** — `email_send`, `calendar_create/update`.
6. **MCP-yta** (`server.py`) — outbox_list/get/approve/reject + lata gettrar.
7. **Board** — API-routes + UI-panel + notis-koppling.
8. **Config + docs** — lägen/allowlist, INDEX, DEVELOPMENT-PROPOSALS-status.
9. **CI** — testsviten grön.

---

## Utvecklingsinstruktioner

Konventioner: se funktion #1-doket. Kör `python -m pytest -q` från `gateway/`.

### Steg 1 — `outbox/queue.py`
Paket `gateway/src/memaix_gateway/outbox/__init__.py` + `ActionQueue` enligt §6.
`claim_for_decision` är hjärtat: `UPDATE pending_actions SET status=?,
decided_by=?, decided_at=? WHERE id=? AND status='pending'`; returnera raden om
`rowcount==1` annars None. **Test** (`tests/test_outbox_queue.py`): enqueue+get;
list filtrerar på status/projekt; `claim_for_decision` True första gången, None
andra (idempotens); `expire_due` sätter `expired` på gamla `pending`.

### Steg 2 — `outbox/preview.py`
`render_preview(tool, args) -> str` för `email_send` och `calendar_create/update`.
**Test** (`tests/test_outbox_preview.py`): innehåller mottagare/ämne resp.
titel/tid; trunkerar långt innehåll.

### Steg 3 — `outbox/policy.py`
`action_mode(cfg, acl, project, tool, args)` och `_recipients(tool, args)` +
allowlist-koll. **Test** (`tests/test_outbox_policy.py`): default `auto`;
projekt-`review` ger review; ej-allowlistad mottagare tvingar review även i auto.

### Steg 4 — `outbox/execute.py`
`execute_pending(acl, action, *, tools=None)` med dispatch-tabell; anropa med
`_confirmed=True`. **Test** (`tests/test_outbox_execute.py`): injicera fejk-tool,
verifiera att den anropas med rätt args + `_confirmed=True`; fel → `failed` +
felmeddelande i result.

### Steg 5 — Gate i verktygen
Lägg `_confirmed`/`_outbox`/`_cfg` på `email_send`, `calendar_create`,
`calendar_update` och gate-logiken (§7). **Test:** befintliga email/calendar-
tester passerar (auto default). Nya: `review`-läge → `email_send` returnerar
`{pending, action_id}` och skickar *inte* (verifiera att `_smtp` ej anropades);
allowlist-override köar även i auto.

### Steg 6 — MCP-yta i `server.py`
Lata `_get_outbox()`. Verktygen `outbox_list/get/approve/reject`. `approve`
enforce:ar rätt roll (map `tool→need`), kör `execute_pending`, hanterar konflikt.
**Test** (`tests/test_server.py`): köa via `email_send` (review), `outbox_list`
visar den, `outbox_approve` utför (fejk-smtp) och andra approve ger konflikt;
`reject` sätter rejected och utför inte.

### Steg 7 — Board
Routes `GET /board/api/outbox` + `POST /board/api/outbox/{id}` (enforce owner för
approve). UI-panel i `board.html`. Koppla notis via funktion #1 vid enqueue
(om #1 finns; annars no-op-hook). **Test:** board-routes med cookie-auth →
lista/godkänn; reader kan inte godkänna.

### Steg 8 — Config + docs
`config/acl.example.yaml`: per-projekt `outbox: review|auto` + `allowlist:`.
`config/memaix.example.yaml`: `memaix.outbox.default_mode`, `expire_hours`.
Registrera doket i `docs/INDEX.md` (gjort); uppdatera `DEVELOPMENT-PROPOSALS.md`.

### Steg 9 — Kör allt
`cd gateway && python -m pytest -q` + `python3 scripts/check-docs-index.py`.

### Acceptanskriterier
- [ ] I `review`-läge returnerar `email_send`/`calendar_create` `{pending, action_id}` och utför inget förrän godkänt.
- [ ] `outbox_approve` av behörig användare utför exakt en gång; andra försök ger konflikt; `reject` utför aldrig. Undantag: mejlverktyg nekas över MCP och godkänns bara via webb-API:t.
- [ ] Ej-allowlistad mottagare tvingas genom utkorgen även i `auto`-läge.
- [ ] Reader kan lista sina synliga projekts ärenden men inte godkänna utgående som kräver owner.
- [ ] Utgångna ärenden markeras `expired` och kan inte godkännas.
- [ ] `auto`-default gör att befintliga flöden/tester är oförändrade.
- [ ] Godkännarens identitet + tidpunkt loggas; inga hemligheter/body i audit; hela sviten + docs-index grön.

---

## Framtida arbete
- Batch-godkännande och "godkänn allt från den här sessionen".
- Signerad godkännandelänk i notisen (funktion #1) för ett-klicks-godkänn utan att öppna board:en.
- Diff-vy för `calendar_update` (före/efter).
- Koppling till funktion #4: regelutlösta utgående åtgärder går alltid genom utkorgen.
- Policy per verktyg (inte bara per projekt) och tidsfönster ("auto dagtid, review kväll").
