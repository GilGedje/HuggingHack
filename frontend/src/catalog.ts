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

export const PRECISIONS: Array<[string, string]> = [
  ['bf16', 'BF16'],
  ['fp8', 'FP8'],
  ['nvfp4', 'NVFP4'],
]

const PRECISION_LABELS: Record<string, string> = {
  ...Object.fromEntries(PRECISIONS),
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
