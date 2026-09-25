import assert from 'node:assert/strict'
import test from 'node:test'
import { describeDevice } from '../src/utils.ts'

test('phones are named before the desktop systems their agents mention', () => {
  const iphone =
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
  assert.equal(describeDevice(iphone), 'Safari on iOS')
  const ipadChrome =
    'Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0 Mobile/15E148 Safari/604.1'
  assert.equal(describeDevice(ipadChrome), 'Chrome on iOS')
  const android =
    'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36'
  assert.equal(describeDevice(android), 'Chrome on Android')
})

test('desktop browsers and tools', () => {
  const mac =
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15'
  assert.equal(describeDevice(mac), 'Safari on macOS')
  const edge =
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 Edg/126.0'
  assert.equal(describeDevice(edge), 'Edge on Windows')
  assert.equal(describeDevice('Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0'), 'Firefox on Linux')
  assert.equal(describeDevice('curl/8.6.0'), 'Command line')
  assert.equal(describeDevice(null), 'Unknown device')
})
