
# Importing a JMeter `.jmx` into Postman

> Reference for the `performance-testing` skill. Run the importer, review the report, then the result is an ordinary performance run.

Turn a JMeter test plan into three artifacts:

1. **Postman Collection v2.1** — requests, flow, headers, auth, variables, extractors, assertions.
2. **Performance config** — VU count, duration, load profile, data file, `--pass-if`, and the exact `postman performance run` command.
3. **Import report** — what imported exactly, what was approximated, what needs manual work, what is unsupported.

The parsing and building are **deterministic Python** (`scripts/`). You (the model) only do the judgment calls the scripts flag: translating Groovy/BeanShell scripts to JavaScript and resolving extractors Python can't (XPath/CSS/complex JSONPath).

## When NOT to guess

Postman performance runs do not support everything JMeter does. The scripts already encode the gaps (see `postman-perf-capabilities.md`). Never invent a Postman feature to cover a JMeter one. If something can't import, it goes in the report's "Needs manual work" or "Not supported" section — that is a correct outcome, not a failure.

## Correctness rules the importer reproduces (do not regress)

These are JMeter semantics that are easy to get subtly wrong. They're covered by `tests/score.py`:

- **Implicit HTTP success** — JMeter fails a sample on HTTP ≥ 400 even with no assertion. Each request gets a default `pm.test` asserting the code is 200–399, **unless** the plan already checks the status code or sets *assume_success* (Ignore Status).
- **Port 0 / -1** → "use protocol default" → omitted from the URL.
- **Full-URL path** — if `HTTPSampler.path` is itself an absolute URL, it's used verbatim; domain/port are ignored.
- **HEAD / TRACE arguments** — JMeter does not send separate parameters as a query for these; they're dropped (reported).
- **Duplicate headers** — in-scope header managers merge; the nearer/last value per name wins and the collapse is reported.
- **Sampler-scoped config** — a Header Manager / Auth Manager / HTTP Defaults inside a request's own subtree applies to that request.
- **Security** — the parser refuses DTD/ENTITY (XXE) and caps input at 25 MiB.

## Workflow

### 1. Run the importer

```bash
python3 scripts/jmx_to_postman.py INPUT.jmx -o OUTDIR --json-report
# CI gate: add --fail-on-blockers to exit non-zero when any item needs manual work
```

Produces in `OUTDIR`:
- `<name>.postman_collection.json` — the collection (import-ready)
- `<name>.perf.md` / `<name>.perf.json` — performance config + CLI command
- `<name>.report.md` — the import report
- `<name>.ir.json` — machine-readable intermediate model + `manual_tasks` + `warnings` (from `--json-report`)

Python 3.8+ only, standard library — no pip installs.

### 2. Resolve the manual tasks

Open `<name>.ir.json` and read `manual_tasks`. Each has `request`, `kind`, `detail`. Handle by kind:

- **`script`** — a JSR223/BeanShell pre/post-processor was auto-translated to JS and wrapped in an IIFE. Open that request's `event` script in the collection, read the `// REVIEW` block, and correct the translation. Use `script-translation.md` for the mapping. Common fixes: `prev`/`ctx`/`sampler` accessors, `.each{}` loops, `import`/`new` statements.
- **`script_sampler`** — a standalone JSR223/BeanShell *sampler* (no HTTP). The stub is a GET to an empty URL carrying the translated script. Decide with the user: delete it, fold its logic into an adjacent request's pre/post script, or replace with a real request.
- **`xpath` / `css`** — the Postman sandbox has no built-in XPath/CSS selector engine. Rewrite the extractor against the JSON body if the response is JSON, or use `xml2js`/`cheerio` via `require(...)` in the script (confirm availability), or drop it with a note.
- **`jsonpath` too complex** — the path used wildcards/filters/recursion. Rewrite it as explicit property access in the request's test script.

Edit the collection JSON in place. Keep the `// REVIEW` comments until the logic is confirmed.

### 3. Validate the output

Confirm the collection is valid v2.1 JSON and — the part that matters — that **every generated `event` script parses as JavaScript** (`node --check` each `exec`, or equivalent). A syntax error in a generated script is invisible in the file and only surfaces when the perf run executes that request, mid-load. Fix any bad script before handing off.

### 4. Finalise the report

`<name>.report.md` already has the four sections. Add anything you changed while resolving manual tasks so the report matches the collection you're handing off.

### 5. Hand off the perf config

`<name>.perf.md`'s command has a `<COLLECTION_UID>` placeholder — the collection must be uploaded to a workspace first to get a real UID. The `--pass-if` value is a suggestion unless the JMeter plan had a Duration Assertion, so surface the VU/duration/profile/pass-if choices for the user to adjust.

### 6. (optional) Publish to a Postman workspace

Only on explicit user consent — and **never make the user paste a workspace ID**. Pick the destination like this:

- **If the current folder is already a connected workspace** (the usual headless case), use it — `postman workspace push` targets the workspace bound to this directory, no ID. Just tell the user which one.
- **If the user named a workspace** (by name), resolve it with `postman workspace list --json` and match.
- **Otherwise present a choice, don't demand an ID.** Run `postman workspace list --json`, show the **names**, and ask the user to pick one *or* create a new one:
  - **pick existing** → connect it once with `postman workspace pull <id>` (ID taken from the list), then push.
  - **create new** → the CLI has no `create`; make it via MCP `createWorkspace` (or the Postman app), then `pull` and push.
- **Get the imported collection into the bound workspace before pushing.** The importer emits a standalone v2.1 `*.postman_collection.json`; a pulled workspace is a git-native v3 tree. After `pull`, bring the file in with `postman collection migrate <file>.postman_collection.json` into the workspace's collections directory (check `postman collection migrate -h`), then `postman workspace push`. Pushing a folder that has no collection in its tree uploads nothing.
- **Push through the CLI, not MCP.** MCP `createCollection` drops each request's URL (requests land blank); use MCP only if the `postman` binary genuinely can't be installed, and repair URLs afterward.
- Interrogate `postman <cmd> -h` for exact flags — the help is authoritative, this file is not.

Smoke-test with `postman collection run` — the **same runtime that executes the perf test**, not a generic client — so the request is proven under the variable/auth resolution it will actually run under. Then wire the returned UID into the perf command.

## Fallback: when you cannot run Python

If this environment has no code execution, do **not** refuse — import by hand, following the exact same rules the script encodes. Output must be identical in shape (collection v2.1 + perf config + report).

1. **Read first:** `mapping-table.md` (what maps to what), `postman-perf-capabilities.md` (the hard limits), `script-translation.md` (Groovy/BeanShell → JS). These ARE the algorithm.
2. **Parse the JMX by the one structural rule:** every element is followed by a sibling `<hashTree>` holding its children. Walk those pairs. A config element (Header Manager, Auth, HTTP Defaults, extractor, assertion) applies to every sampler in its subtree; the same element inside a sampler's own hashTree applies only to that sampler.
3. **Apply the non-negotiable rules** (same as the script). Use the **exact JS templates in `script-translation.md`** for extractors and assertions so a hand import matches the script output byte-for-byte:
   - `${var}` → `{{var}}`; `${__UUID}`→`{{$guid}}`, `${__time}`→`{{$timestamp}}`, `${__Random(..)}`→`{{$randomInt}}`, `${__RandomString(..)}`→`{{$randomAlphaNumeric}}`.
   - Every request gets a default `pm.test` asserting HTTP 200–399 — **unless** that request has a Response Assertion whose `test_field` is `response_code` **or** `response_message`, **or** any assertion with `assume_success=true`. An assertion on the **body** (`response_data`) does NOT suppress it.
   - Port 0/-1 → omit. Full-URL path (`path` starts with `http://`/`https://`) → use verbatim, ignore domain/port. HEAD/TRACE args → drop + note. Keep inherited headers as-is even on a GET (JMeter still sends them); don't prune.
   - Extractors → `pm.collectionVariables.set` in the test script; assertions → `pm.test`. (Templates in the reference.)
   - Thread group → VUs = `num_threads`; **duration (minutes) = round(`ThreadGroup.duration` seconds ÷ 60), minimum 1**; profile = `ramp-up` if `ramp_time`>0 or a plugin/concurrency group, else `fixed`. Loop counts → duration (there is no iteration count). One `--pass-if` only; a Duration Assertion → `less_than(p95, <ms>)`, else default `less_than(error_rate, 5)`.
   - Timers / concurrent thread groups / non-HTTP samplers → **not supported**; list them in the report, never fake them.
4. **Produce the same files** a run would: the collection JSON, the perf `.md`, and the report with the four sections (imported / approximated / needs manual work / not supported).
5. **State clearly** in your answer that this was a by-hand import (no Python), so it's best-effort and not the deterministic, tested path.

This fallback is lower-reliability than running the script — flag anything you were unsure about.

## Mapping

The full per-element mapping (fidelity + whether the script or the model owns it) is `mapping-table.md` — it's the algorithm for the fallback and the regression contract, so it's kept there, not restated here.

## Scope limits (hard facts, verified against the CLI/engine code)

- Performance runs execute **one collection repeated across VUs**. Multiple thread groups cannot run concurrently in a single run.
- Runs are **time-based**, not iteration-based. JMeter loop counts become a duration.
- `--pass-if` is a **single, non-repeatable** condition. Extra assertions stay as `pm.test` in scripts.
- **No native think time.** JMeter timers are approximated — group-scope ones as `--delay-request`, per-request ones as a pre-request pause — not reproduced exactly. Always reported.
- Only **HTTP(S)** samplers import. JDBC/FTP/LDAP/Java/TCP/Debug/Dummy samplers are reported as skipped.
