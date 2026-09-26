export type Theme = 'light' | 'dark'
/** What someone chose: a theme, or whatever this device uses. */
export type ThemePreference = Theme | 'system'

export const THEME_STORAGE_KEY = 'hugginghack-theme'
/** Fired with a ThemePreference as `detail` whenever someone picks one. */
export const THEME_EVENT = 'hugginghack:theme'
/** The page background of each theme, for the browser's own bars. */
const CHROME: Record<Theme, string> = { light: '#ffffff', dark: '#121315' }

export function readThemePreference(value: unknown): ThemePreference | null {
  return value === 'light' || value === 'dark' || value === 'system' ? value : null
}

export function resolveTheme(preference: ThemePreference, systemDark: boolean): Theme {
  if (preference === 'system') return systemDark ? 'dark' : 'light'
  return preference
}

export function darkQuery(): MediaQueryList | null {
  return typeof window === 'undefined' ? null : window.matchMedia?.('(prefers-color-scheme: dark)') || null
}

/** The last choice made in this browser, so the sign-in page and the first paint
 * match it before any account is known; the device's theme when there is none. */
export function storedThemePreference(): ThemePreference {
  try {
    return readThemePreference(localStorage.getItem(THEME_STORAGE_KEY)) || 'system'
  } catch {
    return 'system'
  }
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', CHROME[theme])
}

/** Says a theme was picked, so everything showing the choice follows it. */
export function announceThemePreference(preference: ThemePreference) {
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: preference }))
}

/** The logo for a theme: on dark backgrounds its side brackets turn light, since the
 * light logo's near-black brackets disappear there. Both files live in public/. */
export function brandMark(theme: Theme): string {
  return theme === 'dark' ? '/hugginghack-mark-dark.svg' : '/hugginghack-mark.svg'
}
