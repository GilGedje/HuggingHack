import assert from 'node:assert/strict'
import { test } from 'node:test'
import { parseParallel, transferMemoryBytes } from '../src/transfers.ts'

test('parallel transfers are whole numbers within the limit', () => {
  assert.equal(parseParallel('16', 64), 16)
  assert.equal(parseParallel(' 64 ', 64), 64)
  assert.equal(parseParallel('1', 64), 1)
  for (const bad of ['', '0', '65', '-4', '4.5', '1e2', 'eight', '08x']) assert.equal(parseParallel(bad, 64), null, bad)
})

test('a move holds one part per transfer in memory', () => {
  assert.equal(transferMemoryBytes(64, 64), 4 * 1024 ** 3)
  assert.equal(transferMemoryBytes(4, 64), 256 * 1024 ** 2)
})
