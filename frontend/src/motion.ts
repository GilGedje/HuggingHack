import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

/** Critically damped feel: fast start, long soft settle, no overshoot. */
const EASE_OUT = 'cubic-bezier(0.22, 1, 0.36, 1)'
/** How long a dialog takes to leave; matches `dialog-out` in styles.css. */
const DIALOG_EXIT_MS = 160
/** How long a toast takes to leave; matches `toast-out` in styles.css. */
export const TOAST_EXIT_MS = 180
/** How long the upload panel takes to leave; matches `.upload-dock.leaving`. */
export const DOCK_EXIT_MS = 180

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
 */
export function useClosingTransition(onClose: () => void) {
  const [closing, setClosing] = useState(false)
  const timer = useRef(0)
  const latest = useRef(onClose)
  latest.current = onClose
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const close = useCallback(() => {
    if (timer.current) return
    if (prefersReducedMotion()) {
      latest.current()
      return
    }
    setClosing(true)
    timer.current = window.setTimeout(() => latest.current(), DIALOG_EXIT_MS)
  }, [])
  return { closing, close }
}

/**
 * Slides one shared underline to the active tab instead of swapping borders.
 * The returned ref goes on the element whose direct children are the tabs; it
 * gets `--indicator-x`, `--indicator-w` and `--indicator-bottom`, plus
 * `data-indicator` = `still` (placed without motion), `glide` (moves with a
 * transition) or `hidden` (no active tab). Without JavaScript the tabs keep
 * their own borders. `key` must change whenever the active tab or the set of
 * tabs does.
 */
export function useTabIndicator<T extends HTMLElement>(key: string) {
  const ref = useRef<T>(null)

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
  }, [])

  useLayoutEffect(() => place('glide'), [key, place])

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
 * Switches light and dark as one cross-fade instead of a flash. Element color
 * transitions are paused for the swap so nothing fades on its own schedule.
 */
export function crossfadeTheme(apply: () => void) {
  const root = document.documentElement
  root.classList.add('theme-switching')
  const settle = () =>
    requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('theme-switching')))
  if (document.startViewTransition && !prefersReducedMotion()) {
    document.startViewTransition(apply).finished.finally(settle)
  } else {
    apply()
    settle()
  }
}
