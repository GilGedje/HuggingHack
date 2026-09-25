import type { Element, ElementContent, Root } from 'hast'

/** GitHub alert kinds, which Hugging Face model cards use too, and their titles. */
export const ALERT_TITLES = {
  note: 'Note',
  tip: 'Tip',
  important: 'Important',
  warning: 'Warning',
  caution: 'Caution',
} as const

export type AlertKind = keyof typeof ALERT_TITLES

// The marker opens a quote's first line and is alone on it, in any letter case.
const MARKER = /^[ \t]*\[!(note|tip|important|warning|caution)\][ \t]*(?:\n|$)/i

function isElement(node: ElementContent | undefined): node is Element {
  return node?.type === 'element'
}

/** Turns `> [!NOTE]` quotes into callouts: the marker becomes a title and the
 * quote gets `markdown-alert markdown-alert-<kind>` classes. */
function alertify(quote: Element): void {
  const first = quote.children.find(isElement)
  if (!first || first.tagName !== 'p') return
  const lead = first.children[0]
  if (lead?.type !== 'text') return
  const match = lead.value.match(MARKER)
  if (!match) return
  const kind = match[1].toLowerCase() as AlertKind
  lead.value = lead.value.slice(match[0].length)
  if (!lead.value) first.children.shift()
  // `> [!NOTE]` followed by a blank quote line leaves the first paragraph empty.
  const onlySpace = first.children.every((child) => child.type === 'text' && !child.value.trim())
  if (onlySpace) quote.children.splice(quote.children.indexOf(first), 1)
  quote.properties = { ...quote.properties, className: ['markdown-alert', `markdown-alert-${kind}`] }
  const title: Element = {
    type: 'element',
    tagName: 'p',
    properties: { className: ['markdown-alert-title'], dataAlert: kind },
    children: [{ type: 'text', value: ALERT_TITLES[kind] }],
  }
  quote.children.unshift(title)
}

function visit(node: Root | Element): void {
  for (const child of node.children) {
    if (child.type !== 'element') continue
    if (child.tagName === 'blockquote') alertify(child)
    visit(child)
  }
}

/**
 * Rehype plugin for GitHub alert syntax. It runs after rehype-sanitize and only
 * adds fixed class names and plain text, so it opens no way around sanitizing.
 */
export function rehypeGithubAlerts() {
  return (tree: Root) => {
    visit(tree)
  }
}
