/** Lines up the results of config revisions so the better one is easy to spot. */

type Direction = 'higher' | 'lower' | null

interface Metric {
  id: string
  label: string
  unit: string
  better: Direction
  group: string
}

interface Results {
  values: Record<string, number>
  custom: Array<{ name: string; value: number; unit: string; better: Direction }>
}

interface Revision {
  id: string
  results: Results
}

interface CompareRow {
  key: string
  label: string
  unit: string
  better: Direction
  context: boolean
  values: Record<string, number | undefined>
  /** Revisions holding the best value; empty when there is nothing to compare. */
  best: string[]
}

/** Which of the values wins. Needs a direction and at least two revisions that
 * measured it; ties are all best. */
export function bestOf(values: Record<string, number | undefined>, better: Direction): string[] {
  const measured = Object.entries(values).filter((entry): entry is [string, number] => entry[1] != null)
  if (!better || measured.length < 2) return []
  const target = better === 'higher'
    ? Math.max(...measured.map(([, value]) => value))
    : Math.min(...measured.map(([, value]) => value))
  if (measured.every(([, value]) => value === target)) return []
  return measured.filter(([, value]) => value === target).map(([id]) => id)
}

/** One row per metric that any revision measured: the test's context first, then
 * the known metrics in their order, then custom metrics by name. */
export function compareRows(revisions: Revision[], metrics: Metric[]): CompareRow[] {
  const rows: CompareRow[] = []
  const ordered = [...metrics.filter((metric) => metric.group === 'context'), ...metrics.filter((metric) => metric.group !== 'context')]
  for (const metric of ordered) {
    const values = Object.fromEntries(revisions.map((revision) => [revision.id, revision.results.values?.[metric.id]]))
    if (Object.values(values).every((value) => value == null)) continue
    rows.push({
      key: metric.id,
      label: metric.label,
      unit: metric.unit,
      better: metric.better,
      context: metric.group === 'context',
      values,
      best: bestOf(values, metric.better),
    })
  }
  const custom = new Map<string, CompareRow>()
  for (const revision of revisions) {
    for (const item of revision.results.custom || []) {
      const key = `custom:${item.name.trim().toLowerCase()}`
      let row = custom.get(key)
      if (!row) {
        row = { key, label: item.name, unit: item.unit, better: item.better, context: false, values: {}, best: [] }
        custom.set(key, row)
      }
      row.unit ||= item.unit
      row.better ||= item.better
      row.values[revision.id] = item.value
    }
  }
  for (const row of custom.values()) {
    for (const revision of revisions) row.values[revision.id] ??= undefined
    row.best = bestOf(row.values, row.better)
    rows.push(row)
  }
  return rows
}

export function formatValue(value: number | undefined, unit: string): string {
  if (value == null) return '—'
  const digits = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2
  const number = value.toLocaleString('en-US', { maximumFractionDigits: digits })
  if (!unit) return number
  return unit === '%' || unit === '×' ? `${number}${unit}` : `${number} ${unit}`
}

const HEADLINE = ['output_tps', 'per_user_tps', 'ttft_ms', 'acceptance_rate', 'kv_cache_tokens', 'tpot_ms']

/** The few numbers worth showing on a revision in a list. */
export function headlineMetrics(results: Results, metrics: Metric[], limit = 3): Array<{ label: string; text: string }> {
  const chips = []
  for (const id of HEADLINE) {
    const value = results.values?.[id]
    const metric = metrics.find((item) => item.id === id)
    if (value == null || !metric) continue
    chips.push({ label: metric.label, text: formatValue(value, metric.unit) })
    if (chips.length === limit) break
  }
  return chips
}
