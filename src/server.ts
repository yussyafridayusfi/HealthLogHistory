import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { config } from "./config.js";
import { ingest } from "./pipeline.js";
import type { CanonicalMessage } from "./types.js";

/**
 * Local HTTP surface.
 *
 * This is the seam that makes the pipeline testable without WhatsApp: the
 * Postman collection posts a captured message here and asserts on the outcome.
 * It is also where a real webhook would live if this ever moves to the official
 * Cloud API - the handler body would not change, only the mapping into
 * CanonicalMessage.
 */

const MAX_BODY_BYTES = 64 * 1024;

function send(res: ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload, null, 2);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

async function readBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY_BYTES) throw new Error("Request body too large.");
    chunks.push(chunk as Buffer);
  }
  if (size === 0) throw new Error("Empty request body.");
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

/**
 * Accepts either a full CanonicalMessage or the shorthand `{ "text": "..." }`
 * used in fixtures and manual testing.
 */
function toCanonical(input: unknown): CanonicalMessage {
  if (typeof input !== "object" || input === null) {
    throw new Error("Body must be a JSON object.");
  }
  const body = input as Record<string, unknown>;

  const text = body.text;
  if (typeof text !== "string" || text.trim() === "") {
    throw new Error('Field "text" is required and must be a non-empty string.');
  }

  const now = new Date();
  const asString = (value: unknown, fallback: string): string =>
    typeof value === "string" && value.trim() !== "" ? value : fallback;

  const message: CanonicalMessage = {
    source: "whatsapp-personal",
    // A caller that omits an id gets one derived from the content, so repeated
    // posts of the same fixture exercise the duplicate path.
    messageId: asString(body.messageId, `local-${hash(text)}`),
    chatId: asString(body.chatId, "local@s.whatsapp.net"),
    senderId: asString(body.senderId, "local"),
    sentAt: asString(body.sentAt, now.toISOString()),
    receivedAt: asString(body.receivedAt, now.toISOString()),
    text,
  };
  if (typeof body.senderName === "string") message.senderName = body.senderName;
  return message;
}

function hash(value: string): string {
  let h = 0;
  for (let i = 0; i < value.length; i++) h = (Math.imul(31, h) + value.charCodeAt(i)) | 0;
  return Math.abs(h).toString(36);
}

export function createIngestServer() {
  return createServer((req, res) => {
    void (async () => {
      try {
        if (req.method === "GET" && req.url === "/health") {
          return send(res, 200, {
            status: "ok",
            dryRun: config.dryRun,
            whatsappEnabled: config.whatsappEnabled,
            tzOffsetMinutes: config.tzOffsetMinutes,
          });
        }

        if (req.method === "POST" && req.url === "/ingest") {
          const outcome = await ingest(toCanonical(await readBody(req)));
          const status = outcome.status === "error" ? 500 : 200;
          return send(res, status, outcome);
        }

        return send(res, 404, { error: "Not found. Try GET /health or POST /ingest." });
      } catch (error) {
        return send(res, 400, {
          error: error instanceof Error ? error.message : String(error),
        });
      }
    })();
  });
}

export function startIngestServer(): Promise<number> {
  const server = createIngestServer();
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(config.port, "127.0.0.1", () => resolve(config.port));
  });
}
