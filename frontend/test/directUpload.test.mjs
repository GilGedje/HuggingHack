import assert from 'node:assert/strict'
import test from 'node:test'
import {
  LINKS_PER_REQUEST,
  PARALLEL_PARTS,
  acknowledgedBytes,
  partRange,
  pendingParts,
  storageHost,
  storageUnreachableMessage,
  uploadDirect,
} from '../src/directUpload.ts'
import { directUrl } from '../src/api.ts'

const MB = 1024 * 1024
const state = (overrides = {}) => ({
  direct: true,
  path: 'model.safetensors',
  size: 11 * MB,
  part_size: 5 * MB,
  part_count: 3,
  complete: false,
  done: [],
  ...overrides,
})

test('parts cover the file exactly, the last one shorter', () => {
  assert.deepEqual(partRange(state(), 1), [0, 5 * MB])
  assert.deepEqual(partRange(state(), 3), [10 * MB, 11 * MB])
  assert.equal(acknowledgedBytes(state(), [1, 3]), 6 * MB)
  assert.deepEqual(pendingParts(state({ done: [2] })), [1, 3])
  assert.deepEqual(pendingParts(state({ complete: true, done: [1, 2, 3] })), [])
})

test('the storage is named by host, with a sentence about trust and CORS', () => {
  const url = 'https://s3.grid.example:9443/models/a?X-Amz-Signature=secret'
  assert.equal(storageHost(url), 's3.grid.example:9443')
  const message = storageUnreachableMessage(url)
  assert.match(message, /^The browser could not reach the storage at s3\.grid\.example:9443\./)
  assert.match(message, /private certificate authority/)
  assert.doesNotMatch(message, /secret|Signature/)
})

test('direct upload requests go to the repository or the change session', () => {
  assert.equal(directUrl({ repoId: 'acme/m' }, 'begin'), '/api/uploads/repositories/files/begin?repo_id=acme%2Fm')
  assert.equal(directUrl({ sessionId: 'abc' }, 'parts'), '/api/repos/changes/abc/files/parts')
})

function fakeServer(parts) {
  const linkRequests = []
  return {
    linkRequests,
    endpoints: {
      parts: async (numbers) => {
        linkRequests.push(numbers)
        return { parts: numbers.map((number) => ({ number, url: `https://s3.grid.example/k?partNumber=${number}`, size: 0 })) }
      },
      complete: async () => ({ ...state(), complete: true, done: [1, 2, 3] }),
    },
    parts,
  }
}

test('only missing parts are sent, several at once, and progress counts what arrived', async () => {
  const sent = []
  let inFlight = 0
  let most = 0
  const server = fakeServer()
  const progress = []
  const put = async (url, body) => {
    inFlight += 1
    most = Math.max(most, inFlight)
    await new Promise((resolve) => setTimeout(resolve, 5))
    sent.push([Number(new URL(url).searchParams.get('partNumber')), body.size])
    inFlight -= 1
    return new Response(null, { status: 200 })
  }
  const file = new Blob([new Uint8Array(11 * MB)])
  await uploadDirect(file, state({ done: [2] }), server.endpoints, (bytes) => progress.push(bytes), undefined, put)
  assert.deepEqual(sent.sort((a, b) => a[0] - b[0]), [[1, 5 * MB], [3, MB]])
  assert.ok(most >= 2 && most <= PARALLEL_PARTS)
  assert.deepEqual(server.linkRequests, [[1, 3]])
  assert.equal(progress[0], 5 * MB)
  assert.equal(progress.at(-1), 11 * MB)
})

test('links are asked for in batches', async () => {
  const count = LINKS_PER_REQUEST + 2
  const server = fakeServer()
  server.endpoints.complete = async () => ({ ...state(), complete: true })
  const big = state({ size: count * 5 * MB, part_count: count })
  await uploadDirect(new Blob([new Uint8Array(1)]), big, server.endpoints, () => {}, undefined, async () => new Response(null, { status: 200 }))
  assert.deepEqual(server.linkRequests.map((batch) => batch.length), [LINKS_PER_REQUEST, 2])
})

test('a refused part is signed again and retried; an unreachable storage stops with its sentence', async () => {
  const server = fakeServer()
  let tries = 0
  await uploadDirect(new Blob([new Uint8Array(11 * MB)]), state({ done: [1, 2] }), server.endpoints, () => {}, undefined, async () => {
    tries += 1
    return new Response(null, { status: tries === 1 ? 403 : 200 })
  })
  assert.equal(tries, 2)
  assert.deepEqual(server.linkRequests, [[3], [3]])

  const unreachable = uploadDirect(new Blob([new Uint8Array(11 * MB)]), state({ done: [1, 2] }), fakeServer().endpoints, () => {}, undefined, async () => {
    throw new TypeError('Failed to fetch')
  })
  await assert.rejects(unreachable, /could not reach the storage at s3\.grid\.example/)
})

test('cancelling stops at once without retrying', async () => {
  const controller = new AbortController()
  let tries = 0
  const put = async () => {
    tries += 1
    controller.abort()
    throw new DOMException('The operation was aborted.', 'AbortError')
  }
  await assert.rejects(
    uploadDirect(new Blob([new Uint8Array(11 * MB)]), state({ done: [1, 2] }), fakeServer().endpoints, () => {}, controller.signal, put),
    { name: 'AbortError' },
  )
  assert.equal(tries, 1)
})
