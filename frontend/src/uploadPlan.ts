/**
 * Checks a model folder before it is uploaded: which files to send, which to
 * skip, whether the essentials are there, and what precision the weights use.
 */

/** Folders the server refuses, plus caches no model needs. */
const SKIPPED_FOLDERS = new Set(['.git', '.cache', '__pycache__'])
/** Files the server refuses or that operating systems leave behind. */
const SKIPPED_FILES = new Set(['.hugginghack.json', '.DS_Store', 'Thumbs.db', 'desktop.ini'])
/** Unfinished-transfer names the server keeps hidden. Mirrors `indexer.PART_SUFFIXES`. */
const PART_SUFFIXES = ['.hugginghack-part', '.hugginghack-s3-part']
/** Control characters (a line break, a tab) can be stored but never requested by name. */
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/
const TOKENIZER_FILES = ['tokenizer.json', 'tokenizer_config.json', 'tokenizer.model']

interface PlannedEntry {
  /** Path inside the repository, with forward slashes. */
  path: string
  size: number
}

interface UploadChecks {
  config: boolean
  tokenizer: boolean
  weights: boolean
  card: boolean
}

interface UploadPlan<T extends PlannedEntry> {
  files: T[]
  skipped: T[]
  totalBytes: number
  weightBytes: number
  weightFiles: number
  checks: UploadChecks
}

export function isSkipped(path: string): boolean {
  const parts = path.split('/')
  const name = parts[parts.length - 1]
  return (
    parts.slice(0, -1).some((part) => SKIPPED_FOLDERS.has(part)) ||
    SKIPPED_FILES.has(name) ||
    PART_SUFFIXES.some((suffix) => name.endsWith(suffix)) ||
    CONTROL_CHARACTERS.test(path)
  )
}

/** Files the server leaves out of a commit's record and the file count: any
 * path with a part that starts with a dot, like `.gitattributes`, or a
 * `__pycache__` folder. They are still stored. Mirrors `indexer.hidden_path`. */
export function isRecorded(path: string): boolean {
  const parts = path.split('/').filter(Boolean)
  const name = parts[parts.length - 1] || ''
  return !(
    parts.some((part) => part.startsWith('.') || part === '__pycache__') ||
    name.endsWith('.hugginghack-part') ||
    name.endsWith('.hugginghack-s3-part')
  )
}

/** The default commit message for uploading these paths, counting only the
 * files the commit will list. */
export function uploadCommitMessage(paths: string[]): string {
  const count = paths.filter(isRecorded).length
  return count ? `Upload ${count} file${count === 1 ? '' : 's'}` : 'Upload files'
}

export function planUpload<T extends PlannedEntry>(entries: T[]): UploadPlan<T> {
  const files = entries.filter((entry) => !isSkipped(entry.path))
  const skipped = entries.filter((entry) => isSkipped(entry.path))
  const top = new Set(files.filter((entry) => !entry.path.includes('/')).map((entry) => entry.path))
  const weights = files.filter((entry) => entry.path.toLowerCase().endsWith('.safetensors'))
  return {
    files,
    skipped,
    totalBytes: files.reduce((sum, entry) => sum + entry.size, 0),
    weightBytes: weights.reduce((sum, entry) => sum + entry.size, 0),
    weightFiles: weights.length,
    checks: {
      config: top.has('config.json'),
      tokenizer: TOKENIZER_FILES.some((name) => top.has(name)),
      weights: weights.length > 0,
      card: [...top].some((name) => name.toLowerCase() === 'readme.md'),
    },
  }
}

type Json = Record<string, unknown>

function object(value: unknown): Json {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Json) : {}
}

const CONFIG_DTYPES: Record<string, string> = { bfloat16: 'bf16', float16: 'fp16', float32: 'fp32' }

/**
 * The weights' number format from `config.json` and ModelOpt's
 * `hf_quant_config.json`, following the server's rules: quantization wins over
 * `torch_dtype`, which in FP8 and NVFP4 checkpoints only covers unquantized layers.
 */
export function detectPrecision(configValue: unknown, quantFileValue: unknown = {}): string | null {
  const config = object(configValue)
  const textConfig = object(config.text_config)
  const quantization = object(config.quantization_config ?? textConfig.quantization_config)
  const modelopt = object(object(quantFileValue).quantization)
  if (Object.keys(quantization).length || Object.keys(modelopt).length) {
    const method = String(quantization.quant_method ?? '').toLowerCase()
    const layout = String(quantization.format ?? '').toLowerCase()
    if (method === 'fp8' || method === 'mxfp4') return method
    const algorithms = new Set(
      [quantization.quant_algo, modelopt.quant_algo].filter(Boolean).map((value) => String(value).toUpperCase()),
    )
    for (const layer of Object.values(object(modelopt.quantized_layers))) {
      const algo = object(layer).quant_algo
      if (algo) algorithms.add(String(algo).toUpperCase())
    }
    for (const group of Object.values(object(quantization.config_groups))) {
      const weights = object(group).weights ? object(object(group).weights) : object(group)
      const bits = weights.num_bits
      if (weights.type === 'float' && (bits === 4 || bits === 8)) algorithms.add(bits === 4 ? 'NVFP4' : 'FP8')
      else if (weights.type === 'int' && (bits === 4 || bits === 8)) algorithms.add(`INT${bits}`)
    }
    const names = [...algorithms]
    if (layout.includes('nvfp4') || names.some((name) => name.includes('NVFP4') || name === 'FP4')) return 'nvfp4'
    if (layout.includes('float-quantized') || names.some((name) => name.startsWith('FP8'))) return 'fp8'
    if (method === 'gptq' || method === 'awq') return quantization.bits === 8 ? 'int8' : 'int4'
    if (method === 'bitsandbytes') return quantization.load_in_4bit ? 'int4' : 'int8'
    if (names.some((name) => name.includes('INT4') || name.includes('W4A16'))) return 'int4'
    if (names.some((name) => name.includes('INT8') || name.includes('W8A8'))) return 'int8'
  }
  for (const source of [config, textConfig]) {
    const dtype = source.torch_dtype ?? source.dtype
    if (typeof dtype === 'string') {
      const name = dtype.replace(/^torch\./, '')
      if (CONFIG_DTYPES[name]) return CONFIG_DTYPES[name]
    }
  }
  return null
}
