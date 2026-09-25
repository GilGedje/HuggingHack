import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'

/** Critically damped feel: fast start, long soft settle, no overshoot. */
const EASE_OUT = 'cubic-bezier(0.22, 1, 0.36, 1)'
/** How long a dialog takes to leave; matches `dialog-out` in styles.css. */
const DIALOG_EXIT_MS = 160
/** How long a toast takes to leave; matches `toast-out` in styles.css. */
export const TOAST_EXIT_MS = 180
/** How long the upload panel takes to leave; matches `.upload-dock.leaving`. */
export const DOCK_EXIT_MS = 180
/** How long the sliding highlight glides; matches `.collection-sidebar[data-highlight='glide']`. */
const HIGHLIGHT_GLIDE_MS = 380

export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

/**
 * Fades the element in whenever `key` changes, so new content arrives instead of
 * popping in. Opacity only: a transform here would become the containing block
 * for fixed-position dialogs inside the element.
 */
export function useFadeOnChange<T extends HTMLElement>(key: string, { initial = false, shift = 0 } = {}) {
  const ref = useRef<T>(null)
  const first = useRef(true)
  useLayoutEffect(() => {
    const skip = first.current && !initial
    first.current = false
    const element = ref.current
    if (skip || !element?.animate || prefersReducedMotion()) return
    // A shift is only there while it plays; nothing is left on the element after.
    const frames = shift
      ? [{ opacity: 0, transform: `translateX(${shift}px)` }, { opacity: 1, transform: 'none' }]
      : [{ opacity: 0 }, { opacity: 1 }]
    const animation = element.animate(frames, { duration: shift ? 320 : 220, easing: EASE_OUT })
    return () => animation.cancel()
    // `initial` only matters on the first run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  return ref
}

/**
 * How far new content drifts in when stepping through `index`: from the right
 * going forward, from the left going back, so it arrives from where it lies.
 */
export function useStepDirection(index: number, distance = 14): number {
  const previous = useRef(index)
  const shift = index === previous.current ? 0 : index > previous.current ? distance : -distance
  useEffect(() => {
    previous.current = index
  }, [index])
  return shift
}

/**
 * A dialog that animates out before it unmounts. `close` plays the exit and then
 * calls `onClose`; calling it again while leaving does nothing. The page stays
 * usable while the dialog leaves (the backdrop stops taking clicks at once).
 * With reduced motion the exit is a short fade (styles.css), so it still waits.
 */
export function useClosingTransition(onClose: () => void) {
  const [closing, setClosing] = useState(false)
  const timer = useRef(0)
  const latest = useRef(onClose)
  latest.current = onClose
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const close = useCallback(() => {
    if (timer.current) return
    setClosing(true)
    timer.current = window.setTimeout(() => latest.current(), DIALOG_EXIT_MS)
  }, [])
  return { closing, close }
}

/** How far in from a scrolled tab strip's faded edge the active tab is kept. */
const TAB_EDGE_ROOM = 28

/**
 * Slides one shared underline to the active tab instead of swapping borders.
 * The returned ref goes on the element whose direct children are the tabs; it
 * gets `--indicator-x`, `--indicator-w` and `--indicator-bottom`, plus
 * `data-indicator` = `still` (placed without motion), `glide` (moves with a
 * transition) or `hidden` (no active tab). Without JavaScript the tabs keep
 * their own borders. `key` must change whenever the active tab or the set of
 * tabs does.
 *
 * A strip too narrow for its tabs scrolls: it gets `data-overflow` = `start`,
 * `end` or `both` for the sides with more tabs (the stylesheet fades those
 * edges), and the active tab is scrolled into view within it.
 */
export function useTabIndicator<T extends HTMLElement>(key: string) {
  const ref = useRef<T>(null)
  const revealed = useRef(false)

  const edges = useCallback(() => {
    const track = ref.current
    if (!track) return
    const room = track.scrollWidth - track.clientWidth
    const start = room > 1 && track.scrollLeft > 1
    const end = room > 1 && track.scrollLeft < room - 1
    if (start || end) track.dataset.overflow = start && end ? 'both' : start ? 'start' : 'end'
    else delete track.dataset.overflow
  }, [])

  // Only the strip scrolls, never the page, so this works on the offsets.
  const reveal = useCallback(() => {
    const track = ref.current
    const active = track?.querySelector<HTMLElement>(':scope > .active')
    if (!track || !active || track.scrollWidth <= track.clientWidth) return
    const left = active.offsetLeft - TAB_EDGE_ROOM
    const right = active.offsetLeft + active.offsetWidth + TAB_EDGE_ROOM - track.clientWidth
    const target = Math.min(Math.max(track.scrollLeft, right), left)
    if (Math.abs(target - track.scrollLeft) < 1) return
    const smooth = revealed.current && !prefersReducedMotion()
    track.scrollTo({ left: target, behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  // `follow` keeps whatever motion is under way: a tab growing bold as it becomes
  // active, a web font arriving, or a window resize should not cut a glide short.
  const place = useCallback((motion: 'glide' | 'follow') => {
    const track = ref.current
    if (!track) return
    const active = track.querySelector<HTMLElement>(':scope > .active')
    if (!active || !active.getClientRects().length) {
      track.dataset.indicator = 'hidden'
      return
    }
    const wasShown = track.dataset.indicator === 'still' || track.dataset.indicator === 'glide'
    // Measured from layout boxes; dividing by `scale` undoes a dialog's entrance zoom.
    const box = track.getBoundingClientRect()
    const tab = active.getBoundingClientRect()
    const scale = box.width / track.offsetWidth || 1
    const left = (tab.left - box.left) / scale - track.clientLeft + track.scrollLeft
    const bottom = (tab.bottom - box.top) / scale - track.clientTop + track.scrollTop
    // Snap both edges to device pixels, as layout does for the border it replaces.
    const ratio = window.devicePixelRatio || 1
    const snap = (value: number) => Math.round(value * ratio) / ratio
    const start = snap(left)
    track.style.setProperty('--indicator-x', `${start}px`)
    track.style.setProperty('--indicator-w', `${snap(left + tab.width / scale) - start}px`)
    track.style.setProperty('--indicator-bottom', `${snap(bottom)}px`)
    if (!wasShown || prefersReducedMotion()) track.dataset.indicator = 'still'
    else if (motion === 'glide') track.dataset.indicator = 'glide'
    edges()
  }, [edges])

  useLayoutEffect(() => {
    place('glide')
    reveal()
    revealed.current = true
  }, [key, place, reveal])

  useEffect(() => {
    const track = ref.current
    if (!track) return
    track.addEventListener('scroll', edges, { passive: true })
    return () => track.removeEventListener('scroll', edges)
  }, [key, edges])

  useEffect(() => {
    const track = ref.current
    if (!track || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => place('follow'))
    observer.observe(track)
    for (const child of track.children) observer.observe(child)
    return () => observer.disconnect()
  }, [key, place])

  useEffect(() => {
    document.fonts?.ready.then(() => place('follow')).catch(() => undefined)
  }, [place])

  return ref
}

/**
 * One highlight box that slides to whichever child of the list has `.active`,
 * up and down a sidebar or along it once it wraps into a row. Like
 * useTabIndicator, it writes its place as CSS variables (`--highlight-x/y/w/h`)
 * and its state as `data-highlight`, and the stylesheet draws it. The list must
 * be positioned so the children's offsets are measured from it.
 */
export function useSlidingHighlight<T extends HTMLElement>(key: string) {
  const ref = useRef<T>(null)
  const glidingUntil = useRef(0)

  // `follow` keeps pace with rows that move under it, such as one folding away:
  // it sticks to its row frame by frame, unless a glide is still under way.
  const place = useCallback((motion: 'glide' | 'follow') => {
    const list = ref.current
    if (!list) return
    const active = list.querySelector<HTMLElement>(':scope > .active')
    if (!active || !active.getClientRects().length) {
      list.dataset.highlight = 'hidden'
      return
    }
    const wasShown = list.dataset.highlight === 'still' || list.dataset.highlight === 'glide'
    // Layout offsets, so a row's own transform or fade does not move the box.
    list.style.setProperty('--highlight-x', `${active.offsetLeft}px`)
    list.style.setProperty('--highlight-y', `${active.offsetTop}px`)
    list.style.setProperty('--highlight-w', `${active.offsetWidth}px`)
    list.style.setProperty('--highlight-h', `${active.offsetHeight}px`)
    if (!wasShown || prefersReducedMotion()) {
      list.dataset.highlight = 'still'
    } else if (motion === 'glide') {
      list.dataset.highlight = 'glide'
      glidingUntil.current = performance.now() + HIGHLIGHT_GLIDE_MS
    } else if (performance.now() > glidingUntil.current) {
      list.dataset.highlight = 'still'
    }
  }, [])

  useLayoutEffect(() => place('glide'), [key, place])

  useEffect(() => {
    const list = ref.current
    if (!list || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => place('follow'))
    observer.observe(list)
    for (const child of list.children) observer.observe(child)
    return () => observer.disconnect()
  }, [key, place])

  useEffect(() => {
    document.fonts?.ready.then(() => place('follow')).catch(() => undefined)
  }, [place])

  return ref
}

/**
 * Swaps one whole screen for another as a cross-fade, such as signing out onto
 * the sign-in page. `apply` makes the change; React updates in it are flushed
 * inside the transition so the new screen is what fades in.
 */
export function crossfade(apply: () => void) {
  if (document.startViewTransition && !prefersReducedMotion()) {
    quietly(document.startViewTransition(() => flushSync(apply)))
  } else apply()
}

/** A transition the browser skips (the window resized, another one started) rejects
 * its promises; the change itself has still been applied, so that is not an error. */
function quietly(transition: ViewTransition): ViewTransition {
  for (const step of [transition.ready, transition.updateCallbackDone, transition.finished]) {
    step.catch(() => undefined)
  }
  return transition
}

/**
 * Switches light and dark as one cross-fade instead of a flash. Element color
 * transitions are paused for the swap so nothing fades on its own schedule.
 */
export function crossfadeTheme(apply: () => void) {
  const root = document.documentElement
  root.classList.add('theme-switching')
  const settle = () =>
    requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('theme-switching')))
  if (document.startViewTransition && !prefersReducedMotion()) {
    quietly(document.startViewTransition(apply)).finished.finally(settle).catch(() => undefined)
  } else {
    apply()
    settle()
  }
}

/**
 * A sidebar that scrolls with the page. One that fits the window stays pinned under
 * the header; a taller one moves with the content until its last card is in view,
 * then stays there, so nothing in it is ever out of reach and it has no scroll of
 * its own. Sets `--sticky-top` on the element. `key` changes when the sidebar may
 * have appeared or changed, such as on switching tabs.
 */
export function useStickySidebar<T extends HTMLElement>(key: string, headerOffset = 88, bottomGap = 16) {
  const ref = useRef<T>(null)
  useEffect(() => {
    const element = ref.current
    if (!element) return
    const place = () => {
      const room = window.innerHeight - element.offsetHeight - bottomGap
      element.style.setProperty('--sticky-top', `${Math.min(headerOffset, room)}px`)
    }
    place()
    window.addEventListener('resize', place)
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place)
    observer?.observe(element)
    return () => {
      window.removeEventListener('resize', place)
      observer?.disconnect()
    }
  }, [key, headerOffset, bottomGap])
  return ref
}
