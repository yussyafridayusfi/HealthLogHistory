import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";

/**
 * Idempotency store.
 *
 * WhatsApp (and unofficial clients in particular) can deliver the same message
 * more than once, and a reconnected socket will replay recent history. Without
 * this, a redelivery silently duplicates a medical reading.
 *
 * A JSON file keyed by message id is enough at this volume. Swap in SQLite or
 * Redis if the log grows or more than one process ever runs.
 */
const MAX_ENTRIES = 5_000;

export class DedupeStore {
  #file: string;
  #seen: Set<string> | null = null;

  constructor(file: string) {
    this.#file = file;
  }

  async #load(): Promise<Set<string>> {
    if (this.#seen) return this.#seen;
    try {
      const raw = JSON.parse(await readFile(this.#file, "utf8")) as { ids?: string[] };
      this.#seen = new Set(raw.ids ?? []);
    } catch {
      this.#seen = new Set();
    }
    return this.#seen;
  }

  /** Returns true when this message id has already been handled. */
  async has(messageId: string): Promise<boolean> {
    return (await this.#load()).has(messageId);
  }

  /** Marks a message id handled. Call only after a successful append. */
  async add(messageId: string): Promise<void> {
    const seen = await this.#load();
    seen.add(messageId);
    const ids = [...seen];
    const trimmed = ids.length > MAX_ENTRIES ? ids.slice(ids.length - MAX_ENTRIES) : ids;
    this.#seen = new Set(trimmed);
    await mkdir(dirname(this.#file), { recursive: true });
    await writeFile(this.#file, JSON.stringify({ ids: trimmed }, null, 2), "utf8");
  }
}
