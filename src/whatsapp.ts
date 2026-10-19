import { mkdir } from "node:fs/promises";
import pino from "pino";
import qrcode from "qrcode-terminal";
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  type WASocket,
} from "@whiskeysockets/baileys";
import { config } from "./config.js";
import { ingest, receipt } from "./pipeline.js";
import type { CanonicalMessage } from "./types.js";

/**
 * Unofficial personal-WhatsApp transport (Baileys).
 *
 * Know what this buys and costs before relying on it:
 *  - It logs in as a linked device on your own account, so there is no Meta app
 *    review and no per-message fee.
 *  - It violates WhatsApp's Terms of Service. Accounts get banned. Treat the
 *    number as expendable and never log anything you cannot re-create.
 *  - It needs a long-lived process, so serverless hosts are out. A small VPS,
 *    home server, or Raspberry Pi is the right home for it.
 *  - It depends on an undocumented protocol. It will break when WhatsApp
 *    changes something, and it is not a good foundation for a real product.
 *
 * The migration path is cheap: only this file knows about Baileys. Replacing it
 * with a Cloud API webhook means mapping that payload into CanonicalMessage and
 * calling ingest() - the parser, dedupe store, and Sheets writer do not change.
 */

const logger = pino({ level: process.env.LOG_LEVEL ?? "warn" });

/** Pulls the text out of the message shapes this log actually receives. */
function extractText(message: unknown): string | null {
  const content = message as Record<string, any> | null | undefined;
  if (!content) return null;
  return (
    content.conversation ??
    content.extendedTextMessage?.text ??
    content.ephemeralMessage?.message?.conversation ??
    content.ephemeralMessage?.message?.extendedTextMessage?.text ??
    content.viewOnceMessage?.message?.conversation ??
    null
  );
}

function allowed(senderId: string): boolean {
  if (config.allowFrom.length === 0) return true;
  // JIDs carry a device suffix ("62812:3@s.whatsapp.net"); compare the user part.
  const user = senderId.split("@")[0]?.split(":")[0] ?? "";
  return config.allowFrom.includes(user);
}

export async function startWhatsApp(): Promise<void> {
  await mkdir(config.authDir, { recursive: true });

  const { state, saveCreds } = await useMultiFileAuthState(config.authDir);
  const sock: WASocket = makeWASocket({
    auth: state,
    logger,
    // History sync is not needed to log new readings and slows startup.
    syncFullHistory: false,
    markOnlineOnConnect: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log("\nScan this QR code in WhatsApp > Linked devices:\n");
      qrcode.generate(qr, { small: true });
    }

    if (connection === "open") {
      console.log("[whatsapp] connected");
    }

    if (connection === "close") {
      const status = (lastDisconnect?.error as { output?: { statusCode?: number } } | undefined)
        ?.output?.statusCode;
      const loggedOut = status === DisconnectReason.loggedOut;
      console.log(
        `[whatsapp] disconnected (status ${status ?? "unknown"})${loggedOut ? "; session invalid, rescan the QR code" : "; reconnecting"}`,
      );
      if (!loggedOut) {
        // Backoff so a persistent failure does not spin the process.
        setTimeout(() => void startWhatsApp(), 5_000);
      }
    }
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    // "notify" is a fresh message; "append" is history replay, which the
    // dedupe store would absorb but there is no reason to parse it.
    if (type !== "notify") return;

    for (const raw of messages) {
      void handleMessage(sock, raw);
    }
  });
}

async function handleMessage(sock: WASocket, raw: any): Promise<void> {
  const key = raw?.key;
  const chatId: string | undefined = key?.remoteJid;
  if (!chatId || key?.fromMe) return;
  // Status broadcasts and groups are noise for a personal health log.
  if (chatId === "status@broadcast" || chatId.endsWith("@g.us")) return;

  const text = extractText(raw?.message);
  if (typeof text !== "string") return;

  const senderId: string = key.participant ?? chatId;
  if (!allowed(senderId)) {
    console.log(`[whatsapp] ignored message from ${senderId} (not in ALLOW_FROM)`);
    return;
  }

  const receivedAt = new Date();
  const sentAt = raw?.messageTimestamp
    ? new Date(Number(raw.messageTimestamp) * 1000)
    : receivedAt;

  const message: CanonicalMessage = {
    source: "whatsapp-personal",
    messageId: key.id,
    chatId,
    senderId,
    senderName: raw?.pushName ?? undefined,
    sentAt: sentAt.toISOString(),
    receivedAt: receivedAt.toISOString(),
    text,
  };

  const outcome = await ingest(message);
  console.log(`[whatsapp] ${outcome.status} ${key.id}`);

  if (config.replyEnabled && outcome.status !== "duplicate") {
    try {
      await sock.sendMessage(chatId, { text: receipt(outcome) });
    } catch (error) {
      console.error("[whatsapp] reply failed:", error);
    }
  }
}
