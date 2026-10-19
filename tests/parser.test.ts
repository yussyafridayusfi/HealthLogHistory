import { strict as assert } from "node:assert";
import { test } from "node:test";
import { parseMessage } from "../src/parser.js";
import { toRow } from "../src/sheets.js";
import type { CanonicalMessage } from "../src/types.js";

const ctx = { sentAt: new Date("2026-10-19T05:30:00Z"), tzOffsetMinutes: 420 };

test("parses the reference message", () => {
  const result = parseMessage(
    [
      "date: 2026-10-19",
      "blood sugar: 100",
      "time check: 08.08 am",
      "notes: it's normal after breakfast",
    ].join("\n"),
    ctx,
  );

  assert.equal(result.ok, true);
  assert.ok(result.ok);
  assert.equal(result.reading.bloodSugar, 100);
  assert.equal(result.reading.unit, "mg/dL");
  assert.equal(result.reading.timeCheckLocal, "08:08");
  assert.equal(result.reading.notes, "it's normal after breakfast");
  // 08:08 at UTC+7 -> 01:08Z
  assert.equal(result.reading.measuredAt, "2026-10-19T01:08:00.000Z");
});

test("expands a dotted am/pm marker", () => {
  const result = parseMessage("blood sugar: 120\ntime check: 8.08 a.m.", ctx);
  assert.ok(result.ok);
  assert.equal(result.reading.timeCheckLocal, "08:08");
});

test("adds 12 hours for pm", () => {
  const result = parseMessage("blood sugar: 140\ntime check: 8.08 pm", ctx);
  assert.ok(result.ok);
  assert.equal(result.reading.timeCheckLocal, "20:08");
});

test("warns instead of guessing when am/pm is missing", () => {
  const result = parseMessage("blood sugar: 100\ntime check: 08.08", ctx);
  assert.ok(result.ok);
  assert.equal(result.reading.timeCheckLocal, "08:08");
  assert.match(result.warnings.join(" "), /am\/pm/);
});

test("infers mmol/L from a physiological range", () => {
  const result = parseMessage("blood sugar: 5,6 mmol", ctx);
  assert.ok(result.ok);
  assert.equal(result.reading.unit, "mmol/L");
  assert.equal(result.reading.bloodSugar, 5.6);
});

test("falls back to the message timestamp when date/time are omitted", () => {
  const result = parseMessage("blood sugar: 5.6", ctx);
  assert.ok(result.ok);
  assert.equal(result.reading.measuredAt, "2026-10-19T05:30:00.000Z");
  assert.match(result.warnings.join(" "), /message date/);
  assert.match(result.warnings.join(" "), /message time/);
});

test("understands 'yesterday' relative to the message timestamp", () => {
  const result = parseMessage("date: yesterday\nblood sugar: 90\ntime check: 07:00", ctx);
  assert.ok(result.ok);
  assert.equal(result.reading.measuredAt, "2026-10-18T00:00:00.000Z");
});

test("treats 20:08 as 24-hour time without an am/pm warning", () => {
  const result = parseMessage("date: 2026-10-19\nblood sugar: 90 mg/dL\ntime check: 20:08", ctx);
  assert.ok(result.ok);
  assert.equal(result.reading.timeCheckLocal, "20:08");
  assert.deepEqual(result.warnings, []);
});

test("rejects free-form text with guidance", () => {
  const result = parseMessage("sugar was 100 this morning", ctx);
  assert.equal(result.ok, false);
});

test("rejects a message with a non-numeric reading", () => {
  const result = parseMessage("blood sugar: high", ctx);
  assert.equal(result.ok, false);
});

test("flags unknown fields but keeps the known ones", () => {
  const result = parseMessage("blood sugar: 100\nmood: fine", ctx);
  assert.ok(result.ok);
  assert.equal(result.reading.bloodSugar, 100);
  assert.match(result.warnings.join(" "), /mood/);
});

test("keeps a colon inside notes", () => {
  const result = parseMessage("blood sugar: 100\nnotes: insulin ratio 1:2", ctx);
  assert.ok(result.ok);
  assert.equal(result.reading.notes, "insulin ratio 1:2");
});

test("row layout matches the sheet header", () => {
  const message: CanonicalMessage = {
    source: "whatsapp-personal",
    messageId: "wamid.TEST",
    chatId: "6281234567890@s.whatsapp.net",
    senderId: "6281234567890@s.whatsapp.net",
    senderName: "Test",
    sentAt: "2026-10-19T05:30:00.000Z",
    receivedAt: "2026-10-19T05:30:02.000Z",
    text: "blood sugar: 100\ntime check: 08.08 am",
  };
  const parsed = parseMessage(message.text, ctx);
  assert.ok(parsed.ok);
  const row = toRow(message, parsed.reading);
  assert.equal(row.length, 10);
  assert.equal(row[3], "100");
  assert.equal(row[4], "mg/dL");
  assert.equal(row[8], "wamid.TEST");
  assert.equal(row[9], "blood sugar: 100 | time check: 08.08 am");
});
