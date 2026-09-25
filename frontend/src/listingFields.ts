/** Reading and tidying the listing fields people may correct. */

type Field = 'pipeline_tag' | 'precision' | 'parameter_count' | 'library_name' | 'license' | 'tags'
type Values = Partial<Record<Field, unknown>>

const UNITS: Record<string, number> = { K: 1e3, M: 1e6, B: 1e9, T: 1e12 }

/** "8B", "595M", "1.5T", or a plain count; null when it is not a size. */
export function parseParameters(text: string): number | null {
  const match = text.trim().replace(/,/g, '').match(/^(\d+(?:\.\d+)?)\s*([KMBT])?$/i)
  if (!match) return null
  const value = Math.round(Number(match[1]) * (match[2] ? UNITS[match[2].toUpperCase()] : 1))
  return Number.isSafeInteger(value) && value > 0 ? value : null
}

/** A count written the way people write model sizes: 8B, 595M. */
export function writeParameters(count: number): string {
  for (const unit of ['T', 'B', 'M', 'K'] as const) {
    if (count >= UNITS[unit]) return `${Number((count / UNITS[unit]).toFixed(2))}${unit}`
  }
  return String(count)
}

/** "llama, fp8" as a list, without blanks or repeats. */
export function parseTags(text: string): string[] {
  const tags: string[] = []
  for (const tag of text.split(',').map((item) => item.trim()).filter(Boolean)) {
    if (!tags.includes(tag)) tags.push(tag)
  }
  return tags
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
}

/** Only real changes: a correction equal to what the files say is dropped. */
export function tidyOverrides<T extends Values>(overrides: T, detected: Values): T {
  return Object.fromEntries(
    Object.entries(overrides).filter(([field, value]) => value != null && !same(value, detected[field as Field])),
  ) as T
}

/** The same corrections, whatever order their fields were set in. */
export function sameOverrides(a: Values, b: Values): boolean {
  const sorted = (values: Values) => Object.entries(values).sort(([x], [y]) => x.localeCompare(y))
  return JSON.stringify(sorted(a)) === JSON.stringify(sorted(b))
}
