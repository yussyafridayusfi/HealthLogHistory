import { COLUMNS, describeSpreadsheet, serviceAccountEmail } from "./sheets.js";
import { canWriteToSheet, config } from "./config.js";

/**
 * Answers "which spreadsheet am I actually using?" without guesswork.
 *
 * A service account is a separate identity: it cannot see your Drive, and it
 * only sees files shared with its own address. So both halves get checked here -
 * which SHEET_ID is configured, and whether that robot can really reach it.
 */
async function main(): Promise<void> {
  console.log("Configuration");
  console.log(`  SHEET_ID   ${config.sheetId || "(not set)"}`);
  console.log(`  SHEET_TAB  ${config.sheetTab}`);
  console.log(
    `  DRY_RUN    ${config.dryRun}  ->  writes are ${canWriteToSheet() ? "LIVE" : "DISABLED"}`,
  );
  console.log();

  if (!config.sheetId) {
    console.log("SHEET_ID is not set, so there is nothing to look up yet.");
    console.log();
    console.log("How to find it:");
    console.log("  1. Open the spreadsheet in your browser.");
    console.log("  2. The URL looks like:");
    console.log("       https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit#gid=0");
    console.log("                                             ^--- this is SHEET_ID ---^");
    console.log("  3. Copy that middle part into .env as SHEET_ID=<value>");
    console.log("  4. Share that spreadsheet with the service-account email as Editor,");
    console.log("     then run this again to confirm access.");
    process.exitCode = 1;
    return;
  }

  // Printed before any lookup: this is the address to paste into the Google
  // Sheets "Share" dialog. Without it, every later step fails with 403.
  let email: string;
  try {
    email = await serviceAccountEmail();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.log(`Cannot read the service-account credentials.`);
    console.log(`  ${message}`);
    if (/ENOENT/i.test(message)) {
      console.log();
      console.log("GOOGLE_SERVICE_ACCOUNT_JSON is a path that does not exist. Point it at");
      console.log("the downloaded key file, or paste the JSON itself on one line.");
    }
    process.exitCode = 1;
    return;
  }
  console.log(`Service account (share the sheet with this exact address as Editor):`);
  console.log(`  ${email}`);
  console.log();

  try {
    const info = await describeSpreadsheet();

    console.log("Reachable");
    console.log(`  title      ${info.title}`);
    console.log(`  tabs       ${info.tabs.join(", ") || "(none)"}`);

    const tabExists = info.tabs.includes(config.sheetTab);
    if (!tabExists) {
      console.log();
      console.log(`  Tab "${config.sheetTab}" does not exist yet.`);
      console.log(`  Tabs present: ${info.tabs.join(", ") || "(none)"}`);
      console.log(`  It will be created automatically on the next \`npm start\`, along with`);
      console.log(`  the header row. Or set SHEET_TAB to one of the tabs above.`);
      console.log();
      console.log("Writes go to:");
      console.log(`  https://docs.google.com/spreadsheets/d/${info.id}/edit`);
      return;
    }

    const headerMatches =
      info.header.length === COLUMNS.length && info.header.every((cell, i) => cell === COLUMNS[i]);

    console.log();
    if (info.header.length === 0) {
      console.log(`  Tab "${config.sheetTab}" is empty. The header row is written on next start.`);
    } else if (headerMatches) {
      console.log(`  Header row on "${config.sheetTab}" matches COLUMNS.`);
    } else {
      console.log(`  Header row on "${config.sheetTab}" does not match COLUMNS.`);
      console.log(`    found:    ${info.header.join(" | ")}`);
      console.log(`    expected: ${COLUMNS.join(" | ")}`);
    }

    console.log(`  data rows  ${info.dataRowCount} below the header`);

    console.log();
    console.log("Writes go to:");
    console.log(`  https://docs.google.com/spreadsheets/d/${info.id}/edit`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.log("Could not read the spreadsheet.");
    console.log(`  ${message}`);
    console.log();

    // The two failures people actually hit, called out explicitly.
    if (/403|permission|forbidden/i.test(message)) {
      console.log("That looks like a permissions problem. Share the spreadsheet with");
      console.log(`  ${email}`);
      console.log("as Editor. Sharing it with your own Google account does not help -");
      console.log("the service account is a different identity.");
    } else if (/404|not found/i.test(message)) {
      console.log("That looks like a wrong or missing SHEET_ID, or a key from the wrong project.");
    } else if (/ENOENT/i.test(message)) {
      console.log("The key file path in GOOGLE_SERVICE_ACCOUNT_JSON does not exist.");
    }
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
