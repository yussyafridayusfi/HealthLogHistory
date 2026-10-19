# `.sim.yaml` reference

Read this when writing or debugging a `.sim.yaml` by hand.
`postman simulation create` writes the same shape; the Postman app adds
`id:` fields, which a local run ignores.

```yaml
simulation: checkout-dev               # required: the name
routing: path                          # path (default) or header
mocks:                                 # required: one entry per member
  - routeKey: catalog                  # lowercase letters, digits, - and _
    path: ../mocks/catalog/config.yaml # a mock's config.yaml, or a .js handler
    scenarios:                         # optional: conditions, below
      - overrides:
          conditions:
            latency: { delay_ms: 250 }
```

Run the `.sim.yaml` rather than a simulation id: a run by id drops every
member's conditions (CLI 1.70.0–1.73.0).

## Conditions

| Condition | Field | Effect |
| --- | --- | --- |
| `latency` | `delay_ms` | Holds each request that long, then serves it. |
| `error` | `status_code` (400–599) | Every request gets that status as JSON, with `"scenario":"error"`. |
| `rate_limit` | `requests_per_minute` | Requests over the limit get `429` and `Retry-After`. |
| `chaos` | `failure_rate` (0–100), optional `status_code` | That share of requests, at random, get `500` or that status. |

- A condition covers every route on its member. To fail one endpoint while
  its siblings succeed, give that endpoint a mock of its own.
- They run in a fixed order (`latency`, `error`, `rate_limit`, `chaos`), and
  the first to fail a request ends it, so set one failure at a time,
  optionally with `latency`.
- An unknown condition name is ignored silently, so a misspelling injects
  nothing. `overrides.bypass` prints a warning and is ignored.
- A `latency` request is served and logged, with a `duration` that leaves
  out the delay.

## The log (`--output ndjson`)

One JSON event per line on stdout; notices go to stderr, so keep them apart.

- `listening`: every member has started; a member that fails to start
  stops the run with `Error: …` and exit code 1.
- `request`: one per request a member served.
- `summary`, on stop: `requestsServed` (under 400), `requestsFailed` (400
  and up), `uptimeMs` and `signal`. Injected failures and the simulation's
  own `400`, `404` and `502` aren't counted.
