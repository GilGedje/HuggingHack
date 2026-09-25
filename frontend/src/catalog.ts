/** Filter definitions for the model library. Task ids and labels follow the
 * Hugging Face Hub (huggingface.co/api/tasks). */

export const TASKS = [
  'text-generation',
  'image-text-to-text',
  'any-to-any',
  'feature-extraction',
  'sentence-similarity',
  'text-ranking',
]

/** Precision filter options. Formats of the same width share one, so INT8 models
 * sit with FP8 and INT4, MXFP4, and NVFP4 with FP4; each model keeps its own label. */
export const PRECISION_FILTERS: Array<[string, string]> = [
  ['bf16', 'BF16'],
  ['fp8', 'FP8 / INT8'],
  ['fp4', 'FP4 / INT4'],
]

const PRECISION_LABELS: Record<string, string> = {
  bf16: 'BF16',
  fp8: 'FP8',
  nvfp4: 'NVFP4',
  fp16: 'FP16',
  fp32: 'FP32',
  mxfp4: 'MXFP4',
  int4: 'INT4',
  int8: 'INT8',
}

export function precisionLabel(precision?: string | null): string | null {
  return precision ? PRECISION_LABELS[precision] || precision.toUpperCase() : null
}

/** Slider stops in parameters. The last stop means "and larger". */
export const PARAMETER_STOPS = [0, 1e9, 3e9, 8e9, 14e9, 32e9, 70e9, 128e9, 256e9, 512e9, Infinity]
const LAST_STOP = PARAMETER_STOPS.length - 1

export function stopLabel(index: number): string {
  if (index <= 0) return '0'
  if (index >= LAST_STOP) return `${stopLabel(LAST_STOP - 1)}+`
  const value = PARAMETER_STOPS[index] / 1e9
  return value >= 1000 ? `${value / 1000}T` : `${value}B`
}

export function isFullRange(low: number, high: number): boolean {
  return low <= 0 && high >= LAST_STOP
}

/** The `parameters` query value for a slider range; empty when it covers
 * everything, so models without a known size still show. */
export function parameterQuery(low: number, high: number): string {
  const parts: string[] = []
  if (low > 0) parts.push(`min:${stopLabel(low)}`)
  if (high < LAST_STOP) parts.push(`max:${stopLabel(high)}`)
  return parts.join(',')
}

export function parameterRangeLabel(low: number, high: number): string {
  if (isFullRange(low, high)) return 'Any size'
  if (low === high) return high >= LAST_STOP ? `${stopLabel(high)}` : `About ${stopLabel(low)}`
  if (low <= 0) return `Up to ${stopLabel(high)}`
  if (high >= LAST_STOP) return `${stopLabel(low)} and up`
  return `${stopLabel(low)} – ${stopLabel(high)}`
}

/** The Explore filters as they live in the address. */
export interface CatalogFilters {
  tasks: string[]
  precision: string[]
  hardware: string[]
  /** Slider stop indexes into PARAMETER_STOPS. */
  size: [number, number]
}

/** The address keys the filters use. */
export const CATALOG_FILTER_KEYS = ['task', 'precision', 'hardware', 'size'] as const

function stopIndex(label: string, fallback: number): number {
  if (!label) return fallback
  const index = PARAMETER_STOPS.findIndex((_, stop) => stopLabel(stop).toLowerCase() === label.toLowerCase())
  return index >= 0 ? index : fallback
}

function list(value: string | null): string[] {
  return [...new Set((value || '').split(',').map((item) => item.trim()).filter(Boolean))]
}

/** Reads the filters from `task`, `precision`, `hardware` (comma separated) and
 * `size` (`8B-32B`, `-32B`, or `70B-`); anything unreadable counts as unset. */
export function readCatalogFilters(params: URLSearchParams): CatalogFilters {
  const [low = '', high = ''] = (params.get('size') || '').split('-')
  let size: [number, number] = [stopIndex(low.trim(), 0), stopIndex(high.trim(), LAST_STOP)]
  if (size[0] > size[1]) size = [0, LAST_STOP]
  return {
    tasks: list(params.get('task')),
    precision: list(params.get('precision')),
    hardware: list(params.get('hardware')),
    size,
  }
}

/** Writes the filters over any earlier ones in `params`, leaving other keys alone. */
export function writeCatalogFilters(filters: CatalogFilters, params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params)
  for (const key of CATALOG_FILTER_KEYS) next.delete(key)
  if (filters.tasks.length) next.set('task', filters.tasks.join(','))
  if (filters.precision.length) next.set('precision', filters.precision.join(','))
  if (filters.hardware.length) next.set('hardware', filters.hardware.join(','))
  const [low, high] = filters.size
  // An open end is left empty: `70B-` is 70B and up.
  if (!isFullRange(low, high)) next.set('size', `${low > 0 ? stopLabel(low) : ''}-${high < LAST_STOP ? stopLabel(high) : ''}`)
  return next
}
