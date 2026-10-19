import type { CanonicalMessage, ParseResult, Reading, Unit } from "./types.js";

/**
 * Parses the fixed key/value form the sender writes:
 *
 *   date: 2026-10-19
 *   blood sugar: 100
 *   time check: 08.08 am
 *   notes: it's normal after breakfast
 *
 * Rules are deterministic on purpose: a misread blood glucose value is a
 * health-data error, so ambiguity produces a warning (surfaced back to the
 * sender) rather than a silent guess.
 */

const KEY_ALIASES: Record<string, Field> = {
  date: "date",
  tanggal: "date",
  "blood sugar": "sugar",
  bloodsugar: "sugar",
  "blood glucose": "sugar",
  sugar: "sugar",
  "gula darah": "sugar",
  gds: "sugar",
  "time check": "time",
  timecheck: "time",
  "check time": "time",
  time: "time",
  jam: "time",
  notes: "notes",
  note: "notes",
  catatan: "notes",
};

type Field = "date" | "sugar" | "time" | "notes";

const LINE = /^\s*([A-Za-z][A-Za-z .]*?)\s*[:=]\s*(.*?)\s*$/;
const TIME = /^(\d{1,2})\s*[.:,]\s*(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)?$/i;
const NUMBER = /(-?\d+(?:[.,]\d+)?)/;

type Fields = Partial<Record<Field, string>>;

export type ParseContext = {
  /** Timestamp of the message itself, used when the sender omits a date. */
  sentAt: Date;
  /** Minutes east of UTC. */
  tzOffsetMinutes: number;
};

export function parseMessage(
  text: string,
  ctx: ParseContext,
): ParseResult {
  const warnings: string[] = [];
  const fields: Fields = {};
  const unknown: string[] = [];

  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line === "") continue;
    const match = LINE.exec(line);
    if (!match) {
      warnings.push(`Ignored line without a key: "${truncate(line)}"`);
      continue;
    }
    const key = match[1]!.toLowerCase().replace(/\s+/g, " ").trim();
    const value = match[2] ?? "";
    const field = KEY_ALIASES[key];
    if (!field) {
      unknown.push(key);
      continue;
    }
    // Last value wins for repeated keys; the sender probably corrected a typo.
    fields[field] = value;
  }

  if (unknown.length > 0) {
    warnings.push(`Unknown field(s) ignored: ${unknown.join(", ")}`);
  }
  if (Object.keys(fields).length === 0) {
    return {
      ok: false,
      reason:
        "No recognised fields. Expected lines like 'blood sugar: 100' and 'time check: 08.08 am'.",
      warnings,
    };
  }

  const sugar = parseSugar(fields.sugar, warnings);
  if (!sugar) {
    return {
      ok: false,
      reason: "Could not read a blood sugar number.",
      warnings,
    };
  }

  const time = parseTime(fields.time, warnings);

  const localDate = resolveDate(fields.date, ctx, warnings);
  if (localDate === null) {
    return { ok: false, reason: `Unreadable date: "${truncate(fields.date ?? "")}"`, warnings };
  }

  // Without a "time check" the message timestamp is the best evidence we have
  // for when the reading was taken.
  const sentLocal = shiftToLocal(ctx.sentAt, ctx.tzOffsetMinutes);
  const hh = time?.hour ?? sentLocal.getUTCHours();
  const mm = time?.minute ?? sentLocal.getUTCMinutes();
  if (!time) {
    warnings.push("No 'time check' given; used the message time instead.");
  }

  const measuredAt = new Date(
    Date.UTC(localDate.year, localDate.month - 1, localDate.day, hh, mm) -
      ctx.tzOffsetMinutes * 60_000,
  );

  const reading: Reading = {
    measuredAt: measuredAt.toISOString(),
    bloodSugar: sugar.value,
    unit: sugar.unit,
    timeCheckLocal: time?.text ?? null,
    notes: fields.notes?.trim() ? fields.notes.trim() : null,
  };

  return { ok: true, reading, warnings };
}

function parseSugar(
  raw: string | undefined,
  warnings: string[],
): { value: number; unit: Unit } | null {
  if (raw === undefined) {
    warnings.push("No 'blood sugar' line found.");
    return null;
  }
  const match = NUMBER.exec(raw);
  if (!match) return null;

  const value = Number(match[1]!.replace(",", "."));
  if (!Number.isFinite(value) || value <= 0) return null;

  let unit: Unit | null = null;
  if (/mmol/i.test(raw)) unit = "mmol/L";
  else if (/mg\s*\/?\s*dl/i.test(raw)) unit = "mg/dL";

  if (!unit) {
    // Physiological ranges do not overlap: mg/dL readings are ~40-600,
    // mmol/L readings are ~2-33. A bare number below 40 is therefore mmol/L.
    unit = value < 40 ? "mmol/L" : "mg/dL";
    warnings.push(
      `No unit given; assumed ${unit} for ${value}. Send "blood sugar: ${value} mmol/L" to be explicit.`,
    );
  }

  return { value, unit };
}

function parseTime(
  raw: string | undefined,
  warnings: string[],
): { hour: number; minute: number; text: string } | null {
  if (raw === undefined) return null;
  // "08.08 a.m." -> "08.08 am", "8.08pm" -> "8.08pm"
  const cleaned = raw
    .trim()
    .replace(/a\.\s*m\.?/gi, "am")
    .replace(/p\.\s*m\.?/gi, "pm")
    .replace(/\s+/g, " ");
  const match = TIME.exec(cleaned);
  if (!match) {
    warnings.push(`Could not read 'time check: ${truncate(raw)}'; ignored.`);
    return null;
  }

  let hour = Number(match[1]);
  const minute = Number(match[2]);
  const period = match[3]?.toLowerCase().replace(/\./g, "");

  if (hour > 23 || minute > 59) {
    warnings.push(`Out-of-range time '${truncate(raw)}'; ignored.`);
    return null;
  }

  if (period === "pm" && hour < 12) hour += 12;
  else if (period === "am" && hour === 12) hour = 0;
  else if (!period && hour <= 12) {
    warnings.push(
      `'${truncate(raw)}' has no am/pm; read as ${pad(hour)}:${pad(minute)}. Include am/pm to be sure.`,
    );
  }

  return { hour, minute, text: `${pad(hour)}:${pad(minute)}` };
}

function resolveDate(
  raw: string | undefined,
  ctx: ParseContext,
  warnings: string[],
): { year: number; month: number; day: number } | null {
  const today = shiftToLocal(ctx.sentAt, ctx.tzOffsetMinutes);
  const fallback = {
    year: today.getUTCFullYear(),
    month: today.getUTCMonth() + 1,
    day: today.getUTCDate(),
  };

  if (raw === undefined || raw.trim() === "") {
    warnings.push("No 'date' given; used the message date.");
    return fallback;
  }

  const value = raw.trim().toLowerCase();
  if (value === "today" || value === "hari ini") return fallback;
  if (value === "yesterday" || value === "kemarin") {
    const d = new Date(Date.UTC(fallback.year, fallback.month - 1, fallback.day) - 86_400_000);
    return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() };
  }

  // YYYY-MM-DD
  const iso = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/.exec(value);
  if (iso) {
    return { year: Number(iso[1]), month: Number(iso[2]), day: Number(iso[3]) };
  }

  // DD-MM-YYYY / DD/MM/YYYY (day first: matches how the sender writes times).
  const dmy = /^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$/.exec(value);
  if (dmy) {
    const day = Number(dmy[1]);
    const month = Number(dmy[2]);
    if (day > 12 && month <= 12) return { year: Number(dmy[3]), month, day };
    if (month > 12 && day <= 12) {
      warnings.push(`Read '${truncate(raw)}' as month-first; write YYYY-MM-DD to avoid ambiguity.`);
      return { year: Number(dmy[3]), month: day, day: month };
    }
    warnings.push(`Read '${truncate(raw)}' as DD/MM; write YYYY-MM-DD to avoid ambiguity.`);
    return { year: Number(dmy[3]), month, day };
  }

  return null;
}

/** Shifts a UTC instant into local wall-clock time, exposed via the getUTC* accessors. */
function shiftToLocal(instant: Date, tzOffsetMinutes: number): Date {
  return new Date(instant.getTime() + tzOffsetMinutes * 60_000);
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

function truncate(value: string): string {
  return value.length > 40 ? `${value.slice(0, 40)}...` : value;
}

/** Convenience wrapper for callers holding a CanonicalMessage. */
export function parseCanonical(
  message: CanonicalMessage,
  tzOffsetMinutes: number,
): ParseResult {
  return parseMessage(message.text, {
    sentAt: new Date(message.sentAt),
    tzOffsetMinutes,
  });
}
