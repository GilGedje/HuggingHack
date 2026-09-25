const FOCUSABLE = 'button:not(:disabled), a[href], select:not(:disabled), input:not(:disabled)'

/**
 * Picks where keyboard focus goes once the row holding `trigger` leaves its list,
 * so it does not fall back to the page: the same control in the next row (or the
 * previous one at the end of the list), else anything focusable there, else the
 * heading of the section around the list. Call it before the row goes; the
 * function it returns moves focus once the removal has happened.
 */
export function focusAfterRemoval(trigger: HTMLElement | null, row = trigger?.closest('li, [role="row"]')) {
  const neighbour = row?.nextElementSibling || row?.previousElementSibling
  const controls = neighbour ? [...neighbour.querySelectorAll<HTMLElement>(FOCUSABLE)] : []
  const target =
    controls.find((control) => trigger && control.className === trigger.className && control.tagName === trigger.tagName) ||
    controls[0] ||
    null
  const heading = row?.closest('section')?.querySelector<HTMLElement>('h2, h3') || null
  // A list that is busy may still have its controls disabled for a frame or two.
  const move = (tries: number) => {
    const next = target?.isConnected ? target : heading
    if (!next) return
    if ((next as HTMLButtonElement).disabled && tries > 0) {
      requestAnimationFrame(() => move(tries - 1))
      return
    }
    if (next === heading && !next.hasAttribute('tabindex')) next.tabIndex = -1
    next.focus()
  }
  return () => move(10)
}
