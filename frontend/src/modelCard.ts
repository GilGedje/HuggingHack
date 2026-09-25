import { defaultSchema } from 'rehype-sanitize'

const CODE_TOKEN_START = '\uE000HUGGINGHACK_CODE_'
const CODE_TOKEN_END = '\uE001'

function protectCode(markdown: string): { source: string; restore: (value: string) => string } {
  const protectedSegments: string[] = []
  const stash = (value: string) => {
    const token = `${CODE_TOKEN_START}${protectedSegments.length}${CODE_TOKEN_END}`
    protectedSegments.push(value)
    return token
  }

  // Raw HTML code blocks reach rehype-raw later in the pipeline, so protect them
  // before handling Markdown fences and spans.
  const withoutRawCode = markdown.replace(
    /<(pre|code)\b[^>]*>[\s\S]*?<\/\1\s*>/gi,
    (value) => stash(value),
  )
  const lines = withoutRawCode.split(/(?<=\n)/)
  const output: string[] = []

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    const opener = line.match(/^ {0,3}(`{3,}|~{3,})/)
    if (opener) {
      const marker = opener[1][0]
      const minimumLength = opener[1].length
      const escapedMarker = marker === '`' ? '`' : '~'
      const closer = new RegExp(`^ {0,3}${escapedMarker}{${minimumLength},}[\\t ]*(?:\\n)?$`)
      let block = line

      while (index + 1 < lines.length) {
        index += 1
        block += lines[index]
        if (closer.test(lines[index])) break
      }

      output.push(stash(block))
      continue
    }

    if (/^(?: {4}|\t)/.test(line)) {
      output.push(stash(line))
      continue
    }

    output.push(line.replace(/(`+)([\s\S]*?)\1/g, (value) => stash(value)))
  }

  return {
    source: output.join(''),
    restore: (value) =>
      value.replace(
        new RegExp(`${CODE_TOKEN_START}(\\d+)${CODE_TOKEN_END}`, 'g'),
        (_token, rawIndex: string) => protectedSegments[Number(rawIndex)] || '',
      ),
  }
}

export function stripModelCardFrontmatter(source: string): string {
  const normalized = source.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n')
  const lines = normalized.split('\n')
  if (lines[0]?.trim() !== '---') return normalized

  const closingIndex = lines.findIndex(
    (line, index) => index > 0 && /^(?:---|\.\.\.)[ \t]*$/.test(line),
  )
  return closingIndex > 0 ? lines.slice(closingIndex + 1).join('\n') : normalized
}

export function prepareModelCardMarkdown(source: string): string {
  const withoutFrontmatter = stripModelCardFrontmatter(source)
  const protectedCode = protectCode(withoutFrontmatter)
  const normalizedMath = protectedCode.source
    .replace(
      /\\\[([\s\S]*?)\\\]/g,
      (_match, math: string) => `\n$$\n${math.trim()}\n$$\n`,
    )
    // With single-dollar math disabled, a double-dollar inline delimiter keeps
    // currency such as "$5 and $10" as prose while still rendering \(...\).
    .replace(/\\\((.+?)\\\)/g, (_match, math: string) => `$$${math.trim()}$$`)

  return protectedCode.restore(normalizedMath).trim()
}

export function resolveModelCardUrl(
  value: string,
  attribute: string,
  sourceUrl: string,
  revision: string,
): string | null {
  const url = value.trim()
  if (!url) return ''
  if (url.startsWith('#')) return url

  const explicitScheme = url.match(/^([a-z][a-z\d+.-]*):/i)?.[1]?.toLowerCase()
  if (explicitScheme && !['http', 'https', 'mailto'].includes(explicitScheme)) return null
  if (explicitScheme || url.startsWith('//')) return url

  try {
    const route = attribute === 'src' ? 'resolve' : 'blob'
    const encodedRevision = revision
      .split('/')
      .map((part) => encodeURIComponent(part))
      .join('/')
    const base = `${sourceUrl.replace(/\/+$/, '')}/${route}/${encodedRevision}/`
    return new URL(url, base).toString()
  } catch {
    return null
  }
}

/**
 * Resolve model card URLs for a repository served from the local library.
 *
 * Relative images load through the library asset endpoint. External images are
 * dropped because an air-gapped browser cannot reach them, and relative links to
 * other repository files have no page to open, so they are removed too.
 */
export function resolveLocalModelCardUrl(
  value: string,
  attribute: string,
  repoId: string,
): string | null {
  const url = value.trim()
  if (!url) return ''
  if (url.startsWith('#')) return url

  const explicitScheme = url.match(/^([a-z][a-z\d+.-]*):/i)?.[1]?.toLowerCase()
  if (explicitScheme || url.startsWith('//')) {
    if (attribute === 'src') return null
    return explicitScheme && ['http', 'https', 'mailto'].includes(explicitScheme) ? url : null
  }
  if (attribute !== 'src') return null

  try {
    const resolved = new URL(url, 'http://repository.invalid/')
    const path = decodeURIComponent(resolved.pathname.replace(/^\/+/, ''))
    if (!path) return null
    const params = new URLSearchParams({ repo_id: repoId, path })
    return `/api/library/asset?${params.toString()}`
  } catch {
    return null
  }
}

export function modelCardHeadingId(value: string): string {
  return value
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^\p{Letter}\p{Number}\s_-]/gu, '')
    .trim()
    .replace(/\s+/g, '-')
}

const attributes = defaultSchema.attributes || {}

/** `srcset` never passes through the URL check that `src` and `href` get, so a
 * `<picture>` source could load any external address; it is dropped. */
const withoutSrcSet = (list: NonNullable<typeof attributes.img> = []) =>
  list.filter((attribute) => (Array.isArray(attribute) ? attribute[0] : attribute) !== 'srcSet')

export const modelCardSanitizeSchema = {
  ...defaultSchema,
  attributes: {
    ...attributes,
    div: [...(attributes.div || []), 'align'],
    p: [...(attributes.p || []), 'align'],
    img: [...withoutSrcSet(attributes.img), 'align', 'height', 'width'],
    source: withoutSrcSet(attributes.source),
    details: [...(attributes.details || []), 'open'],
  },
}
