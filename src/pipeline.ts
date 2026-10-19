import { config } from "./config.js";
import { DedupeStore } from "./dedupe.js";
import { parseCanonical } from "./parser.js";
import { appendRow, toRow } from "./sheets.js";
import type { CanonicalMessage, IngestOutcome } from "./types.js";

const store = new DedupeStore(config.stateFile);

/**
 * The whole write path: dedupe -> parse -> append -> remember.
 *
 * Ordering matters. The message id is only recorded after a successful append,
 * so a Sheets outage leaves the message retryable instead of silently lost.
 */
export async function ingest(message: CanonicalMessage): Promise<IngestOutcome> {
  if (await store.has(message.messageId)) {
    return { status: "duplicate", messageId: message.messageId };
  }

  const parsed = parseCanonical(message, config.tzOffsetMinutes);
  if (!parsed.ok) {
    return { status: "unparsed", reason: parsed.reason, warnings: parsed.warnings };
  }

  const row = toRow(message, parsed.reading);
  try {
    await appendRow(row);
  } catch (error) {
    return {
      status: "error",
      reason: error instanceof Error ? error.message : String(error),
    };
  }

  await store.add(message.messageId);

  return { status: "appended", row, warnings: parsed.warnings, reading: parsed.reading };
}

/**
 * Human-readable receipt. Echoing the parsed values back is the cheapest
 * correction mechanism there is: the sender sees a misread number immediately.
 */
export function receipt(outcome: IngestOutcome): string {
  switch (outcome.status) {
    case "appended": {
      const r = outcome.reading;
      const lines = [
        `Logged: ${r.bloodSugar} ${r.unit}`,
        `Measured: ${r.measuredAt}`,
        r.timeCheckLocal ? `Time check: ${r.timeCheckLocal}` : null,
        r.notes ? `Notes: ${r.notes}` : null,
        ...outcome.warnings.map((w) => `Note: ${w}`),
      ];
      return lines.filter(Boolean).join("\n");
    }
    case "duplicate":
      return "Already logged this message; nothing appended.";
    case "unparsed":
      return [
        `Could not log this message. ${outcome.reason}`,
        "Format:",
        "date: 2026-10-19",
        "blood sugar: 100",
        "time check: 08.08 am",
        "notes: normal after breakfast",
      ].join("\n");
    case "error":
      return `Could not write to the spreadsheet: ${outcome.reason}`;
  }
}
