# HealthLogHistory

Log history about health by send text from whatsapp and spreadsheet.

Turns a WhatsApp message like this:

```
date: 2026-10-19
blood sugar: 100
time check: 08.08 am
notes: it's normal after breakfast
```

into a row in a Google Sheet, and replies with a receipt so a misread number is
visible immediately.

## Tech stack and why

| Layer | Choice | Why this one |
|---|---|---|
| Transport | **Baileys** (`@whiskeysockets/baileys` 6.7.24) | No Meta app review, no per-message fee, works with a personal number. See the warning below. |
| Runtime | **Node 22 + TypeScript**, run via `tsx` | Baileys needs a long-lived socket, so serverless is out. Node 22 has a stable `fetch` and `node:test`, so there is no test framework to add. |
| Parse | **Deterministic line parser** | A misread blood glucose value is a health-data error. Regex cannot hallucinate; an LLM can. Ambiguity becomes a warning, not a guess. |
| Write | **Google Sheets API v4** (`spreadsheets.values.append`) | Append-only, no shared mutable state, and the exact HTTP request can be replayed from Postman. An Apps Script web app is fewer moving parts but much harder to test outside Google. |
| Idempotency | **JSON file keyed by message id** | Redelivery is a certainty, not an edge case. Swap for SQLite/Redis if more than one process ever runs. |
| Contract | **`openapi.yaml` + Postman collection** | The pipeline is exercisable without a logged-in WhatsApp session. |

### Why the contract is the message event, not the WhatsApp payload

`src/types.ts` defines `CanonicalMessage`. Only `src/whatsapp.ts` knows about
Baileys. Everything downstream — parse, dedupe, append — is driven by that
shape, which is why `npm run smoke` can prove the whole write path without
WhatsApp running.

That also makes the migration path cheap. Moving to the official Cloud API means
writing one new mapping from Meta's webhook payload into `CanonicalMessage` and
calling `ingest()`. Nothing else changes. That matters, because the transport is
the part most likely to break.

## How it fits together

```
Baileys socket ──> messages.upsert ──> CanonicalMessage ──┐
                                                          ├─> dedupe ─> parse ─> Sheets append ─> receipt reply
POST /ingest (Postman, smoke) ────> CanonicalMessage ─────┘
```

`src/pipeline.ts` is the single write path for both entry points, so a bug
reproduced over HTTP is the same bug that would hit a real message.

## Setup

```bash
npm install
cp .env.example .env
```

### Finding your spreadsheet (SHEET_ID)

A service account is a **separate robot identity**. It cannot see your Google
Drive, and it only sees files you explicitly share with its own address.
Sharing a sheet with your personal Google account grants it nothing.

So there are two things to get right, and `npm run sheet:check` confirms both:

```bash
npm run sheet:check
```

1. **The ID.** Open your spreadsheet in the browser and read it out of the URL:

   ```
   https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit#gid=0
                                         ^--------- SHEET_ID ---------^
   ```

   Put that in `.env` as `SHEET_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz`.

2. **The share.** Run `npm run sheet:check` and it prints the service account's
   `client_email`. Share the spreadsheet with that exact address as **Editor**
   (Share → paste the address → Editor → Send). A service account cannot accept
   the invite; it just gains access.

Then run it again. It reads the spreadsheet back and reports the **title**, the
tab names, and whether `SHEET_TAB` exists - so you can see you are pointed at the
sheet you think you are, not a similarly-named one. It also prints the exact
write URL and tells you if the header row matches `COLUMNS`.

If `SHEET_TAB` does not exist, **it is created on the next `npm start`** along
with the header row. That matters because a brand-new Google Sheet has a single
tab called `Sheet1`, not `Log`, so a blank spreadsheet would otherwise fail on
the first write.

A 403 that arrives *after* a successful read means the share level is wrong: set
the service account to **Editor**, not Viewer or Commenter — those cannot append
rows. A 403 on everything means the sheet was never shared or went to a different
address. A 404 means the ID is wrong or the key is from a different project.

## Running

```bash
npm start                 # HTTP + WhatsApp
npm run dev               # same, with reload
npm test                  # parser unit tests
npm run smoke             # end-to-end checks against a running instance
npm run sheet:check       # which spreadsheet is configured, and can it be reached
```

Starts in **dry-run** by default: rows are parsed and logged but not written.
That is deliberate — verify the parse output before granting write access. Set
`DRY_RUN=false` and a `SHEET_ID` to go live.

To work on the pipeline without WhatsApp:

```bash
set WHATSAPP_ENABLED=false && npm start
npm run smoke
```

To connect WhatsApp, leave `WHATSAPP_ENABLED=true` and scan the QR code printed
on first start.

## Working on the API in Postman

`postman init` registered `openapi.yaml` as the authoritative spec and created
the local artifacts alongside the code:

```
postman/
├── collections/whatsapp-health-log/   # 5 requests, 21 assertions
│   ├── health check.request.yaml
│   ├── ingest reference message.request.yaml
│   ├── ingest unparsable message.request.yaml
│   ├── reject empty body.request.yaml
│   └── reject malformed json.request.yaml
└── environments/Local.environment.yaml
```

The target address lives in the environment, not the collection, so pointing the
same assertions at a deployed instance later is a one-line change:

```bash
npm run verify:api        # the collection run below
postman collection lint postman/collections/whatsapp-health-log
```

Saved examples for each outcome (`appended`, `duplicate`, `unparsed`, `400`)
double as documentation and as inputs for mocking, so a future consumer can see
what a response looks like without running anything.

Two rules worth keeping:

- `{{variables}}` must be single-quoted in YAML, and JSON bodies belong in `|-`
  block scalars. Do not put `base_url` in the collection as well as the
  environment — competing values are how a run silently targets the wrong host.
- The idempotency check in `ingest reference message` sets `message_id` in a
  pre-request script instead of using `{{$guid}}`. A dynamic variable
  re-resolves on the replay, which appends for real and makes a broken dedupe
  check look like a passing one.

## Sheet layout

`src/sheets.ts` owns `COLUMNS`. On start it creates the tab if it is missing and
writes the header row if the tab is empty, so a blank spreadsheet needs no manual
preparation:

| received_at | measured_at | time_check_local | blood_sugar | unit | notes | sender_id | sender_name | wa_message_id | raw_message |

`measured_at` is UTC (derived from the date plus `time check` and
`TZ_OFFSET_MINUTES`; `420` = UTC+7). `wa_message_id` is the idempotency key, and
`raw_message` is kept so history can be re-parsed after a parser fix.

## Parsing rules

- Keys are case-insensitive and accept a few aliases (`sugar`, `gds`,
  `tanggal`, `catatan`). Unknown keys are reported, not silently dropped.
- `time check` accepts `08.08 am`, `8.08 a.m.`, `8.08pm`, `20:08`.
- Without am/pm the value is read literally **and a warning is returned** —
  `08.08` is genuinely ambiguous.
- Without a unit, a value under 40 is read as mmol/L and anything else as
  mg/dL, with a warning either way. mg/dL and mmol/L ranges do not overlap.
- A missing `date` falls back to the message timestamp, which is more reliable
  than the server clock.
- Free-form text is rejected with the expected format rather than guessed at.

## Known limitations

- **The unofficial transport is a real risk.** Baileys logs in as a linked
  device on your own account. This violates WhatsApp's Terms of Service and
  accounts do get banned. Treat the number as expendable. It also depends on an
  undocumented protocol and will break when WhatsApp changes something. It is
  not a foundation for a product or for anything you cannot re-create.
- **`.state/` is a credential.** The Baileys session directory grants full
  access to the logged-in account. It is gitignored; keep it that way.
- **No retry queue.** A Sheets outage returns `error` and the message id is not
  recorded, so a *manual* resend works, but nothing retries automatically. Add a
  queue if this becomes more than a personal log.
- **Single process.** The JSON dedupe store is not safe across processes.
- **No auth on `/ingest`.** It binds to `127.0.0.1` only. Do not expose it
  without adding a shared secret.

## Verified

- `npm test` — 13/13 parser and row-layout tests pass.
- `npm run smoke` — 13/13 checks pass against a live instance in dry-run:
  append, timezone conversion, duplicate suppression, shorthand body, free-form
  rejection, 400/404 handling.
- `npm run verify:api` — 21/21 collection assertions pass across 5 requests,
  including the idempotency replay, against that same instance.
- `postman collection lint` and `postman environment lint` — 0 errors, 0
  warnings.

**Not verified:** live Google Sheets writes (needs a service account and a
shared spreadsheet) and the Baileys transport itself (needs a QR scan and a real
message). Both are the untested parts of the path — the parse, dedupe, and
contract layers are covered above.
