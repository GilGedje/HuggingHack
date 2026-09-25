import assert from 'node:assert/strict'
import test from 'node:test'
import { UNREACHABLE_MESSAGE, api, errorDetail, statusMessage } from '../src/api.ts'
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

test('failures without a reason get a sentence, not a status code', () => {
  assert.equal(statusMessage(502), 'HuggingHack is not answering right now. Try again in a moment.')
  assert.equal(statusMessage(504), statusMessage(503))
  assert.match(statusMessage(500), /^HuggingHack ran into a problem/)
  assert.match(statusMessage(413), /too large/)
  assert.equal(statusMessage(418), 'The server refused the request (status 418).')
})

test('an unreachable server and a proxy error page read as sentences', async (t) => {
  const original = globalThis.fetch
  t.after(() => {
    globalThis.fetch = original
  })
  globalThis.fetch = async () => {
    throw new TypeError('Failed to fetch')
  }
  await assert.rejects(api.account(), { message: UNREACHABLE_MESSAGE })

  globalThis.fetch = async () => new Response('<html><body>502 Bad Gateway</body></html>', { status: 502 })
  await assert.rejects(api.account(), { message: statusMessage(502) })

  // A reason the server gives still wins.
  globalThis.fetch = async () => Response.json({ detail: 'The database is not reachable.' }, { status: 503 })
  await assert.rejects(api.account(), { message: 'The database is not reachable.' })

  // Cancelling is the caller's doing and stays an abort.
  globalThis.fetch = async () => {
    throw new DOMException('The operation was aborted.', 'AbortError')
  }
  await assert.rejects(api.account(), { name: 'AbortError' })
})
