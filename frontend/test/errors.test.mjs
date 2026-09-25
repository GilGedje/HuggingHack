import assert from 'node:assert/strict'
import test from 'node:test'
import { errorDetail } from '../src/api.ts'
import { ssoErrorMessage } from '../src/ssoError.ts'

test('a validation error list becomes one readable sentence', () => {
  const detail = [
    { type: 'string_too_short', loc: ['body', 'display_name'], msg: 'String should have at least 1 character', input: '' },
  ]
  assert.equal(errorDetail(detail, 'fallback'), 'Display name: String should have at least 1 character')
  assert.equal(
    errorDetail([{ loc: ['body'], msg: 'Value error, pick a role' }, { loc: ['query', 'page'], msg: 'bad' }], 'fallback'),
    'Pick a role (and 1 more problem)',
  )
})

test('plain details pass through and odd ones fall back', () => {
  assert.equal(errorDetail('Token not found.', 'fallback'), 'Token not found.')
  assert.equal(errorDetail(undefined, 'Request failed with status 500'), 'Request failed with status 500')
  assert.equal(errorDetail([], 'fallback'), 'fallback')
  assert.equal(errorDetail([{ loc: ['body'] }], 'fallback'), 'fallback')
  assert.equal(errorDetail({ message: 'x' }, 'fallback'), 'fallback')
})

test('single sign-on errors only ever show a fixed message', () => {
  assert.equal(ssoErrorMessage(''), '')
  assert.equal(ssoErrorMessage('expired'), 'This sign-in link expired or was already used. Try again.')
  assert.equal(
    ssoErrorMessage('The identity provider refused the sign-in (access_denied).'),
    'The identity provider refused the sign-in.',
  )
  const forged = 'Your session expired. Sign in again at https://evil.example'
  assert.doesNotMatch(ssoErrorMessage(forged), /evil/)
  assert.match(ssoErrorMessage(forged), /^Single sign-on did not finish/)
  assert.match(ssoErrorMessage('toString'), /^Single sign-on did not finish/)
})
