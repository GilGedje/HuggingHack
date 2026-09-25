import assert from 'node:assert/strict'
import test from 'node:test'

import { metadataPreview } from '../src/gguf.ts'

test('GGUF metadata arrays are summarized without retaining the full value', () => {
  assert.deepEqual(metadataPreview(['one', 'two', 'three', 'four', 'five']), {
    value: '["one", "two", "three", "four", …]',
    itemCount: 5,
  })
  assert.deepEqual(metadataPreview([[1, 2, 3], true]), {
    value: '[[3 items], true]',
    itemCount: 2,
  })
})

test('GGUF scalar metadata is readable and bounded', () => {
  assert.deepEqual(metadataPreview(32n), { value: '32' })
  assert.deepEqual(metadataPreview('line one\r\nline two'), {
    value: 'line one\nline two',
  })
  assert.equal(metadataPreview('x'.repeat(600)).value.length, 501)
})

test('GGUF header cache follows the file, not only its name', async () => {
  const { ggufCacheKey, ggufCacheReusable } = await import('../src/gguf.ts')
  const file = { path: 'model.gguf', size: 100 }
  assert.notEqual(ggufCacheKey('a/b', 'main', file), ggufCacheKey('a/b', 'main', { ...file, size: 101 }))
  assert.notEqual(
    ggufCacheKey('a/b', 'main', { ...file, blob_id: 'x' }),
    ggufCacheKey('a/b', 'main', { ...file, blob_id: 'y' }),
  )
  // Library files have no content hash: only the same listing may reuse a header.
  assert.equal(ggufCacheReusable(file, file), true)
  assert.equal(ggufCacheReusable(file, { ...file }), false)
  assert.equal(ggufCacheReusable({ ...file, blob_id: 'x' }, { ...file, blob_id: 'x' }), true)
})
