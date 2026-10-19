import { canWriteToSheet, config } from "./config.js";
import { ensureHeader } from "./sheets.js";
import { startIngestServer } from "./server.js";

async function main(): Promise<void> {
  const port = await startIngestServer();
  console.log(`[http] listening on http://127.0.0.1:${port}`);
  console.log(`[http]   GET  /health`);
  console.log(`[http]   POST /ingest   {"text":"blood sugar: 100\\ntime check: 08.08 am"}`);

  console.log(
    canWriteToSheet()
      ? `[sheets] live writes to spreadsheet ${config.sheetId}, tab "${config.sheetTab}"`
      : `[sheets] dry run (DRY_RUN=${config.dryRun}, SHEET_ID=${config.sheetId ? "set" : "empty"}) - rows are logged, not written`,
  );

  if (canWriteToSheet()) {
    try {
      const { tabCreated, headerWritten } = await ensureHeader();
      if (tabCreated) console.log(`[sheets] created tab "${config.sheetTab}"`);
      if (headerWritten) console.log("[sheets] wrote the header row");
      if (!tabCreated && !headerWritten) console.log("[sheets] tab and header already in place");
    } catch (error) {
      console.error(
        `[sheets] could not prepare the sheet: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  if (!config.whatsappEnabled) {
    console.log("[whatsapp] disabled (WHATSAPP_ENABLED=false); HTTP ingest only");
    return;
  }

  const { startWhatsApp } = await import("./whatsapp.js");
  await startWhatsApp();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
