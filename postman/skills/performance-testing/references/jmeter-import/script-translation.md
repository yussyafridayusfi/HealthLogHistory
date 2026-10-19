# Translating JSR223 / BeanShell scripts to Postman JavaScript

`scripts/groovy_to_js.py` does a best-effort mechanical pass and the importer wraps every result in an IIFE so it is always syntactically valid. It is **never trusted** — each imported script is flagged as a manual task. This file is how you (the model) finish the job.

## Execution-context map

JMeter runs these inside a sampler's processor. Postman runs them as a request's `prerequest` or `test` event. Equivalents:

| JMeter (Groovy/BeanShell) | Postman JS | Context |
|---|---|---|
| `vars.get("x")` / `vars["x"]` | `pm.variables.get("x")` | both |
| `vars.put("x", v)` | `pm.variables.set("x", v)` | both |
| `props.get/put(...)` | `pm.variables.get/set(...)` (approx) | both |
| `log.info/warn/error(...)` | `console.log/warn/error(...)` | both |
| `prev.getResponseDataAsString()` | `pm.response.text()` | post only |
| `prev.getResponseCode()` | `String(pm.response.code)` | post only |
| `prev.getResponseMessage()` | `pm.response.status` | post only |
| `prev.getTime()` | `pm.response.responseTime` | post only |
| `ctx.getPreviousResult()` | `pm.response` | post only |
| `sampler.getUrl()` | `pm.request.url.toString()` | pre only |
| `sampler.addArgument(k,v)` | `pm.request.url.query.add({key:k,value:v})` or body edit | pre only |
| `System.currentTimeMillis()` | `Date.now()` | both |
| `UUID.randomUUID().toString()` | `require('uuid').v4()` | both |
| `Integer.parseInt(x)` | `parseInt(x)` | both |

## What the mechanical pass cannot do — fix these by hand

- **BeanShell implicit result vars** (`ResponseCode`, `ResponseMessage`, `IsSuccess`, `bsh.args`, `Parameters`). No pm.* equivalent in a processor. For a sampler that sets `IsSuccess=false`, express it as a `pm.test(...)` that fails. For `return X`, delete the return (the IIFE makes it legal, but it's a no-op).
- **Groovy closures / collection methods** (`.each {}`, `.collect {}`, `->`). Rewrite as `for...of` / `.map()` / arrow functions.
- **`import` / `new ClassName()` / Java types** (`StringBuilder`, `SimpleDateFormat`, `JsonSlurper`). Replace with JS: string concat, `Date`, `JSON.parse`. `JsonSlurper().parseText(prev...)` → `pm.response.json()`.
- **`try/catch`, multi-statement logic** — valid JS already; just verify semantics.
- **GString interpolation** `"${x} and ${y}"` → template literal `` `${x} and ${y}` `` **only if** `x`/`y` are JS locals; if they are JMeter vars use `pm.variables.get`.

## Procedure

1. Read the `// --- imported ... (REVIEW) ---` block in the request's event script.
2. Compare against the original `<script>` text in the `.jmx` (the IR keeps the element's name).
3. Rewrite line by line using the table. Preserve variable names so `{{var}}` references elsewhere still resolve.
4. Remove the `// REVIEW` marker once correct.
5. Re-run the node syntax gate from `SKILL.md` step 3.

## Example

JMeter (Groovy post-processor):
```groovy
import groovy.json.JsonSlurper
def json = new JsonSlurper().parseText(prev.getResponseDataAsString())
def token = json.data.token
vars.put("authToken", token)
log.info("got " + token)
```
Postman (test script):
```javascript
const json = pm.response.json();
const token = json.data.token;
pm.collectionVariables.set("authToken", token);
console.log("got " + token);
```

## Canonical JS templates (match the script byte-for-byte in fallback mode)

When importing by hand, emit exactly these so hand output equals the script's. `<default>` empty-string if JMeter has none. Source is `pm.response.text()` normally, or `pm.response.headers.toString()` when the extractor's `useHeaders` is true.

**Regex extractor** (`$n$` template → group `n`):
```javascript
try { var _m = String(pm.response.text()).match(new RegExp("<REGEX>")); pm.collectionVariables.set("<ref>", _m ? _m[<n>] : "<default>"); } catch (e) { pm.collectionVariables.set("<ref>", "<default>"); }
```

**Boundary extractor** (left/right → non-greedy capture):
```javascript
try { var _b = String(pm.response.text()).match(new RegExp("<lboundary>([\\s\\S]*?)<rboundary>")); pm.collectionVariables.set("<ref>", _b ? _b[1] : "<default>"); } catch (e) { pm.collectionVariables.set("<ref>", "<default>"); }
```

**JSON extractor** (`JSONPostProcessor` / plugin). Only **simple** paths (`$.a.b`, `$.a[0].c`) import; `$`/`$.` → `pm.response.json()`:
```javascript
try { pm.collectionVariables.set("<ref>", pm.response.json().a.b[0]); } catch (e) { pm.collectionVariables.set("<ref>", "<default>"); }
```
Wildcards / filters / recursive descent (`*`, `..`, `?`, `@`) → do NOT import; emit `// [manual] JSONPath "<expr>" too complex` and add a manual task.

**Implicit HTTP success** (first test on every request unless a `response_code`/`response_message` assertion or `assume_success` exists):
```javascript
pm.test('JMeter default success (HTTP 200-399)', function () {
    pm.expect(pm.response.code).to.be.within(200, 399);
});
```

**Response Assertion** — decode `test_type` bits (`MATCH=1, CONTAINS=2, NOT=4, EQUALS=8, SUBSTRING=16, OR=32`). Field getter: `response_code`→`pm.response.code`, `response_message`→`pm.response.status`, `response_headers`→`pm.response.headers.toString()`, else `pm.response.text()`. For each test string `s`:
- **EQUALS (8)** on `response_code`: `pm.expect(pm.response.code).to.eql(<s>)` (number). Other fields: `.to.eql("<s>")`.
- **SUBSTRING (16)** → literal: `pm.expect(<getter>).to.include("<s>")`.
- **CONTAINS (2) or MATCH (1)** → regex: `pm.expect(String(<getter>)).to.match(new RegExp("<s>"))`.
- **NOT (4)** → insert `.not` before `.to`.
Wrap each in `pm.test("<name>", function () { <expr>; });`.

**Duration Assertion** → a test plus a perf `--pass-if` hint:
```javascript
pm.test("Response time < <ms>ms", function () { pm.expect(pm.response.responseTime).to.be.below(<ms>); });
```
and set the run's `--pass-if "less_than(p95, <ms>)"`.
