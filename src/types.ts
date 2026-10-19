/**
 * The canonical inbound event.
 *
 * Deliberately independent of Baileys: the WhatsApp client's job is only to
 * turn a transport-specific message into this shape. Everything downstream
 * (parse -> dedupe -> append) is testable from a fixture file, which is what
 * lets the Postman collection drive the pipeline without WhatsApp running.
 */
export type CanonicalMessage = {
  source: "whatsapp-personal";
  /** WhatsApp message id. The idempotency key. */
  messageId: string;
  /** Chat the message belongs to, e.g. 6281234567890@s.whatsapp.net */
  chatId: string;
  /** Author of the message. */
  senderId: string;
  senderName?: string;
  /** ISO 8601 UTC, derived from the message's own timestamp. */
  sentAt: string;
  /** ISO 8601 UTC, when this process first saw it. */
  receivedAt: string;
  text: string;
};

export type Unit = "mg/dL" | "mmol/L";

export type Reading = {
  /** ISO 8601 UTC, built from the parsed date + "time check". */
  measuredAt: string;
  bloodSugar: number;
  unit: Unit;
  /** "HH:MM" local as written by the sender, or null when not supplied. */
  timeCheckLocal: string | null;
  notes: string | null;
};

export type ParseResult =
  | { ok: true; reading: Reading; warnings: string[] }
  | { ok: false; reason: string; warnings: string[] };

export type IngestOutcome =
  | { status: "appended"; row: string[]; warnings: string[]; reading: Reading }
  | { status: "duplicate"; messageId: string }
  | { status: "unparsed"; reason: string; warnings: string[] }
  | { status: "error"; reason: string };
