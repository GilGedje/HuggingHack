/** Page numbers to show, with null where a run of pages is skipped. */
export function pageList(current: number, total: number): Array<number | null> {
  if (total <= 7) return Array.from({ length: Math.max(total, 1) }, (_, index) => index + 1)
  const wanted = new Set([1, total, current - 1, current, current + 1].filter((page) => page >= 1 && page <= total))
  if (current <= 3) [2, 3, 4].forEach((page) => page <= total && wanted.add(page))
  if (current >= total - 2) [total - 3, total - 2, total - 1].forEach((page) => page >= 1 && wanted.add(page))
  const pages = [...wanted].sort((a, b) => a - b)
  const result: Array<number | null> = []
  pages.forEach((page, index) => {
    if (index && page - pages[index - 1] > 1) result.push(page - pages[index - 1] === 2 ? page - 1 : null)
    result.push(page)
  })
  return result
}
