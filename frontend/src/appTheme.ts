import { useEffect, useRef, useState } from 'react'
import { crossfadeTheme } from './motion'
import {
  THEME_EVENT,
  THEME_STORAGE_KEY,
  applyTheme,
  darkQuery,
  readThemePreference,
  resolveTheme,
  storedThemePreference,
  type Theme,
  type ThemePreference,
} from './theme'

/**
 * The theme on screen, for the whole app, signed in or not. The account's saved
 * preference wins once it is known; choices announced with
 * `announceThemePreference` apply at once; "system" follows the device live.
 * Changes after the first paint cross-fade.
 */
export function useAppTheme(accountPreference: unknown): Theme {
  const [preference, setPreference] = useState<ThemePreference>(
    () => readThemePreference(accountPreference) || storedThemePreference(),
  )
  const [systemDark, setSystemDark] = useState(() => Boolean(darkQuery()?.matches))
  const theme = resolveTheme(preference, systemDark)
  const applied = useRef(false)

  useEffect(() => {
    const saved = readThemePreference(accountPreference)
    if (saved) setPreference(saved)
  }, [accountPreference])

  useEffect(() => {
    const pick = (event: Event) => {
      const value = readThemePreference((event as CustomEvent<unknown>).detail)
      if (value) setPreference(value)
    }
    window.addEventListener(THEME_EVENT, pick)
    return () => window.removeEventListener(THEME_EVENT, pick)
  }, [])

  useEffect(() => {
    const query = darkQuery()
    if (!query) return
    const follow = () => setSystemDark(query.matches)
    query.addEventListener?.('change', follow)
    return () => query.removeEventListener?.('change', follow)
  }, [])

  useEffect(() => {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, preference)
    } catch {
      // The theme still applies for this page view.
    }
  }, [preference])

  useEffect(() => {
    if (applied.current && document.documentElement.dataset.theme !== theme) crossfadeTheme(() => applyTheme(theme))
    else applyTheme(theme)
    applied.current = true
  }, [theme])

  return theme
}
