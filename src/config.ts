import "dotenv/config";

function bool(value: string | undefined, fallback: boolean): boolean {
  if (value === undefined || value.trim() === "") return fallback;
  return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
}

function num(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const config = {
  port: num(process.env.PORT, 8787),

  /** Parse and log the row, but do not write to Google Sheets. */
  dryRun: bool(process.env.DRY_RUN, true),
  whatsappEnabled: bool(process.env.WHATSAPP_ENABLED, true),
  replyEnabled: bool(process.env.REPLY_ENABLED, true),

  /** Minutes east of UTC used to interpret "time check". */
  tzOffsetMinutes: num(process.env.TZ_OFFSET_MINUTES, 420),

  authDir: process.env.AUTH_DIR ?? ".state/baileys-auth",
  stateFile: process.env.STATE_FILE ?? ".state/processed.json",

  /** Empty = accept every inbound chat. */
  allowFrom: (process.env.ALLOW_FROM ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean),

  sheetId: process.env.SHEET_ID ?? "",
  sheetTab: process.env.SHEET_TAB ?? "Log",
  googleCredentials: process.env.GOOGLE_SERVICE_ACCOUNT_JSON ?? "",
} as const;

/** Writing needs credentials AND an opt-out of dry run. Both, not either. */
export function canWriteToSheet(): boolean {
  return !config.dryRun && config.sheetId !== "";
}
