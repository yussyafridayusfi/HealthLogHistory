# JMeter → Postman mapping table

Fidelity: **exact** (behaviour preserved) · **approx** (closest supported equivalent, reported) · **manual** (needs the model/user) · **unsupported** (dropped, reported).
Owner: **script** (deterministic Python) · **model** (judgment call).

## Structure

| JMeter element | XML tag | Postman | Fidelity | Owner |
|---|---|---|---|---|
| Test Plan | `TestPlan` | Collection (`info` + `item`) | exact | script |
| Thread Group | `ThreadGroup` | load config (VUs/duration/profile) + folder when >1 | approx | script |
| setUp/tearDown Thread Group | `SetupThreadGroup`/`PostThreadGroup` | folder; suggest `--setup-collection`/`--teardown-collection` | approx | script+model |
| Concurrency Thread Group | `...ConcurrencyThreadGroup` | VUs=`TargetLevel`, duration=`RampUp`+`Hold`, `ramp-up` | approx | script |
| Stepping Thread Group | `kg.apc...SteppingThreadGroup` | VUs=`num_threads`, duration=`flighttime`, `ramp-up` | approx | script |
| Ultimate Thread Group | `kg.apc...UltimateThreadGroup` | VUs=max row concurrency, duration=sum of row times | approx | script |
| Arrivals Thread Group | `...ArrivalsThreadGroup` | treated like concurrency | approx | script |
| Transaction Controller | `TransactionController` | folder | exact | script |
| Simple/Generic Controller | `GenericController`/`SimpleController` | folder | exact | script |
| Loop Controller | `LoopController` | flattened; loop count → duration | approx | script |
| If Controller | `IfController` | each enclosed request gets a pre-request skip guard (`pm.execution.skipRequest`); `${var}` → `pm.variables.get(...)` | approx | script |
| While Controller | `WhileController` | flattened, requests run unconditionally; restore looping with `pm.execution.setNextRequest` | approx | model |
| Recording Controller | `RecordingController` | transparent (children recursed) | exact | script |
| Module / Include Controller | `ModuleController`/`IncludeController` | flattened children if inline; external refs reported | manual | model |

## Requests

| JMeter | XML | Postman | Fidelity | Owner |
|---|---|---|---|---|
| HTTP Request | `HTTPSamplerProxy` | request item | exact | script |
| method / path / domain / port / protocol | `HTTPSampler.*` | `request.method` + `url` (blank protocol → `http`) | exact | script |
| Query/body params | `Arguments`/`HTTPArgument` | query (GET) / urlencoded (POST/PUT/PATCH) / formdata (multipart) | exact | script |
| Raw body | `postBodyRaw=true` | `body.mode=raw` | exact | script |
| Header Manager | `HeaderManager` | `request.header` (merged by scope, later wins) | exact | script |
| HTTP Request Defaults | `ConfigTestElement`/`HttpDefaultsGui` | fallback for blank domain/port/protocol/path | exact | script |
| Auth Manager Basic/Digest | `AuthManager` | `request.auth` basic/digest (first entry; multiples reported) | exact | script |
| Auth Manager Kerberos/NTLM | `AuthManager` mechanism | — | unsupported | script |
| Cookie / Cache / DNS Manager | `CookieManager`/... | auto per-VU cookies; presets not transferred | unsupported | script |
| Non-HTTP samplers | `JDBC/FTP/LDAP/Java/TCP/Debug/Dummy...Sampler` | — | unsupported | script (reported) |

## Extractors (post-response → test script `pm.collectionVariables.set`)

| JMeter | XML | Fidelity | Owner |
|---|---|---|---|
| Regular Expression Extractor | `RegexExtractor` | exact (via `new RegExp`, group from `$n$` template) | script |
| Boundary Extractor | `BoundaryExtractor` | exact (left/right → non-greedy regex) | script |
| JSON Extractor (native) | `JSONPostProcessor` | exact for dot/index paths; complex → manual | script→model |
| JSON Path Extractor (plugin) | `...JSONPathExtractor` | exact for simple paths | script→model |
| XPath / XPath2 Extractor | `XPathExtractor`/`XPath2Extractor` | manual (no sandbox XPath) | model |
| CSS/HTML Extractor | `HtmlExtractor` | manual (no sandbox CSS) | model |

## Assertions (→ `pm.test`)

| JMeter | XML | Maps to | Fidelity | Owner |
|---|---|---|---|---|
| Response Assertion | `ResponseAssertion` | status / contains / equals / matches, honouring `test_type` bitmask + NOT | exact | script |
| — test_field response_code | | `pm.response.code` | exact | script |
| — test_field response_data | | `pm.response.text()` | exact | script |
| — test_field headers/message | | `pm.response.headers`/`.status` | exact | script |
| Duration Assertion | `DurationAssertion` | `pm.test` responseTime + `--pass-if less_than(p95, ms)` hint | exact | script |
| Size Assertion | `SizeAssertion` | `pm.test` on `responseSize` | approx | script |
| JSON Assertion | `JSONPathAssertion` | `pm.test` on simple path | approx | script→model |

`ResponseAssertion.test_type` bits: `MATCH=1, CONTAINS=2, NOT=4, EQUALS=8, SUBSTRING=16, OR=32`.

## Variables & data

| JMeter | XML | Postman | Fidelity | Owner |
|---|---|---|---|---|
| User Defined Variables | `Arguments` under TestPlan/scope | collection `variable[]` | exact | script |
| `${var}` references | inline | `{{var}}` | exact | script |
| `${__UUID}` / `${__time}` | function | `{{$guid}}` / `{{$timestamp}}` | exact | script |
| `${__Random(...)}` and other `__fn` | function | `{{$randomInt}}` or flagged reference | approx/manual | script→model |
| CSV Data Set Config | `CSVDataSet` | `--data-file` + column list | approx (one file/run; naive CSV) | script |
| Counter / Random Variable | `CounterConfig`/`RandomVariableConfig` | pre-request script or `{{$randomInt}}` | manual | model |

## Scripting

| JMeter | XML | Postman | Fidelity | Owner |
|---|---|---|---|---|
| JSR223 PreProcessor | `JSR223PreProcessor` | prerequest script (JS, IIFE-wrapped) | model-assisted | script+model |
| JSR223 PostProcessor | `JSR223PostProcessor` | test script | model-assisted | script+model |
| BeanShell Pre/PostProcessor | `BeanShell*Processor` | pre/test script | model-assisted | script+model |
| JSR223 / BeanShell Sampler | `JSR223Sampler`/`BeanShellSampler` | script-only stub (no HTTP) | manual | script+model |

## Timing / pacing — approximated, always reported

Postman has no native think time. A fixed delay is recovered where one exists;
random/throughput/sync shaping is not reproduced.

| JMeter | XML | Mapping |
|---|---|---|
| Constant Timer (per-request) | `ConstantTimer` under a sampler | pre-request pause of its delay |
| Constant / Uniform / Gaussian / Poisson Timer (group scope) | `ConstantTimer`/`UniformRandomTimer`/... | `--delay-request <ms>` (fixed part only) |
| Constant Throughput Timer | `ConstantThroughputTimer` | not reproduced; reported |
| Throughput Shaping Timer | plugin | not reproduced; reported |
| Synchronizing Timer | `SyncTimer` | not reproduced; reported |
| Test Action (pause/stop) | `TestAction` | dropped; reported |
