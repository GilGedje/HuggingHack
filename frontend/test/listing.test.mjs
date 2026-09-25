import assert from 'node:assert/strict'
import test from 'node:test'
import { parseParameters, parseTags, tidyOverrides, writeParameters } from '../src/listingFields.ts'
import { listingPreviewRequest, safetensorsHeader } from '../src/listingPreview.ts'

function shard(header) {
  const json = new TextEncoder().encode(JSON.stringify(header))
  const length = new Uint8Array(8)
  new DataView(length.buffer).setBigUint64(0, BigInt(json.length), true)
  return new Blob([length, json, new Uint8Array(64)])
}

test('model sizes read and write the way people say them', () => {
  assert.equal(parseParameters('8B'), 8_000_000_000)
  assert.equal(parseParameters(' 595m '), 595_000_000)
  assert.equal(parseParameters('1.5T'), 1_500_000_000_000)
  assert.equal(parseParameters('7,241,732'), 7_241_732)
  for (const bad of ['', 'eight', '0', '8 GB', '-3B']) assert.equal(parseParameters(bad), null, bad)
  assert.equal(writeParameters(8_000_000_000), '8B')
  assert.equal(writeParameters(8_190_000_000), '8.19B')
  assert.equal(writeParameters(595_000_000), '595M')
  assert.equal(writeParameters(999), '999')
})

test('tags split on commas without blanks or repeats', () => {
  assert.deepEqual(parseTags('llama, fp8,, llama ,vision'), ['llama', 'fp8', 'vision'])
})

test('a correction that matches the files is not a correction', () => {
  const detected = { precision: 'fp8', tags: ['llama'], parameter_count: 8 }
  assert.deepEqual(
    tidyOverrides({ precision: 'fp8', tags: ['llama', 'x'], parameter_count: null, license: 'mit' }, detected),
    { tags: ['llama', 'x'], license: 'mit' },
  )
})

test('a preview request carries headers and text files, never weights', async () => {
  const header = { 'a.weight': { dtype: 'BF16', shape: [4], data_offsets: [0, 8] } }
  assert.deepEqual(JSON.parse(await safetensorsHeader(shard(header))), header)
  assert.equal(await safetensorsHeader(new Blob([new Uint8Array(4)])), null)
  const request = await listingPreviewRequest('me/tiny', [
    { path: 'config.json', file: new Blob(['{"model_type":"llama"}']) },
    { path: 'README.md', file: new Blob(['---\nlicense: mit\n---\n']) },
    { path: 'model.safetensors', file: shard(header) },
    { path: 'nested/config.json', file: new Blob(['{}']) },
  ])
  assert.equal(request.config, '{"model_type":"llama"}')
  assert.equal(request.quant_config, null)
  assert.deepEqual(Object.keys(request.headers), ['model.safetensors'])
  assert.deepEqual(request.paths, ['config.json', 'README.md', 'model.safetensors', 'nested/config.json'])
})

test('corrections compare without regard to field order', async () => {
  const { sameOverrides } = await import('../src/listingFields.ts')
  assert.equal(sameOverrides({ precision: 'fp8', tags: ['a'] }, { tags: ['a'], precision: 'fp8' }), true)
  assert.equal(sameOverrides({ precision: 'fp8' }, { precision: 'nvfp4' }), false)
})
