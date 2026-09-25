/** Small Markdown helpers for text people write on the site, like an
 * organization's About section. */

/** The first thing the text says, as plain text: for cards and lists that show
 * one line. Markdown marks, links, and images are reduced to their words. */
export function markdownSummary(source: string, max = 180): string {
  const line = source
    .split('\n')
    .map((text) =>
      text
        .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
        .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
        .replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/, '')
        .replace(/(\*\*|__|\*|_|~~|`)(.+?)\1/g, '$2')
        .replace(/<[^>]+>/g, '')
        .trim(),
    )
    .find((text) => text && !/^([-*_])\1{2,}$/.test(text))
  if (!line) return ''
  return line.length > max ? `${line.slice(0, max - 1).trimEnd()}…` : line
}

export type MarkdownFormat = 'bold' | 'italic' | 'heading' | 'bullets' | 'numbers' | 'link' | 'code'

const WRAPS: Partial<Record<MarkdownFormat, [string, string, string]>> = {
  bold: ['**', '**', 'bold text'],
  italic: ['_', '_', 'italic text'],
  code: ['`', '`', 'code'],
  link: ['[', '](https://)', 'link text'],
}

const PREFIXES: Partial<Record<MarkdownFormat, (index: number) => string>> = {
  heading: () => '## ',
  bullets: () => '- ',
  numbers: (index) => `${index + 1}. `,
}

/**
 * Apply a toolbar format to the selection: wrap it (bold, italic, code, link)
 * or prefix each selected line (heading, lists). Returns the new text and the
 * selection to restore, so typing continues where it makes sense.
 */
export function applyFormat(
  text: string,
  start: number,
  end: number,
  format: MarkdownFormat,
): { text: string; start: number; end: number } {
  const wrap = WRAPS[format]
  if (wrap) {
    const [open, close, placeholder] = wrap
    const selected = text.slice(start, end) || placeholder
    const next = text.slice(0, start) + open + selected + close + text.slice(end)
    if (format === 'link' && end > start) {
      // Words are there already; select the address to replace.
      const address = start + open.length + selected.length + 2
      return { text: next, start: address, end: address + 'https://'.length }
    }
    return { text: next, start: start + open.length, end: start + open.length + selected.length }
  }
  const prefix = PREFIXES[format]!
  const lineStart = text.lastIndexOf('\n', start - 1) + 1
  const lineEnd = text.indexOf('\n', Math.max(end, start + (end > start ? -1 : 0)))
  const blockEnd = lineEnd === -1 ? text.length : lineEnd
  const lines = text.slice(lineStart, blockEnd).split('\n')
  const changed = lines.map((line, index) => prefix(index) + line).join('\n')
  const next = text.slice(0, lineStart) + changed + text.slice(blockEnd)
  return { text: next, start: lineStart + prefix(0).length, end: lineStart + changed.length }
}
