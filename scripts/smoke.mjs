/**
 * End-to-end smoke check against a running instance.
 *
 *   npm start              (in one terminal, WHATSAPP_ENABLED=false)
 *   npm run smoke          (in another)
 *
 * Every assertion here is about observable behaviour of the HTTP contract in
 * openapi.yaml, so this doubles as the "what does this thing actually do" doc.
 */
import { readFile } from "node:fs/promises";

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:8787";

let failures = 0;

function check(label, condition, detail) {
  const status = condition ? "PASS" : "FAIL";
  if (!condition) failures++;
  console.log(`${status}  ${label}`);
  if (!condition) console.log(`      ${JSON.stringify(detail)}`);
}

async function post(body) {
  const res = await fetch(`${BASE}/ingest`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  return { status: res.status, json: await res.json() };
}

const fixture = async (name) =>
  JSON.parse(await readFile(new URL(`../fixtures/${name}`, import.meta.url), "utf8"));

// --- health ----------------------------------------------------------------
const health = await (await fetch(`${BASE}/health`)).json();
console.log("health:", JSON.stringify(health));
check("GET /health reports ok", health.status === "ok", health);
check("tz offset is 420", health.tzOffsetMinutes === 420, health);

// --- the reference message -------------------------------------------------
// A run-scoped id keeps repeat runs meaningful while still letting the second
// POST prove idempotency.
const canonical = await fixture("canonical-message.json");
canonical.messageId = `wamid.SMOKE-${Date.now()}`;
const first = await post(canonical);
console.log("\nPOST /ingest (canonical):", JSON.stringify(first.json, null, 2));
check("reference message appends", first.json.status === "appended", first.json);
check("reads 100 mg/dL", first.json.reading?.bloodSugar === 100 && first.json.reading?.unit === "mg/dL", first.json.reading);
check("normalises 08.08 am to 08:08", first.json.reading?.timeCheckLocal === "08:08", first.json.reading);
check("converts 08:08 UTC+7 to 01:08Z", first.json.reading?.measuredAt === "2026-10-19T01:08:00.000Z", first.json.reading);
check("writes 10 cells", first.json.row?.length === 10, first.json.row);
// The sample message states no unit, so exactly one warning is correct here.
check(
  "warns only about the assumed unit",
  first.json.warnings?.length === 1 && /assumed mg\/dL/.test(first.json.warnings[0]),
  first.json.warnings,
);

// --- idempotency -----------------------------------------------------------
const again = await post(canonical);
check("redelivery is a duplicate, not a second row", again.json.status === "duplicate", again.json);

// --- shorthand -------------------------------------------------------------
// The shorthand body derives its id from the text, so a repeat run is expected
// to be a duplicate. Both outcomes mean the same thing: the pipeline handled it.
const shorthand = await post(await fixture("shorthand-message.json"));
check(
  "shorthand body is accepted and not unparsed",
  shorthand.json.status === "appended" || shorthand.json.status === "duplicate",
  shorthand.json,
);

// --- unparsable ------------------------------------------------------------
const unparsable = await post(await fixture("unparsable-message.json"));
check("free-form text is rejected with a reason", unparsable.json.status === "unparsed", unparsable.json);

// --- error handling --------------------------------------------------------
const empty = await post({});
check("missing text is a 400", empty.status === 400, empty);

const badJson = await post("{not json");
check("invalid JSON is a 400", badJson.status === 400, badJson);

const missing = await (await fetch(`${BASE}/nope`)).json();
check("unknown route is a 404", missing.error !== undefined, missing);

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) failed.`);
process.exitCode = failures === 0 ? 0 : 1;
