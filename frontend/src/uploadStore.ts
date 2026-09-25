/**
 * Unfinished uploads are remembered in localStorage, which every tab of the site
 * shares. Each tab saves its jobs under a key of its own that no other tab writes,
 * with the time it last said it was alive. Another tab therefore never mistakes
 * them for interrupted, discards their change session, or writes over them: it
 * only takes over the jobs of a tab that is gone.
 */

/** Tabs refresh their record this often while they have unfinished jobs. */
export const LEASE_HEARTBEAT_MS = 20_000
/** Long enough to outlast the once-a-minute timers of a throttled background tab. */
export const LEASE_TTL_MS = 120_000

/** What one tab saves: its unfinished jobs, and when it was last alive
 * (0 once it has closed or reloaded). */
export interface TabRecord<T> {
  seen: number
  jobs: T[]
}

export function tabGone(seen: number, now: number): boolean {
  return !seen || now - seen >= LEASE_TTL_MS
}

/** The jobs of tabs that are gone (closed, reloaded, crashed), and the keys they
 * were saved under, to remove once this tab has saved the jobs as its own. */
export function claimOrphans<T extends { id: string }>(
  records: Array<[key: string, record: TabRecord<T>]>,
  ownKey: string,
  now: number,
): { jobs: T[]; keys: string[] } {
  const jobs: T[] = []
  const keys: string[] = []
  const seen = new Set<string>()
  for (const [key, record] of records) {
    if (key === ownKey || !tabGone(record.seen, now)) continue
    keys.push(key)
    for (const job of record.jobs) {
      if (seen.has(job.id)) continue
      seen.add(job.id)
      jobs.push(job)
    }
  }
  return { jobs, keys }
}

/** Reads a stored record, tolerating the unreadable and the old single-list
 * format (saved before tabs had keys, so always left behind). */
export function parseTabRecord<T>(raw: string | null): TabRecord<T> | null {
  if (!raw) return null
  try {
    const value: unknown = JSON.parse(raw)
    if (Array.isArray(value)) return { seen: 0, jobs: value as T[] }
    if (value && typeof value === 'object' && Array.isArray((value as TabRecord<T>).jobs)) {
      const record = value as TabRecord<T>
      return { seen: Number(record.seen) || 0, jobs: record.jobs }
    }
  } catch {
    // Unreadable: the caller treats it as an empty record left behind.
  }
  return null
}
