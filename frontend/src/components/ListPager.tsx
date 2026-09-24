import { useCallback, useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronLeft, ChevronRight } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { pageList } from '../pagination'

export const PAGE_SIZES = [10, 25, 50, 100]
const DEFAULT_PAGE_SIZE = 25

function storedPageSize(key: string): number {
  try {
    const value = Number(window.localStorage.getItem(key))
    return PAGE_SIZES.includes(value) ? value : DEFAULT_PAGE_SIZE
  } catch {
    return DEFAULT_PAGE_SIZE
  }
}

export type Paged<F> = F & { page: number; per_page: number }

/**
 * List filters, page, and page size kept in the address, so reload, Back, and
 * shared links keep them. `defaults` names the text filters; `choices` limits
 * any of them to known values. The page size is also remembered per browser
 * under `sizeKey`. The search box follows `q` with a short debounce.
 */
export function useListQuery<F extends { q: string } & Record<string, string>>(
  defaults: F,
  sizeKey: string,
  choices: Partial<Record<keyof F, readonly string[]>> = {},
) {
  const [params, setParams] = useSearchParams()
  const query = useMemo(() => {
    const values = { ...defaults }
    for (const key of Object.keys(defaults) as Array<keyof F & string>) {
      const value = params.get(key)
      const allowed = choices[key]
      if (value !== null && (!allowed || allowed.includes(value))) values[key] = value as F[typeof key]
    }
    const perPage = Number(params.get('per_page'))
    return {
      ...values,
      page: Math.max(1, Math.floor(Number(params.get('page'))) || 1),
      per_page: PAGE_SIZES.includes(perPage) ? perPage : storedPageSize(sizeKey),
    } as Paged<F>
    // defaults and choices are constants at each call site.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, sizeKey])

  const update = useCallback(
    (changes: Partial<Paged<F>>, replace = false) => {
      const next = { ...query, page: 1, ...changes }
      const values = new URLSearchParams()
      for (const key of Object.keys(defaults)) {
        const value = String(next[key] ?? '').trim()
        if (value && value !== defaults[key]) values.set(key, value)
      }
      if (next.page > 1) values.set('page', String(next.page))
      if (next.per_page !== DEFAULT_PAGE_SIZE) values.set('per_page', String(next.per_page))
      setParams(values, { replace })
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [query, setParams],
  )

  const [search, setSearch] = useState(query.q)
  // The address is the source of truth: Back, Forward, or a tab link replace
  // the search box text instead of the box re-applying an old search.
  useEffect(() => {
    setSearch((current) => (current.trim() === query.q ? current : query.q))
  }, [query.q])
  useEffect(() => {
    if (search.trim() === query.q) return
    const timer = window.setTimeout(() => update({ q: search } as Partial<Paged<F>>, true), 250)
    return () => window.clearTimeout(timer)
  }, [search, query.q, update])

  const changePageSize = useCallback(
    (value: number) => {
      try {
        window.localStorage.setItem(sizeKey, String(value))
      } catch {
        // Remembering the page size is a convenience only.
      }
      // Keep the first row on screen in view after the page size changes.
      const firstIndex = (query.page - 1) * query.per_page
      update({ per_page: value, page: Math.floor(firstIndex / value) + 1 } as Partial<Paged<F>>)
    },
    [query.page, query.per_page, sizeKey, update],
  )

  return { query, update, search, setSearch, changePageSize }
}

interface ListPagerProps {
  page: number
  pages: number
  perPage: number
  total: number
  label: string
  onPage: (page: number) => void
  onPageSize: (size: number) => void
}

export function ListPager({ page, pages, perPage, total, label, onPage, onPageSize }: ListPagerProps) {
  if (total <= 0) return null
  return (
    <div className="admin-user-pager">
      <span className="admin-user-muted">
        Showing {(page - 1) * perPage + 1}–{Math.min(page * perPage, total)} of {total}
      </span>
      <label className="pager-size">
        Rows per page
        <span className="sort-control compact">
          <select value={perPage} onChange={(event) => onPageSize(Number(event.target.value))} aria-label="Rows per page">
            {PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
          </select>
          <ChevronDown size={14} />
        </span>
      </label>
      {pages > 1 && (
        <nav className="pager" aria-label={label}>
          <button type="button" disabled={page <= 1} onClick={() => onPage(page - 1)} aria-label="Previous page">
            <ChevronLeft size={15} />
          </button>
          {pageList(page, pages).map((item, index) =>
            item === null ? (
              <span key={`gap-${index}`} className="pager-gap">…</span>
            ) : (
              <button
                key={item}
                type="button"
                className={item === page ? 'current' : undefined}
                aria-current={item === page ? 'page' : undefined}
                onClick={() => onPage(item)}
              >
                {item}
              </button>
            ),
          )}
          <button type="button" disabled={page >= pages} onClick={() => onPage(page + 1)} aria-label="Next page">
            <ChevronRight size={15} />
          </button>
        </nav>
      )}
    </div>
  )
}
