import { readFile } from "node:fs/promises";
import { google } from "googleapis";
import { config, canWriteToSheet } from "./config.js";
import type { CanonicalMessage, Reading } from "./types.js";

/**
 * Append-only writer for a Google Sheet.
 *
 * Uses the Sheets API rather than an Apps Script web app so the exact request
 * (and its response) can be replayed from a Postman collection.
 */

export const COLUMNS = [
  "received_at",
  "measured_at",
  "time_check_local",
  "blood_sugar",
  "unit",
  "notes",
  "sender_id",
  "sender_name",
  "wa_message_id",
  "raw_message",
] as const;

export function toRow(message: CanonicalMessage, reading: Reading): string[] {
  return [
    message.receivedAt,
    reading.measuredAt,
    reading.timeCheckLocal ?? "",
    String(reading.bloodSugar),
    reading.unit,
    reading.notes ?? "",
    message.senderId,
    message.senderName ?? "",
    message.messageId,
    message.text.replace(/\r?\n/g, " | "),
  ];
}

async function loadCredentials(): Promise<Record<string, unknown>> {
  const source = config.googleCredentials;
  if (!source) {
    throw new Error(
      "GOOGLE_SERVICE_ACCOUNT_JSON is not set. Point it at the service-account key file, or paste the JSON on one line.",
    );
  }

  // Accept either a path to the key file or the raw JSON on one line.
  const parsed: unknown = source.trimStart().startsWith("{")
    ? JSON.parse(source)
    : JSON.parse(await readFile(source, "utf8"));

  if (typeof parsed !== "object" || parsed === null) {
    throw new Error("The service-account credentials are not a JSON object.");
  }
  return parsed as Record<string, unknown>;
}

let cachedCredentials: Record<string, unknown> | null = null;

async function credentials(): Promise<Record<string, unknown>> {
  cachedCredentials ??= await loadCredentials();
  return cachedCredentials;
}

let cached: ReturnType<typeof google.sheets> | null = null;

async function client() {
  if (cached) return cached;

  const auth = new google.auth.GoogleAuth({
    credentials: await credentials(),
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });

  cached = google.sheets({ version: "v4", auth });
  return cached;
}

/**
 * Turns a Google API failure into something actionable.
 *
 * The raw GaxiosError dumps ~100 lines of transport detail that bury the one
 * fact that matters: whether this is a sharing mistake or a real bug. A 403 that
 * arrives after a successful read almost always means the service account was
 * shared as Viewer or Commenter, which cannot append rows.
 */
function explain(error: unknown): Error {
  const message = error instanceof Error ? error.message : String(error);
  const status = (error as { code?: unknown })?.code;

  if (status === 403 || /does not have permission|permission_denied|forbidden/i.test(message)) {
    return new Error(
      "The service account can reach this spreadsheet but cannot write to it (403 PERMISSION_DENIED). " +
        "Its access is probably Viewer or Commenter. In Google Sheets: Share -> set the service " +
        "account's access to Editor. Run `npm run sheet:check` to see the exact address to share with.",
    );
  }

  if (status === 404 || /not found/i.test(message)) {
    return new Error(
      "Spreadsheet not found (404). Check SHEET_ID, and that the key file belongs to the project " +
        "the spreadsheet was shared from.",
    );
  }

  if (/unable to parse range/i.test(message)) {
    return new Error(
      `Tab "${config.sheetTab}" does not exist and could not be created. Check SHEET_TAB, or create the tab by hand.`,
    );
  }

  return new Error(message);
}

/** Runs an API call, replacing transport noise with an actionable message. */
async function guard<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation();
  } catch (error) {
    throw explain(error);
  }
}

/**
 * The robot's email address. This is the identity that needs Editor access on
 * the spreadsheet - it is not your own Google account, and sharing the sheet
 * with yourself does not grant it anything.
 */
export async function serviceAccountEmail(): Promise<string> {
  const email = (await credentials()).client_email;
  if (typeof email !== "string" || email === "") {
    throw new Error('The credentials JSON has no "client_email" - is this a service-account key?');
  }
  return email;
}

export type SpreadsheetInfo = {
  id: string;
  title: string;
  tabs: string[];
  /** The header row as it currently exists, if any. */
  header: string[];
  /** Non-empty rows below the header. Proves data is actually landing. */
  dataRowCount: number;
};

/** The tab names only. Cheaper than describeSpreadsheet, and safe when the tab is missing. */
export async function listTabs(): Promise<string[]> {
  const sheets = await client();
  const meta = await sheets.spreadsheets.get({
    spreadsheetId: config.sheetId,
    fields: "sheets.properties.title",
  });

  return (meta.data.sheets ?? [])
    .map((sheet) => sheet.properties?.title)
    .filter((title): title is string => typeof title === "string");
}

/** Reads back what the configured SHEET_ID actually points at. */
export async function describeSpreadsheet(): Promise<SpreadsheetInfo> {
  const sheets = await client();

  const meta = await sheets.spreadsheets.get({
    spreadsheetId: config.sheetId,
    fields: "properties.title,sheets.properties.title",
  });

  const tabs = (meta.data.sheets ?? [])
    .map((sheet) => sheet.properties?.title)
    .filter((title): title is string => typeof title === "string");

  // Only read the header when the tab exists: a values.get against a missing
  // tab throws, which would hide the real problem (a missing tab) behind a
  // generic read failure.
  let header: string[] = [];
  let dataRowCount = 0;
  if (tabs.includes(config.sheetTab)) {
    const values = await sheets.spreadsheets.values.get({
      spreadsheetId: config.sheetId,
      range: `${config.sheetTab}!A1:J1000`,
    });
    const rows = values.data.values ?? [];
    header = (rows[0] ?? []).map(String);
    dataRowCount = rows
      .slice(1)
      .filter((row) => row.some((cell) => String(cell).trim() !== "")).length;
  }

  return {
    id: config.sheetId,
    title: meta.data.properties?.title ?? "(untitled)",
    tabs,
    header,
    dataRowCount,
  };
}

/**
 * Creates the configured tab if it is missing.
 *
 * A brand-new spreadsheet has a single tab named "Sheet1", so the default
 * SHEET_TAB of "Log" will not exist. Creating it beats making the user either
 * rename their tab or guess this setting.
 */
export async function ensureTab(): Promise<boolean> {
  const tabs = await listTabs();
  if (tabs.includes(config.sheetTab)) return false;

  const sheets = await client();
  await guard(() =>
    sheets.spreadsheets.batchUpdate({
      spreadsheetId: config.sheetId,
      requestBody: {
        requests: [{ addSheet: { properties: { title: config.sheetTab } } }],
      },
    }),
  );

  return true;
}

export type ProvisionResult = { tabCreated: boolean; headerWritten: boolean };

/** Makes the sheet usable: tab present, header row present. Idempotent. */
export async function ensureHeader(): Promise<ProvisionResult> {
  const tabCreated = await ensureTab();

  const sheets = await client();
  const existing = await guard(() =>
    sheets.spreadsheets.values.get({
      spreadsheetId: config.sheetId,
      range: `${config.sheetTab}!A1:J1`,
    }),
  );
  if (existing.data.values?.[0]?.length) {
    return { tabCreated, headerWritten: false };
  }

  await guard(() =>
    sheets.spreadsheets.values.update({
      spreadsheetId: config.sheetId,
      range: `${config.sheetTab}!A1`,
      valueInputOption: "RAW",
      requestBody: { values: [[...COLUMNS]] },
    }),
  );

  return { tabCreated, headerWritten: true };
}

export type AppendResult = { mode: "dry-run" | "live"; row: string[] };

export async function appendRow(row: string[]): Promise<AppendResult> {
  if (!canWriteToSheet()) {
    console.log("[sheets] dry-run row:", JSON.stringify(row));
    return { mode: "dry-run", row };
  }

  const sheets = await client();
  await guard(() =>
    sheets.spreadsheets.values.append({
      spreadsheetId: config.sheetId,
      range: `${config.sheetTab}!A1`,
      valueInputOption: "USER_ENTERED",
      // INSERT_ROWS so concurrent appends never overwrite each other.
      insertDataOption: "INSERT_ROWS",
      requestBody: { values: [row] },
    }),
  );

  return { mode: "live", row };
}
