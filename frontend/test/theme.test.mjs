import assert from 'node:assert/strict'
import test from 'node:test'
import { brandMark, readThemePreference, resolveTheme, storedThemePreference } from '../src/theme.ts'

test('a device preference follows the device; a chosen theme stays', () => {
  assert.equal(resolveTheme('system', true), 'dark')
  assert.equal(resolveTheme('system', false), 'light')
  assert.equal(resolveTheme('light', true), 'light')
  assert.equal(resolveTheme('dark', false), 'dark')
})

test('only known preferences are read, and a browser without storage follows the device', () => {
  assert.equal(readThemePreference('system'), 'system')
  assert.equal(readThemePreference('dark'), 'dark')
  assert.equal(readThemePreference('sepia'), null)
  assert.equal(readThemePreference(undefined), null)
  assert.equal(storedThemePreference(), 'system')
})

test('dark mode uses the logo whose brackets stay visible on dark backgrounds', () => {
  assert.equal(brandMark('light'), '/hugginghack-mark.svg')
  assert.equal(brandMark('dark'), '/hugginghack-mark-dark.svg')
})
