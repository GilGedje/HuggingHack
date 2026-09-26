/** The Storage page's parallel-transfer setting: what the server sends to or fetches
 * from a bucket at once, per file. The backend's limits are in main.py
 * (`TransferSettingsRequest`); the page reads them from `transfers.limit`. */

/** A whole number of parts from 1 to `limit`, or null for anything else. */
export function parseParallel(draft: string, limit: number): number | null {
  const text = draft.trim()
  if (!/^\d+$/.test(text)) return null
  const value = Number(text)
  return value >= 1 && value <= limit ? value : null
}

/** Memory a move into a bucket may hold: one part per transfer in flight. */
export function transferMemoryBytes(parallel: number, partSizeMb: number): number {
  return parallel * partSizeMb * 1024 * 1024
}
